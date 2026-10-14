"""The supervisor: queue, admission, process ownership, stall checks (docs/02 §2.2, §2.10, §2.11).

One per host, started by launchd (KeepAlive). Every tick it:
- writes its own tick (the health job watches it) and notes host sleep;
- reaps drivers that exited, and requeues runs whose driver died without ending them;
- checks each live run's phase against its bound, and kills and requeues stalled runs;
- expires consumer leases, delivers due notices;
- admits queued work under host budget, per-project limits and host-load admission,
  writing a refusal row (I10) for anything it does not start.
It never adopts a process it did not start (I4): at start-up it terminates survivors.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import batches, bounds, clock, faults, gitops, lease, paths, procs
from .runs import counts_as_restart, fenced, has_unfinished_cancel, ready_plans, root_in_use_by_other
from .store import Conflict, Store, new_id

HOST_BUDGET = 2  # Q15: ilk2 runs per host, an experiment guarded by load admission
FREE_MEM_MIN = 2 * 1024 ** 3
ACTIVE_RUN = ("starting", "running")


def free_memory() -> int:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = 16384
    pages = 0
    for line in out.splitlines():
        if "page size of" in line:
            page = int(line.split("page size of")[1].split()[0])
        for key in ("Pages free", "Pages inactive", "Pages speculative"):
            if line.startswith(key):
                pages += int(line.split(":")[1].strip().rstrip("."))
    return pages * page


class Supervisor:
    def __init__(self, s: Store, b: bounds.Bounds | None = None, drive_cmd: list[str] | None = None,
                 host_budget: int = HOST_BUDGET, load_check: bool = True):
        self.s = s
        self.b = b or bounds.load()
        self.drive_cmd = drive_cmd or [sys.executable, "-m", "ilk2", "drive"]
        self.host_budget = host_budget
        self.load_check = load_check
        self.children: dict[str, subprocess.Popen] = {}
        self.exit_requested = False
        self.last_wall, self.last_mono = clock.wall(), clock.mono()

    # -- start-up ---------------------------------------------------------------------------

    def start(self) -> None:
        with self.s.tx() as c:
            c.execute("UPDATE supervisor SET pid = ?, proc_start = ?, boot_time = ?, started_wall = ?, "
                      "tick_wall = ?, tick_mono = ? WHERE id = 1",
                      (os.getpid(), procs.start_time(os.getpid()), clock.boot_time(), clock.wall(),
                       clock.wall(), clock.mono()))
        self.s.event("supervisor.start", {"pid": os.getpid()})
        with self.s.tx() as c:  # what an upgrade compares to decide whether it must drain
            c.execute("INSERT INTO meta(key, value) VALUES ('supervisor_code', ?) ON CONFLICT(key) "
                      "DO UPDATE SET value = excluded.value", (json.dumps(loaded_code()),))
        self.reconcile()

    def reconcile(self) -> None:
        """Terminate every surviving driver and requeue its run; never adopt (I4, §2.10)."""
        for run in self.s.all("SELECT * FROM runs WHERE status IN ('starting', 'running')"):
            reboot = run["boot_time"] is not None and run["boot_time"] != clock.boot_time()
            if not reboot and procs.is_same_process(run["pid"], run["proc_start"]):
                res = procs.terminate_group(run["pgid"] or run["pid"], term_grace_s=self.b.term_grace,
                                            kill_grace_s=self.b.kill_grace)
                if res["survivors"]:
                    self.fence(run, res["survivors"])
            self.end_and_requeue(run, "lost (reboot)" if reboot else "lost", count=not reboot)

    # -- the tick ---------------------------------------------------------------------------

    def tick(self) -> None:
        faults.hit("supervisor-tick")
        now_w, now_m = clock.wall(), clock.mono()
        if (now_w - self.last_wall) - (now_m - self.last_mono) > self.b.host_slept_gap:
            self.s.event("host-slept", {"gap_s": round((now_w - self.last_wall) - (now_m - self.last_mono))})
        self.last_wall, self.last_mono = now_w, now_m
        with self.s.tx() as c:
            c.execute("UPDATE supervisor SET tick_wall = ?, tick_mono = ? WHERE id = 1", (now_w, now_m))
        self.reload_host_config()
        self.reap()
        self.claim_v1()
        self.close_orphan_attempts()
        self.check_stalls()
        self.handle_cancels()
        for root in lease.expire_due(self.s):
            b = self.s.one("SELECT id FROM batches WHERE execution_root = ? ORDER BY created_at DESC",
                           root)
            self.s.open_needs_human("consumer-lease-expired", f"consumer lease on {root} expired",
                                    f"ilk2 lease recover --root {root}",
                                    batch_id=b["id"] if b else None)
        self.check_waits()
        if now_w - getattr(self, "_pruned_at", 0) > 3600:  # hourly is plenty for a 30-day bound
            self._pruned_at = now_w
            n = self.s.prune_events(self.b.events_keep)
            if n:
                self.s.event("events.pruned", {"count": n})
            gone = self.gc_worktrees()
            if gone:
                self.s.event("worktrees.gc", {"batches": gone})
        if self.drain():
            return
        self.admit()

    def drain(self) -> bool:
        """Upgrade drain (§2.7): admit nothing; when no run is left, or the bound passes,
        exit so launchd restarts the supervisor from the new release. True while draining."""
        if not self.s.one("SELECT 1 FROM meta WHERE key = 'draining' AND value = '1'"):
            return False
        live = self.s.all("SELECT * FROM runs WHERE status IN ('starting', 'running')")
        started = float((self.s.one("SELECT value FROM meta WHERE key = 'drain_started_wall'")
                         or {"value": clock.wall()})["value"])
        if live and clock.wall() - started > self.b.attempt_cap + self.b.run_outer_extra:
            for run in live:
                self.kill_run(run)
                self.end_and_requeue(run, "drained (bound)", count=False)
            live = []
        if not live:
            with self.s.tx() as c:
                c.execute("UPDATE meta SET value = '0' WHERE key = 'draining'")
            self.s.event("supervisor.drained", {})
            self.exit_requested = True
        return True

    def reap(self) -> None:
        for rid, p in list(self.children.items()):
            if p.poll() is None:
                continue
            del self.children[rid]
            run = self.s.get("runs", rid)
            if run["status"] != "ended":
                self.end_and_requeue(run, "interrupted")
            elif counts_as_restart(run["terminal_reason"]):
                # The driver ended its own run with a failure (error, store-busy, ...): that
                # is a restart like a crash, or it would respawn forever (Codex M1 review #3).
                self.after_run_ended(run, run["terminal_reason"])

    def claim_v1(self) -> None:
        """Submit the queued V1 masters of projects taken over from V1 (docs/04 §4.3)."""
        from . import plans as plans_mod, v1
        for p in self.s.all("SELECT id FROM projects"):
            pcfg = batches.project_config(self.s, p["id"])
            key = None if pcfg.retired else pcfg.v1_key
            if not key:
                continue
            for path in v1.claim(key):
                ref = "v1:" + path.name.split(".md")[0]
                try:
                    spec = plans_mod.parse_v1_master(path)
                    bid = batches.submit(self.s, p["id"], spec, consumer_ref=ref,
                                         originals={"v1_master": str(path)})
                    self.s.event("v1.claimed", {"master": path.name, "batch": bid}, batch_id=bid)
                except Exception as e:  # noqa: BLE001 - a bad plan is a planner's job, not a crash
                    self.s.event("v1.claim-rejected", {"master": path.name, "error": str(e)[:500]})
                    self.s.open_needs_human("plan-rejected", f"{ref}: {str(e)[:1500]}",
                                            f"fix the plan, rename {path.name} back to "
                                            f"{path.name.replace(v1.CLAIMED, '')} to resubmit",
                                            project_id=p["id"])

    def uncount_attempt(self, step_id: str, reason: str) -> None:
        """An interrupted attempt was never judged: like an API error (S5) it does not count
        toward the attempt limit. Lost runs are bounded by the crash-loop ladder instead."""
        st = self.s.get("steps", step_id)
        if st and st["attempt_count"] > 0 and st["status"] == "pending":
            self.s.update("steps", step_id, {"attempt_count": st["attempt_count"] - 1},
                          actor="supervisor", reason=f"interrupted attempt not counted ({reason})")

    def close_orphan_attempts(self) -> None:
        """An attempt still `running` under an ended run can never finish: close it (I11)."""
        for a in self.s.all("SELECT a.id, a.step_id, r.ended_at AS run_ended FROM attempts a JOIN runs r ON "
                            "r.id = a.run_id WHERE a.status = 'running' AND r.status = 'ended'"):
            # It stopped when its run ended; the time until someone noticed is not work.
            self.s.transition("attempts", a["id"], "done", actor="supervisor", reason="run ended",
                              fields={"outcome": "interrupted", "reason": "its run had ended",
                                      "ended_at": a["run_ended"] or clock.wall()})
            self.uncount_attempt(a["step_id"], "its run had ended")

    # -- stalls (§2.11.1) -------------------------------------------------------------------

    def phase_bound(self, run) -> float:
        ph = run["phase"]
        if ph == "dispatch":
            return self.b.attempt_cap + self.b.dispatch_grace
        if ph in ("gates", "base", "setup"):
            return self.gate_bound(run["batch_id"]) + self.b.gate_grace
        if ph == "attribute":  # its own wall budget, plus one probe that started just inside it
            return self.b.attribution_wall + self.gate_bound(run["batch_id"]) + self.b.gate_grace
        return self.b.phase

    def gate_bound(self, batch_id: str) -> float:
        caps = [0.0]
        for r in self.s.all("SELECT gates FROM steps WHERE batch_id = ?", batch_id):
            caps += [g.get("cap_s", 600) for g in json.loads(r["gates"])]
        scale = float(os.environ.get("ILK2_TIME_SCALE", "1") or 1)
        # a gate may be re-run once (transient) and timeout-class tests re-run alone
        return 3 * max(caps) / scale

    def check_stalls(self) -> None:
        now = clock.mono()
        for run in self.s.all("SELECT * FROM runs WHERE status IN ('starting', 'running')"):
            if run["boot_time"] not in (None, clock.boot_time()):
                continue
            reason = None
            if run["phase"] is None:
                if now - (run["spawned_mono"] or now) > self.b.spawn:
                    reason = "stalled (spawn)"
            elif now - (run["phase_started_mono"] or now) > self.phase_bound(run):
                reason = f"stalled ({run['phase']})"
            if reason is None and run["last_event_mono"] and \
                    now - run["last_event_mono"] > self.b.attempt_cap + self.b.run_outer_extra:
                reason = "stalled (run)"
            if reason:
                self.kill_run(run)
                self.end_and_requeue(run, reason)

    def kill_run(self, run) -> dict | None:
        pgid = run["pgid"] or run["pid"]
        if not pgid:
            return None
        child = self.children.pop(run["id"], None)
        res = procs.terminate_group(pgid, reap=(child.poll if child else lambda: None),
                                    term_grace_s=self.b.term_grace, kill_grace_s=self.b.kill_grace)
        if res["survivors"]:
            self.fence(run, res["survivors"])
        return res

    def fence(self, run, survivors) -> None:
        """Survivors of SIGKILL may still write to the root: stop the batch where it is, with
        the root lease kept, so nothing requeues or releases it (Codex M1 review #5). `ilk2
        resume` refuses while a process is still inside the root."""
        bid = run["batch_id"]
        self.s.open_needs_human("unkillable", f"run {run['id']} survived SIGKILL: {survivors}",
                                f"kill the listed pids, then: ilk2 resume {bid}", batch_id=bid)
        if self.s.get("batches", bid)["status"] in ("queued", "active", "parked"):
            self.s.transition("batches", bid, "blocked", actor="supervisor", reason="unkillable",
                              fields={"blocked_reason": f"unkillable: {str(survivors)[:400]}"})

    # -- requeue, restart counting, crash loops ----------------------------------------------

    def current_step(self, batch_id: str, plan_id: str | None = None):
        """The step a lost run was on: its own plan's, for a lane run. A lane lost while
        merging has every step accepted: its restarts count on the plan's last step, so a
        crashing merge reaches the crash-loop ladder instead of respawning forever."""
        if plan_id:
            st = self.s.one("SELECT * FROM steps WHERE plan_id = ? AND status IN ('pending', "
                            "'accepting') ORDER BY idx LIMIT 1", plan_id)
            if st is None and self.s.get("plans", plan_id)["status"] not in ("shipped", "blocked",
                                                                              "cancelled"):
                st = self.s.one("SELECT * FROM steps WHERE plan_id = ? ORDER BY idx DESC LIMIT 1",
                                plan_id)
            return st
        for p in self.s.all("SELECT * FROM plans WHERE batch_id = ? AND status IN ('pending', 'active') "
                            + ("AND id = ? " if plan_id else "") + "ORDER BY ord",
                            batch_id, *([plan_id] if plan_id else [])):
            st = self.s.one("SELECT * FROM steps WHERE plan_id = ? AND status IN ('pending', 'accepting') "
                            "ORDER BY idx LIMIT 1", p["id"])
            if st:
                return st
        return None

    def end_and_requeue(self, run, reason: str, count: bool = True) -> None:
        try:
            self.s.transition("runs", run["id"], "ended", actor="supervisor", reason=reason,
                              expect_from=set(ACTIVE_RUN),
                              fields={"terminal_reason": reason, "ended_at": clock.wall()})
        except Conflict:
            return
        self.after_run_ended(run, reason, count)

    def after_run_ended(self, run, reason: str, count: bool = True) -> None:
        self.s.event("run.lost", {"run": run["id"], "reason": reason}, batch_id=run["batch_id"])
        for a in self.s.all("SELECT id, step_id FROM attempts WHERE run_id = ? AND status = 'running'",
                            run["id"]):
            # the attempt in flight is the cost of a lost run (§2.10); close it as interrupted
            self.s.transition("attempts", a["id"], "done", actor="supervisor", reason=reason,
                              fields={"outcome": "interrupted", "reason": reason,
                                      "ended_at": clock.wall()})
            self.uncount_attempt(a["step_id"], reason)
        batch = self.s.get("batches", run["batch_id"])
        if batch["status"] != "active":
            return
        step = self.current_step(run["batch_id"], run["plan_id"] if "plan_id" in run.keys() else None)
        if step is None or not count:
            return
        n = step["restart_count"] + 1
        self.s.update("steps", step["id"], {"restart_count": n}, actor="supervisor",
                      reason=f"restart after {reason}")
        if n < bounds.RESTARTS_BEFORE_HUMAN and n % bounds.RESTARTS_PER_STEP == 0:
            wait = self.b.crash_backoff_first if n == bounds.RESTARTS_PER_STEP else self.b.crash_backoff_second
            self.s.refuse(run["batch_id"], "crash-backoff",
                          f"step {step['id']} lost its run {n} times ({reason}); retrying after "
                          f"{wait / 60:.0f} min", "the backoff ends (automatic)", "supervisor")
            self.s.event("crash-backoff", {"step": step["id"], "restarts": n, "seconds": wait},
                         batch_id=run["batch_id"])
            return
        merging = step["status"] == "accepted"  # a lane lost while merging (current_step)
        if n >= bounds.RESTARTS_BEFORE_HUMAN and (step["status"] == "pending" or merging):
            if not merging:
                self.s.transition("steps", step["id"], "blocked", actor="supervisor", reason="crash-loop",
                                  fields={"blocked_reason": f"crash-loop: {reason}"})
            self.s.transition("plans", step["plan_id"], "blocked", actor="supervisor",
                              reason="crash-loop", fields={"blocked_reason": f"step {step['idx']}: crash-loop"})
            reasons = [r["terminal_reason"] for r in self.s.all(
                "SELECT terminal_reason FROM runs WHERE batch_id = ? ORDER BY created_at DESC LIMIT 3",
                run["batch_id"])]
            self.s.open_needs_human("crash-loop", f"step {step['id']} lost its run {n} times, "
                                    f"after two backoffs: {reasons}",
                                    f"ilk2 resume {run['batch_id']}", batch_id=run["batch_id"])
            others = self.s.one("SELECT COUNT(*) n FROM plans WHERE batch_id = ? AND status IN "
                                "('pending', 'active')", run["batch_id"])["n"]
            if others == 0:
                self.s.transition("batches", run["batch_id"], "blocked", actor="supervisor",
                                  reason="crash-loop", fields={"blocked_reason": "crash-loop"})
                self.release_lease(run["batch_id"])

    def release_lease(self, batch_id: str) -> None:
        b = self.s.get("batches", batch_id)
        root = b["execution_root"]
        row = lease.get(self.s, root)
        if row and row["holder"] == "driver" and not fenced(self.s, batch_id, Path(root)) \
                and not root_in_use_by_other(self.s, batch_id):
            from . import gitops
            lease.driver_release(self.s, root, gitops.branch_head(Path(root), b["branch"]))

    # -- cancel (§2.8) ---------------------------------------------------------------------------

    def handle_cancels(self) -> None:
        """Stop live runs of cancelled batches, and of parked batches with `park --now`, as a
        crash would. Then, keyed on batch state rather than on catching the run (a driver may
        have exited or been reaped first), recover any `accepting` step and release the lease."""
        for run in self.s.all("SELECT r.* FROM runs r JOIN batches b ON b.id = r.batch_id WHERE "
                              "r.status IN ('starting', 'running') AND (b.status = 'cancelled' OR "
                              "(b.status = 'parked' AND b.control = 'park-now'))"):
            self.kill_run(run)
            why = "cancelled" if self.s.get("batches", run["batch_id"])["status"] == "cancelled" \
                else "parked"
            self.end_and_requeue(run, why, count=False)  # also closes its running attempt
        for b in self.s.all("SELECT * FROM batches WHERE status = 'cancelled' OR (status = 'parked' "
                            "AND control = 'park-now')"):
            if self.s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status IN ('starting', 'running')",
                          b["id"]):
                continue
            if b["status"] == "cancelled":
                if has_unfinished_cancel(self.s, b["id"]):
                    _finish_cancel(self.s, b["id"], self.b)
                continue
            row = lease.get(self.s, b["execution_root"])
            accepting = self.s.one("SELECT 1 FROM steps WHERE batch_id = ? AND status = 'accepting'",
                                   b["id"])
            if (accepting or (row and row["holder"] == "driver")) and \
                    not fenced(self.s, b["id"], Path(b["execution_root"])):
                self.finish_park(b["id"])
            if b["status"] == "parked":
                self.s.update("batches", b["id"], {"control": None}, actor="supervisor",
                              reason="park-now done")

    def finish_park(self, batch_id: str) -> None:
        from .driver import Driver
        rid = new_id("r")
        self.s.insert("runs", {"id": rid, "batch_id": batch_id, "status": "ended",
                               "terminal_reason": "park-recovery", "created_at": clock.wall()})
        Driver(self.s, rid, self.b).recover_accepting()
        self.release_lease(batch_id)

    # -- waits: api and quota refusals ------------------------------------------------------

    def check_waits(self) -> None:
        now = clock.wall()
        for r in self.s.open_refusals():
            if r["reason"] == "waiting-api":
                if now - r["opened_at"] >= self.b.api_needs_human:
                    self.s.open_needs_human("api", "model API failing for 6 h", "check the provider",
                                            batch_id=r["batch_id"])
            if r["reason"] == "quota" and now - r["opened_at"] >= self.b.quota_max:
                b = self.s.get("batches", r["batch_id"])
                if b["status"] == "active":
                    self.s.transition("batches", r["batch_id"], "blocked", actor="supervisor",
                                      reason="quota", fields={"blocked_reason": "quota for 24 h"})
                    self.s.open_needs_human("quota", "quota exhausted for 24 h", "top up, then: "
                                            f"ilk2 resume {r['batch_id']}", batch_id=r["batch_id"])
                    self.s.clear_refusal(r["batch_id"], "quota")

    def wait_over(self, batch_id: str) -> bool:
        now = clock.wall()
        for r in self.s.open_refusals(batch_id):
            if r["reason"] == "waiting-api" and now - r["opened_at"] < self.b.api_probe:
                return False
            if r["reason"] == "quota" and now - r["opened_at"] < self.b.quota_probe:
                return False
            if r["reason"] == "root-lease":
                b = self.s.get("batches", batch_id)
                row = lease.get(self.s, b["execution_root"])
                if row and row["holder"] in ("consumer", "consumer-expired"):
                    return False
            if r["reason"] == "crash-backoff":
                step = self.current_step(batch_id)
                second = step is not None and step["restart_count"] > bounds.RESTARTS_PER_STEP
                wait = self.b.crash_backoff_second if second else self.b.crash_backoff_first
                if now - r["opened_at"] < wait:
                    return False
        return True

    # -- admission (§2.11.1 host load, I10) -------------------------------------------------

    def load_refusal(self) -> str | None:
        if not self.load_check:
            return None
        load5 = os.getloadavg()[1]
        cores = os.cpu_count() or 1
        if load5 > cores:
            return f"5-minute load {load5:.1f} > {cores} cores"
        mem = free_memory()
        if mem < FREE_MEM_MIN:
            return f"free memory {mem / 1024 ** 3:.1f} GB < 2 GB"
        since = clock.mono() - self.b.load_timeout_window
        n = self.s.one("SELECT COUNT(*) n FROM events WHERE kind = 'gate.timeout' AND mono > ?",
                       since)["n"]
        if n >= bounds.LOAD_TIMEOUTS:
            return f"{n} gate timeouts in the last 30 min"
        return None

    def admit(self) -> None:
        live_runs = self.s.all("SELECT batch_id FROM runs WHERE status IN ('starting', 'running')")
        live = {r["batch_id"] for r in live_runs}
        n_live = len(live_runs)  # the host budget counts runs: under lanes a batch has several
        candidates = self.s.all("SELECT * FROM batches WHERE status IN ('queued', 'active') "
                                "ORDER BY priority DESC, created_at")
        load = None
        load_checked = False
        for b in candidates:
            lane_plan = None
            cfg = batches.project_config(self.s, b["project_id"])
            if b["id"] in live:
                self.s.clear_refusal(b["id"], "host-budget")
                lane_plan = self.next_lane(b, cfg)
                if lane_plan is None:
                    continue
            elif cfg.max_lanes > 1:
                ready = ready_plans(self.s, b["id"])
                lane_plan = ready[0]["id"] if ready else None  # none ready: a plain run ships or blocks
            if not self.wait_over(b["id"]):
                continue
            per_project = sum(1 for x in self.s.all(
                "SELECT r.batch_id FROM runs r JOIN batches b ON b.id = r.batch_id WHERE "
                "r.status IN ('starting', 'running') AND b.project_id = ?", b["project_id"]))
            limit = batches.project_config(self.s, b["project_id"]).concurrency_limit
            if per_project >= limit:
                self.s.refuse(b["id"], "project-limit", f"{per_project} of {limit} runs in use",
                              "a run of this project ends", "supervisor")
                continue
            busy_root = self.s.one(
                "SELECT r.batch_id FROM runs r JOIN batches o ON o.id = r.batch_id WHERE r.status IN "
                "('starting', 'running') AND o.execution_root = ? AND o.id != ?",
                b["execution_root"], b["id"])
            if busy_root:  # R4: one batch at a time on a root
                self.s.refuse(b["id"], "root-busy", f"batch {busy_root['batch_id']} runs on the same root",
                              "that run ends", "supervisor")
                continue
            reserved = self.reserved_for_others(b["project_id"])
            if n_live + reserved >= self.host_budget:
                why = f"{n_live} of {self.host_budget} runs in use" + (
                    f", {reserved} reserved for other projects (R5)" if reserved else "")
                self.s.refuse(b["id"], "host-budget", why, "a run ends", "supervisor")
                continue
            if not load_checked:
                load, load_checked = self.load_refusal(), True
            if load:
                self.s.refuse(b["id"], "host-load", load, "host load falls", "supervisor")
                continue
            for reason in ("project-limit", "host-budget", "host-load", "waiting-api", "quota",
                           "crash-backoff", "root-lease", "root-busy"):
                self.s.clear_refusal(b["id"], reason)
            self.spawn(b, plan_id=lane_plan)
            live.add(b["id"])
            n_live += 1

    def gc_worktrees(self) -> list[str]:
        """Remove what a finished batch left on disk once it has been finished for
        worktrees_keep: its ilk2-made worktree (the branch stays), its diag worktrees, and its
        runs' temp dirs (logs stay). A root another unfinished batch uses is left alone."""
        import shutil
        now, out = clock.wall(), []
        for b in self.s.all(
                "SELECT b.*, p.repo_root, (SELECT MAX(t.at) FROM transitions t WHERE t.entity = "
                "'batches' AND t.entity_id = b.id AND t.to_state IN ('shipped', 'cancelled')) AS "
                "ended FROM batches b JOIN projects p ON p.id = b.project_id WHERE b.status IN "
                "('shipped', 'cancelled')"):
            if not b["ended"] or now - b["ended"] < self.b.worktrees_keep:
                continue
            root = Path(b["execution_root"])
            if self.s.one("SELECT 1 FROM batches WHERE execution_root = ? AND status NOT IN "
                          "('shipped', 'cancelled')", b["execution_root"]):
                continue
            did = False
            if root.parent == paths.worktrees() and root.exists():
                gitops.remove_worktree(Path(b["repo_root"]), root)
                did = True
            diag = paths.diag(b["id"])
            if diag.exists():
                for w in diag.iterdir():
                    gitops.remove_worktree(Path(b["repo_root"]), w)
                shutil.rmtree(diag, ignore_errors=True)
                did = True
            for r in self.s.all("SELECT id FROM runs WHERE batch_id = ?", b["id"]):
                for tmp in paths.run_dir(r["id"]).glob("a-*/tmp"):
                    shutil.rmtree(tmp, ignore_errors=True)
                    did = True
            if did:
                out.append(b["id"])
        return out

    def reload_host_config(self) -> None:
        """host.json is re-read every tick: a budget change needs no supervisor restart, which
        would interrupt every live attempt (Chad, Oct 14: 3+ kira batches at once)."""
        try:
            hc = host_config()
        except (OSError, ValueError):
            return  # a half-written file: keep the current settings
        if "host_budget" in hc and int(hc["host_budget"]) != self.host_budget:
            self.s.event("host.config", {"host_budget": int(hc["host_budget"]), "was": self.host_budget})
            self.host_budget = int(hc["host_budget"])

    def next_lane(self, b, cfg) -> str | None:
        """Another lane for a batch that already runs: under max_lanes, once its base is
        measured (the first run measures it), and when a plan no live run holds is ready."""
        if cfg.max_lanes <= 1:
            return None
        n = self.s.one("SELECT COUNT(*) n FROM runs WHERE batch_id = ? AND status IN ('starting', "
                       "'running')", b["id"])["n"]
        if n >= cfg.max_lanes or not self.s.one(
                "SELECT 1 FROM base_measurements WHERE batch_id = ? LIMIT 1", b["id"]):
            return None
        if self.s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status IN ('starting', 'running') "
                      "AND plan_id IS NULL", b["id"]):
            return None  # a plain run is shipping or blocking the batch
        ready = ready_plans(self.s, b["id"])
        return ready[0]["id"] if ready else None

    def reserved_for_others(self, project_id: str) -> int:
        """Runs other projects reserve and are not using now (R5)."""
        total = 0
        for p in self.s.all("SELECT id FROM projects WHERE id != ?", project_id):
            want = batches.project_config(self.s, p["id"]).reserve
            if want:
                used = self.s.one("SELECT COUNT(*) n FROM runs r JOIN batches b ON b.id = r.batch_id "
                                  "WHERE r.status IN ('starting', 'running') AND b.project_id = ?",
                                  p["id"])["n"]
                total += max(0, want - used)
        return total

    def spawn(self, b, plan_id: str | None = None) -> str:
        if b["status"] == "queued":
            self.s.transition("batches", b["id"], "active", actor="supervisor", reason="admitted",
                              expect_from={"queued"})
        rid = new_id("r")
        self.s.insert("runs", {"id": rid, "batch_id": b["id"], "status": "starting", "plan_id": plan_id,
                               "created_at": clock.wall(), "spawned_mono": clock.mono(),
                               "boot_time": clock.boot_time()})
        log = paths.run_dir(rid)
        log.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen([*self.drive_cmd, rid], stdout=(log / "driver.out").open("w"),
                             stderr=subprocess.STDOUT, start_new_session=True,
                             env=procs.host_env() | {k: v for k, v in os.environ.items()
                                                     if k.startswith("ILK2_")})
        self.children[rid] = p
        self.s.update("runs", rid, {"pid": p.pid, "pgid": p.pid,
                                    "proc_start": procs.start_time(p.pid)},
                      actor="supervisor", reason="spawned")
        self.s.event("run.spawned", {"run": rid, "pid": p.pid}, batch_id=b["id"])
        return rid

    # -- forever ------------------------------------------------------------------------------

    def run_forever(self, stop_after: float | None = None) -> None:
        self.start()
        stop = {"flag": False}
        signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
        t_end = clock.mono() + stop_after if stop_after else None
        while not stop["flag"] and not self.exit_requested and (t_end is None or clock.mono() < t_end):
            self.tick()
            time.sleep(self.b.tick)
        for rid, p in self.children.items():
            run = self.s.get("runs", rid)
            if run["status"] != "ended":
                self.kill_run(run)
                self.end_and_requeue(run, "lost (supervisor stopped)", count=False)


def loaded_code() -> dict[str, str]:
    """sha256 of every ilk2 module this process has loaded, by path inside the package."""
    import hashlib
    import ilk2
    pkg = Path(ilk2.__file__).parent
    out = {}
    for m in list(sys.modules.values()):
        f = getattr(m, "__file__", None)
        if f and Path(f).is_relative_to(pkg) and f.endswith(".py"):
            out[str(Path(f).relative_to(pkg))] = hashlib.sha256(Path(f).read_bytes()).hexdigest()
    return out


def host_config() -> dict:
    """~/.ilk2/host.json: {"host_budget": 2, "load_check": true}. Per-host settings (Q15)."""
    p = paths.home() / "host.json"
    return json.loads(p.read_text()) if p.exists() else {}


def main(stop_after: float | None = None) -> int:
    paths.ensure()
    s = Store(paths.db())
    hc = host_config()
    try:
        Supervisor(s, host_budget=int(hc.get("host_budget", HOST_BUDGET)),
                   load_check=bool(hc.get("load_check", True))).run_forever(stop_after)
    finally:
        s.close()
    return 0


def _finish_cancel(s, batch_id, b=None):
    from .driver import finish_cancel  # lazy: keeps driver code out of the supervisor's start
    return finish_cancel(s, batch_id, b)
