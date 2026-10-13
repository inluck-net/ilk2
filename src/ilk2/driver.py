"""The driver: owns one run of one batch (docs/02 §2.2, §2.4, §2.5, §2.11).

Started by the supervisor as `ilk2 drive <run_id>`, in its own process group. It writes a
phase event at the start and end of every phase; the supervisor's stall checks read them.
Only the driver changes batch, plan and step state (I1).
"""

from __future__ import annotations

import json
import os
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, replace
from fnmatch import fnmatch
from pathlib import Path

from . import batches, bounds, clock, faults, gates, gitops, lease, paths, procs, worker
from .gates import vitest
from .gates.env import pinned_env
from .plans import Gate, v1_documents
from .runs import (NOT_A_RESTART, Stop, counts_as_restart, fenced, has_unfinished_cancel,  # noqa: F401
                   is_root_plan, lane_names, root_in_use_by_other)
from .store import Conflict, Store, StoreBusy, dumps, new_id

LOCKFILES = {"package-lock.json", "bun.lockb", "bun.lock", "yarn.lock", "pnpm-lock.yaml",
             "uv.lock", "poetry.lock", "Pipfile.lock"}


# Run endings that are not a failure of the run: they never consume a restart (§2.10).
# Anything else (error, store-busy, lease, a crash) does, so a driver that fails the same
# way every time reaches blocked (crash-loop) instead of respawning forever (Codex M1 review #3).
SETUP_DONE = ".ilk2-setup-done"  # in a diagnostic worktree: its setup hook completed
KEPT_NOTE = ("The worktree still holds the previous attempt's changes (the driver restarted "
             "between attempts and kept them). Start from them: check what they do against the "
             "evidence above and finish the step rather than starting over.")


VERIFY_NOTE = ("This is a verification step. The driver has already run its gates (the full suite) "
               "at the batch head; the failures it found are in the evidence below, compared with the "
               "batch base. There is no separate verification record or recorder to read: V1's "
               "verification records and scripts are not part of this run. Fix what the evidence "
               "names, or change nothing if it names nothing.\n\n")


class Driver:
    def __init__(self, s: Store, run_id: str, b: bounds.Bounds | None = None):
        self.s = s
        self.run_id = run_id
        self.b = b or bounds.load()
        run = s.get("runs", run_id)
        self.batch_id = run["batch_id"]
        batch = s.get("batches", self.batch_id)
        self.project_id = batch["project_id"]
        self.cfg = batches.project_config(s, self.project_id)
        self.repo = Path(s.get("projects", self.project_id)["repo_root"])
        self.root = Path(batch["execution_root"])
        self.branch = batch["branch"]
        self.base = batch["base_commit"]
        # Lanes (docs/02 §2.4.2a): a run bound to a plan. A work plan under max_lanes > 1 runs
        # in its own worktree and lane branch; repair and verification plans run in the batch
        # root. batch_root/batch_branch always name the batch's own.
        self.batch_root, self.batch_branch = self.root, self.branch
        self.plan_id = run["plan_id"] if "plan_id" in run.keys() else None
        self.plan = s.get("plans", self.plan_id) if self.plan_id else None
        self.lane = bool(self.plan) and not is_root_plan(self.plan) and self.cfg.max_lanes > 1
        if self.lane:
            self.root, self.branch = lane_names(batch, self.plan["slug"])
        self.dir = paths.run_dir(run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.api_spent = 0.0
        self.spec_changed: set[str] = set()  # gates whose spec this run's steps changed
        self._env_n = 0

    # -- phases and events --------------------------------------------------------------

    @contextmanager
    def phase(self, name: str, **payload):
        self.s.update("runs", self.run_id, {"phase": name, "phase_started_mono": clock.mono()},
                      actor="driver", reason=f"phase {name}")
        self.s.event("phase.start", {"phase": name, **payload}, batch_id=self.batch_id,
                     run_id=self.run_id)
        faults.hit(f"phase-{name}")
        yield
        self.s.event("phase.end", {"phase": name, **payload}, batch_id=self.batch_id,
                     run_id=self.run_id)

    def event(self, kind: str, **payload) -> None:
        self.s.event(kind, payload, batch_id=self.batch_id, run_id=self.run_id)

    # -- entry --------------------------------------------------------------------------

    def run(self) -> str:
        reason = "error"
        try:
            self.s.transition("runs", self.run_id, "running", actor="driver", reason="started",
                              expect_from={"starting"},
                              fields={"pid": os.getpid(), "pgid": os.getpgid(0),
                                      "proc_start": procs.start_time(os.getpid()),
                                      "boot_time": clock.boot_time()})
            self.take_root()
            with self.phase("recover"):
                self.recover_accepting()
            self.activate()
            reason = self.loop()
        except Stop as e:
            reason = e.reason
            self.event("run.stop", reason=e.reason, detail=e.detail)
            if reason == "cancelled":
                self.finish_cancel()
            elif reason == "parked":  # §2.4.2: the driver releases at these states
                row = lease.get(self.s, str(self.batch_root))
                if row and row["holder"] == "driver" and not self.s.one(
                        "SELECT 1 FROM steps WHERE batch_id = ? AND status = 'accepting'", self.batch_id):
                    lease.driver_release(self.s, str(self.batch_root),
                                         gitops.branch_head(self.batch_root, self.batch_branch))
        except StoreBusy:
            reason = "store-busy"
        except Exception:  # noqa: BLE001 - recorded, the supervisor requeues
            reason = "error"
            (self.dir / "driver-error.txt").write_text(traceback.format_exc())
        self.release_lock()
        try:
            self.s.transition("runs", self.run_id, "ended", actor="driver", reason=reason,
                              fields={"terminal_reason": reason, "ended_at": clock.wall()})
        except Exception:  # noqa: BLE001 - the supervisor reconciles a run left open
            pass
        return reason

    def deny_paths(self) -> list[str]:
        snap = json.loads(self.s.get("batches", self.batch_id)["snapshot"])
        return list(self.cfg.deny_paths) + list(snap.get("deny_paths") or [])

    def take_root(self) -> None:
        """Hold the batch root's lease and stand on the batch branch (R4); a lane then stands
        in its own worktree, and a repair or verification plan's run takes the batch's merge
        lock for its whole run (nothing merges under it)."""
        self.refresh_remote()
        self._take_batch_root()
        if self.lane:
            self.make_lane()
        elif self.plan_id:
            self.take_lock()

    def refresh_remote(self) -> None:
        """A shadow project's remote refs are refreshed at every run start, outside the
        sandbox: a worker cannot fetch into the shared .git, and a batch runs for hours."""
        self.refreshed_at = None
        if not self.cfg.shadow_of:
            return
        gitops.git(self.repo, "fetch", "-q", "--prune", "origin", check=False)
        self.refreshed_at = time.strftime("%Y-%m-%d %H:%M %Z")
        self.event("remote.refreshed", at=self.refreshed_at)

    def _take_batch_root(self) -> None:
        """A root a consumer holds is waited for behind a refusal row that the supervisor
        clears on release."""
        root, branch = self.batch_root, self.batch_branch
        row = lease.get(self.s, str(root))
        if row and row["holder"] in ("consumer", "consumer-expired"):
            self.s.refuse(self.batch_id, "root-lease", f"root {self.root} is held by "
                          f"{row['holder']} {row['owner']}", "cleared by the supervisor once the "
                          "consumer releases the root", "supervisor")
            raise Stop("waiting-lease")
        if row and row["holder"] == "released":
            lease.driver_acquire(self.s, str(root), row["generation"])
        cur = gitops.git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
        if cur != branch:
            if gitops.branch_head(root, branch) is None:
                gitops.git(root, "checkout", "-q", "-b", branch, self.base)
            else:
                gitops.git(root, "checkout", "-q", branch)

    # -- lanes (docs/02 §2.4.2a) ------------------------------------------------------------

    def make_lane(self) -> None:
        """The lane's worktree, cut from the batch head when the lane first starts; reused
        (with the step's kept work) when the plan's run is requeued."""
        if (self.root / ".git").exists():
            return
        if gitops.branch_head(self.repo, self.branch):
            self.root.parent.mkdir(parents=True, exist_ok=True)
            gitops.git(self.repo, "worktree", "add", "-q", str(self.root), self.branch)
        else:
            head = gitops.branch_head(self.batch_root, self.batch_branch)
            gitops.add_worktree(self.repo, self.root, head, branch=self.branch)
        self.event("lane.started", plan=self.plan["slug"], root=str(self.root))
        if self.cfg.setup:
            self.setup(self.root)

    def lock_key(self) -> str:
        return f"merge-lock:{self.batch_id}"

    def take_lock(self) -> None:
        """The batch's merge lock: one merge, or one run working in the batch root, at a time.
        A lock whose owner run has ended is free."""
        while True:
            with self.s.tx() as c:
                row = c.execute("SELECT value FROM meta WHERE key = ?", (self.lock_key(),)).fetchone()
                owner = row["value"] if row else None
                live = owner and owner != self.run_id and c.execute(
                    "SELECT 1 FROM runs WHERE id = ? AND status IN ('starting', 'running')",
                    (owner,)).fetchone()
                if not live:
                    c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE "
                              "SET value = excluded.value", (self.lock_key(), self.run_id))
                    return
            self.check_control()
            time.sleep(min(5.0, self.b.tick))

    def release_lock(self) -> None:
        try:
            with self.s.tx() as c:
                c.execute("DELETE FROM meta WHERE key = ? AND value = ?", (self.lock_key(), self.run_id))
        except Exception:  # noqa: BLE001 - an ended run's lock counts as free anyway
            pass

    def merge_lane(self) -> None:
        """Merge the lane into the batch branch under the merge lock, re-gate the plan's gates
        on the merged head, and ship the plan. A conflict or a red re-gate adds a repair plan
        on the batch branch, which runs before anything else (§2.5.7)."""
        self.take_lock()
        root, branch, slug = self.batch_root, self.batch_branch, self.plan["slug"]
        with self.phase("merge", plan=slug):
            if gitops.git(root, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).strip():
                gitops.git(root, "merge", "--abort", check=False)  # a merge a crash left behind
            parent = gitops.branch_head(root, branch)
            lane_head = gitops.branch_head(self.repo, self.branch)
            done = gitops.is_ancestor(root, lane_head, parent)  # merged before a crash: re-gate only
            repair = None
            if not done:
                out = gitops.git(root, *gitops.NO_HOOKS, "merge", "--no-ff", "--no-edit", "-m",
                                 f"Merge plan {slug} (ilk2 lane)", self.branch,
                                 env=gitops.IDENT, check=False)
                if not gitops.is_ancestor(root, lane_head, gitops.branch_head(root, branch)):
                    files = gitops.git(root, "diff", "--name-only", "--diff-filter=U", check=False).split()
                    gitops.git(root, "merge", "--abort", check=False)
                    repair = ("conflict", self.conflict_brief(files, parent, lane_head, out))
            if repair is None:
                red = self.regate(parent)
                if red:
                    repair = ("red", red)
            if repair:
                self.add_merge_repair(*repair)
            head = gitops.branch_head(root, branch)
            self.s.transition("plans", self.plan_id, "shipped", actor="driver",
                              reason="lane merged" if repair is None else f"lane merged; repair: {repair[0]}")
            lease.set_accepted_head(self.s, str(root), head)
            self.event("lane.merged", plan=slug, head=head, repair=repair[0] if repair else None)
        gitops.remove_worktree(self.repo, self.root)

    def conflict_brief(self, files: list[str], parent: str, lane_head: str, out: str) -> str:
        return (f"Plan {self.plan['slug']} was built in its own lane (branch {self.branch}) from an "
                f"earlier batch head, and merging it into the batch branch conflicts in: "
                f"{', '.join(files[:30]) or '(see output)'}.\n\nApply the plan's changes onto the "
                f"current batch head, keeping what the other plans changed. See them with "
                f"`git diff $(git merge-base {parent} {lane_head}) {lane_head}`. The plan's own "
                f"instructions and gates still apply.\n\nMerge output:\n{out[-2000:]}")

    def regate(self, parent: str) -> str | None:
        """The plan's gates on the merged batch head, against the batch base."""
        seen, reds = set(), []
        for r in self.s.all("SELECT gates FROM steps WHERE plan_id = ? ORDER BY idx", self.plan_id):
            for g in json.loads(r["gates"]):
                key = json.dumps(g, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                gate = Gate(**g)
                env = self.env()
                spec = gates.spec_version(gate, self.batch_root, env)
                res = gates.measure(gate, self.batch_root, env, self.dir / "gates", spec)
                base = {} if gate.adapter == "shell" else self.base_for(gate, spec)
                v = gates.judge(res, base, [], obligations=False)
                if v.outcome == "unmeasured":
                    reds.append(f"Gate `{gate.name}` could not be measured on the merged head: {res.reason}")
                elif v.outcome == "fail":
                    reds.append(f"Gate `{gate.name}` failed on the merged head: `{' '.join(gate.command)}`\n"
                                + "\n".join(f"- {tid}" for tid in v.new_failures[:30])
                                + "\n\nOutput tail:\n" + res.tail[-2000:])
        return "\n\n".join(reds) if reds else None

    def add_merge_repair(self, kind: str, brief: str) -> None:
        last = self.s.one("SELECT id FROM steps WHERE plan_id = ? ORDER BY idx DESC LIMIT 1",
                          self.plan_id)["id"]
        gl, seen = [], set()
        for r in self.s.all("SELECT gates FROM steps WHERE plan_id = ? ORDER BY idx", self.plan_id):
            for g in json.loads(r["gates"]):
                if json.dumps(g, sort_keys=True) not in seen:
                    seen.add(json.dumps(g, sort_keys=True))
                    gl.append(g)
        slug = f"merge-{self.plan['slug']}"
        if self.s.one("SELECT 1 FROM plans WHERE batch_id = ? AND slug = ?", self.batch_id, slug):
            return  # added before a crash: the retry joins it
        b = self.s.get("batches", self.batch_id)
        batches.amend(self.s, self.batch_id, [{
            "op": "add_plan", "slug": slug, "repair_of_step_id": last,
            "steps": [{"title": f"Repair the merge of plan {self.plan['slug']} ({kind})",
                       "instructions": brief, "gates": gl}]}],
            expect_version=b["version"], actor="driver", reason=f"lane-merge-{kind}")
        self.event("repair.added", plan=slug, repair_of=last, repair_kind=f"merge-{kind}")

    # -- activation: setup and base measurement (§2.5.3, §2.11.2) -------------------------

    def activate(self) -> None:
        done = self.s.one("SELECT 1 FROM base_measurements WHERE batch_id = ? LIMIT 1", self.batch_id)
        if done:
            return
        if self.cfg.setup:
            with self.phase("setup"):
                diag = self.diag_worktree(self.base)  # runs setup in it from the base's lockfile
                self.setup(self.root)
        else:
            diag = self.diag_worktree(self.base)
        with self.phase("base"):
            for gate in self.all_gates():
                self.measure_base(gate, diag, self.base)

    def setup(self, where: Path) -> None:
        cmd, cap = self.cfg.setup["command"], self.cfg.setup.get("cap_s", self.b.setup_cap)
        g = Gate(name="setup", adapter="shell", command=cmd, cap_s=int(cap), declares_tests=False)
        for i in range(2):
            rc, out, capped, _ = gates._run(cmd, where, self.env(), g.cap_s)
            if rc == 0:
                return
            transient = capped or (rc is not None and rc < 0) or "ETIMEDOUT" in out \
                or "network" in out.lower() or "ECONNRESET" in out
            if not transient or i == 1:
                self.block_batch("setup", f"setup failed (exit {rc}): {out[-1500:]}",
                                 "fix the setup, then: ilk2 resume " + self.batch_id)

    def all_gates(self) -> list[Gate]:
        seen, out = set(), []
        for r in self.s.all("SELECT gates FROM steps WHERE batch_id = ? ORDER BY plan_id, idx",
                            self.batch_id):
            for g in json.loads(r["gates"]):
                key = json.dumps(g, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    out.append(Gate(**g))
        return out

    def measure_base(self, gate: Gate, where: Path, commit: str) -> dict[str, str] | None:
        env = self.env()
        spec = gates.spec_version(gate, where, env)
        row = self.s.one("SELECT * FROM base_measurements WHERE batch_id = ? AND gate = ? AND "
                         "spec_version = ? AND commit_sha = ?", self.batch_id, gate.name, spec, commit)
        if row:
            return json.loads(row["failures"])
        r = gates.measure(gate, where, env, self.dir / "gates", spec)
        if r.outcome == "unmeasured":
            self.block_batch("base-unmeasured",
                             f"gate {gate.name} cannot be measured at {commit[:12]}: {r.reason}",
                             f"fix the gate, then: ilk2 resume {self.batch_id}")
        # A shell gate red at the base is recorded, not blocking: it is judged absolutely after
        # its step (run_gates), never against this base (§2.5.1, revised Oct 13).
        failures = r.failure_map()
        self.s.insert("base_measurements", {"batch_id": self.batch_id, "gate": gate.name,
                                            "spec_version": spec, "commit_sha": commit,
                                            "outcome": r.outcome, "failures": dumps(failures),
                                            "at": clock.wall()})
        return failures

    def diag_worktree(self, commit: str) -> Path:
        p = paths.diag(self.batch_id) / commit[:12]
        if not (p / ".git").exists():
            gitops.add_worktree(self.repo, p, commit)
        if self.cfg.setup and not (p / SETUP_DONE).exists():
            # §2.5.7, A5: each diagnostic worktree installs from its own commit's lockfile,
            # so old source is never tested against later dependencies. Keyed on completion,
            # not creation: a run that died mid-setup leaves no marker (kira, Oct 13).
            self.setup(p)
            (p / SETUP_DONE).write_text(f"{clock.wall():.0f}\n")
        return p

    def env(self) -> dict[str, str]:
        self._env_n += 1
        return pinned_env(home=self.dir / "home" / str(self._env_n), tmp=self.dir / "tmp",
                          path_prelude=self.cfg.path_prelude, allow=self.cfg.env_allow,
                          set_vars=self.cfg.env_set, home_files=self.cfg.home_files)

    # -- the batch loop -----------------------------------------------------------------

    def loop(self) -> str:
        while True:
            self.check_control()
            step = self.next_step()
            if step == "lane-merge":
                self.merge_lane()
                step = "plan-done"
            if step == "plan-done":
                if all(p["status"] == "shipped" for p in self.s.all(
                        "SELECT status FROM plans WHERE batch_id = ?", self.batch_id)):
                    self.ship_batch()
                    return "shipped"
                return "plan-done"
            if step == "plan-blocked":
                return "plan-blocked"
            if step == "shipped":
                self.ship_batch()
                return "shipped"
            if step == "blocked":
                self.finish_blocked()
                return "blocked"
            self.run_step(step)

    def check_control(self) -> None:
        b = self.s.get("batches", self.batch_id)
        if b["status"] == "parked":
            raise Stop("parked")
        if b["status"] == "cancelled":
            raise Stop("cancelled")
        if self.s.one("SELECT value FROM meta WHERE key = 'draining' AND value = '1'"):
            raise Stop("drained")

    def next_step(self):
        if self.plan_id:  # a run bound to one plan (lanes)
            plan = self.s.get("plans", self.plan_id)
            if plan["status"] == "shipped":
                return "plan-done"
            if plan["status"] == "blocked":
                return "plan-blocked"
            step = self.s.one("SELECT * FROM steps WHERE plan_id = ? AND status != 'accepted' "
                              "ORDER BY idx LIMIT 1", self.plan_id)
            if step is None:
                if self.lane:
                    return "lane-merge"
                self.s.transition("plans", self.plan_id, "shipped", actor="driver",
                                  reason="all steps accepted")
                return "plan-done"
            return "plan-blocked" if step["status"] == "blocked" else step
        plans = self.s.all("SELECT * FROM plans WHERE batch_id = ? ORDER BY ord", self.batch_id)
        status = {p["slug"]: p["status"] for p in plans}
        repairs = [p for p in plans if p["repair_of_step_id"] and p["status"] != "shipped"]
        if repairs:
            # §2.5.7: a repair plan is a batch barrier. No other plan starts a new step until
            # every repair has shipped; a blocked repair blocks the batch.
            for p in repairs:
                step = self.s.one("SELECT * FROM steps WHERE plan_id = ? AND status != 'accepted' "
                                  "ORDER BY idx LIMIT 1", p["id"])
                if step is None:
                    self.s.transition("plans", p["id"], "shipped", actor="driver",
                                      reason="all steps accepted")
                    return self.next_step()
                if step["status"] != "blocked" and p["status"] != "blocked":
                    return step
            return "blocked"
        for p in plans:
            if p["status"] in ("shipped", "blocked"):
                continue
            deps = json.loads(p["depends_on"])
            bad = [d for d in deps if status.get(d) == "blocked"]
            if bad:
                self.s.transition("plans", p["id"], "blocked", actor="driver",
                                  reason=f"dependency {bad[0]} blocked",
                                  fields={"blocked_reason": f"dependency: {bad[0]}"})
                status[p["slug"]] = "blocked"
                # No needs_human row of its own: it clears when the blocking plan clears, and
                # that plan's row already names the human action (§2.11.1, S7).
                continue
            if any(status.get(d) != "shipped" for d in deps):
                continue
            step = self.s.one("SELECT * FROM steps WHERE plan_id = ? AND status != 'accepted' "
                              "ORDER BY idx LIMIT 1", p["id"])
            if step is None:
                self.s.transition("plans", p["id"], "shipped", actor="driver", reason="all steps accepted")
                status[p["slug"]] = "shipped"
                continue
            if step["status"] == "blocked":
                continue
            return step
        if all(v == "shipped" for v in status.values()):
            return "shipped"
        return "blocked"

    def ship_batch(self) -> None:
        head = gitops.branch_head(self.batch_root, self.batch_branch)
        self.s.transition("batches", self.batch_id, "shipped", actor="driver", reason="all plans shipped",
                          expect_from={"active"}, fields={"terminal_reason": "shipped"})
        lease.driver_release(self.s, str(self.batch_root), head)
        self.cleanup_diag()
        self.event("batch.shipped", head=head)

    def finish_blocked(self) -> None:
        blocked = [f"{p['slug']}: {p['blocked_reason']}" for p in
                   self.s.all("SELECT * FROM plans WHERE batch_id = ? AND status = 'blocked'",
                              self.batch_id)]
        self.s.transition("batches", self.batch_id, "blocked", actor="driver",
                          reason="nothing runnable", expect_from={"active"},
                          fields={"blocked_reason": "; ".join(blocked)})
        head = gitops.branch_head(self.batch_root, self.batch_branch)
        lease.driver_release(self.s, str(self.batch_root), head)  # batch.blocked: emitted by the store

    def block_batch(self, reason: str, detail: str, clear: str, release: bool = True) -> None:
        self.s.transition("batches", self.batch_id, "blocked", actor="driver", reason=reason,
                          fields={"blocked_reason": f"{reason}: {detail[:500]}"})
        self.s.open_needs_human(reason, detail[:2000], clear, batch_id=self.batch_id)
        if not release:  # the root stays fenced: survivors may still be writing to it
            raise Stop(f"blocked ({reason})", detail[:300])
        head = gitops.branch_head(self.batch_root, self.batch_branch)
        try:
            lease.driver_release(self.s, str(self.batch_root), head)
        except Conflict:
            pass
        raise Stop(f"blocked ({reason})", detail[:300])

    def cleanup_diag(self) -> None:
        d = paths.diag(self.batch_id)
        if d.exists():
            for w in d.iterdir():
                gitops.remove_worktree(self.repo, w)

    # -- one step -----------------------------------------------------------------------

    def run_step(self, step) -> None:
        plan = self.s.get("plans", step["plan_id"])
        if plan["status"] == "pending":
            self.s.transition("plans", plan["id"], "active", actor="driver", reason="first step")
        if step["restart_count"] >= bounds.RESTARTS_BEFORE_HUMAN:
            self.block_step(step, "crash-loop", f"{step['restart_count']} restarts of this step",
                            self.restart_reasons(step["id"]))
            return
        gate_list = [Gate(**g) for g in json.loads(step["gates"])]
        expect_red = json.loads(step["expected_reds"])
        must_pass = json.loads(step["must_pass"])
        scope = json.loads(step["scope"])
        kept = False
        with self.phase("prepare", step=step["id"]):
            head = gitops.branch_head(self.root, self.branch)
            if gitops.changed_paths(self.root):
                (self.dir / f"leftover-{step['id']}.diff").write_text(gitops.diff_text(self.root))
                kept = self.keeps_leftover(step, head)
                if kept:
                    self.event("worktree.kept", step=step["id"],
                               paths=gitops.changed_paths(self.root)[:50])
                else:
                    gitops.reset_hard(self.root, head)
            parent = head
        n = step["attempt_count"]
        last = self.s.one("SELECT evidence FROM attempts WHERE step_id = ? AND evidence IS NOT NULL "
                          "ORDER BY created_at DESC LIMIT 1", step["id"])
        evidence = last["evidence"] if last else None
        if kept:
            evidence = ((evidence + "\n\n") if evidence else "") + KEPT_NOTE
        session = None
        limit = bounds.ATTEMPTS_PER_STEP + bounds.ESCALATION_ATTEMPTS
        # Kept work is gated too: it may already be green, and otherwise the worker gets the
        # failures as they stand now, not the last attempt's (gh-resolve P5 batch, Oct 14).
        if plan["verification"] and n < limit:
            # A verification step checks the batch head: its gates run first, and a worker is
            # dispatched only when they are red, with their failures as its brief. gh-resolve P5
            # batch, Oct 14: a worker spent 18 min trying to run and record the full suite that
            # is this step's own gate.
            # Every (re)entry, not only the first: after repairs or a restart the last evidence
            # is stale (gh-resolve P5 batch, Oct 14: a worker chased failures the repairs had
            # already fixed).
            n += 1
            aid = new_id("a")
            self.s.insert("attempts", {"id": aid, "run_id": self.run_id, "step_id": step["id"],
                                       "n": n, "status": "running", "parent_commit": parent,
                                       "created_at": clock.wall()})
            self.s.update("steps", step["id"], {"attempt_count": n}, actor="driver",
                          reason="gates first (verification)")
            with self.phase("gates", step=step["id"]):
                red, verdicts = self.run_gates(step, aid, gate_list, expect_red, parent, must_pass)
            if red is None:
                with self.phase("accept", step=step["id"]):
                    self.accept(step, aid, parent)
                self.end_attempt(aid, "accepted", "gates green; no worker needed", {},
                                 gates_out=verdicts)
                return
            evidence = red + ("\n\n" + KEPT_NOTE if kept else "")
            self.end_attempt(aid, "red", red[:300], {}, evidence=red, gates_out=verdicts)
            # A gates-first run is a measurement, not work: it never takes a worker's attempt
            # (gh-resolve P5 batch, Oct 14: it took the 5th and blocked the step unseen).
            n -= 1
            self.s.update("steps", step["id"], {"attempt_count": n}, actor="driver",
                          reason="gates-first red: not counted")
        while n < limit:
            self.check_control()
            n += 1
            aid = new_id("a")
            self.s.insert("attempts", {"id": aid, "run_id": self.run_id, "step_id": step["id"],
                                       "n": n, "status": "running", "parent_commit": parent,
                                       "created_at": clock.wall()})
            self.s.update("steps", step["id"], {"attempt_count": n}, actor="driver",
                          reason=f"attempt {n}")
            if n == bounds.ATTEMPTS_PER_STEP + 1:
                evidence = self.all_evidence(step["id"])  # the escalation round's brief
                session = None
            outcome, summary, session = self.dispatch(step, aid, n, gate_list, evidence, session)
            if outcome in ("api", "quota"):
                n -= 1
                self.s.update("steps", step["id"], {"attempt_count": n}, actor="driver",
                              reason=f"{outcome} error, not counted")
                self.end_attempt(aid, outcome, f"{outcome} error (not counted)", summary)
                self.api_backoff(outcome)
                continue
            red = None
            if not plan["verification"] and \
                    gitops.worktree_tree(self.root) == gitops.commit_tree_of(self.root, parent):
                # (a verification plan's step may change nothing: it checks the batch head)
                red = ("The attempt made no change to any file. The step needs a change that makes its "
                       "gates pass; nothing was committed.")
            unfinished = None if outcome == "done" else (
                f"The worker's session did not finish ({outcome}"
                f"{', exit ' + str(summary.get('returncode')) if outcome == 'crashed' else ''}). "
                "Its work so far is kept in the worktree; finish the step.")
            with self.phase("scope", step=step["id"]):
                deny = self.deny_paths()
                denied = [p for p in gitops.changed_paths(self.root) if batches.path_matches(p, deny)]
                out_of_scope = self.out_of_scope(scope)
                if out_of_scope and not self.cfg.enforce_scope:
                    self.event("scope.advisory", step=step["id"], outside=out_of_scope[:50])
                    out_of_scope = []
                mirrored = self.mirrored_copy()
                if mirrored and red is None:
                    red = (f"The attempt copied the repository into the worktree: {len(mirrored)} added "
                           f"files repeat tracked paths under a new directory (e.g. {mirrored[0]}). "
                           "Remove the copy; keep scratch copies in $TMPDIR, outside the worktree.")
                if denied and red is None:
                    # docs/06 R8: never accepted, whatever the step's scope says
                    red = ("Changes to denied paths (deny_paths: the project's plus this batch's); "
                           "revert them: " + ", ".join(denied[:20]))
                elif out_of_scope and red is None:
                    red = "Changes outside the step's declared scope: " + ", ".join(out_of_scope[:20])
            verdicts: dict = {}
            if red is None:
                with self.phase("gates", step=step["id"]):
                    red, verdicts = self.run_gates(step, aid, gate_list, expect_red, parent,
                                                   must_pass)
            if unfinished:
                # Never accepted, even with green gates (Codex M1 review #2); the gate output
                # is still the evidence for the next attempt.
                red = unfinished + ("\n\n" + red if red else "")
            if red is None:
                with self.phase("accept", step=step["id"]):
                    self.accept(step, aid, parent)
                self.end_attempt(aid, "accepted", "", summary, gates_out=verdicts)
                return
            evidence = red
            self.end_attempt(aid, "red", red[:300], summary, evidence=red, gates_out=verdicts)
        self.block_step(step, "attempts", f"{limit} attempts red (3, then an escalation round)",
                        evidence or "")

    def keeps_leftover(self, step, head: str) -> bool:
        """Changes left in the root by an earlier run are kept when they are this step's own
        work: the root is at the branch head, and the step's last attempt ended without being
        accepted. Within a run the next attempt continues from the worktree; a new run (resume,
        upgrade drain, crash recovery) used to reset it. gh-resolve P5 batch, Oct 13: a 554-line
        diff, red only on failures inherited from the base, was discarded by an upgrade drain."""
        if gitops.head(self.root) != head:
            return False
        last = self.s.one("SELECT outcome FROM attempts WHERE step_id = ? AND outcome IS NOT NULL "
                          "ORDER BY created_at DESC LIMIT 1", step["id"])
        return bool(last) and last["outcome"] in ("red", "interrupted", "timeout", "crashed")

    def carried_session(self, step) -> str | None:
        """The session of the previous step of the same plan, if that step was accepted and its
        session stayed under carry_max_context (docs/02 §2.6: the step is still the bound
        for a fresh start; carrying is an optimisation that falls back to a fresh session)."""
        if step["idx"] == 0:
            return None
        row = self.s.one("SELECT a.session_id, a.timings FROM attempts a JOIN steps st ON "
                         "st.id = a.step_id WHERE st.plan_id = ? AND st.idx = ? AND "
                         "a.outcome = 'accepted' ORDER BY a.created_at DESC LIMIT 1",
                         step["plan_id"], step["idx"] - 1)
        if not row or not row["session_id"]:
            return None
        peak = json.loads(row["timings"] or "{}").get("peak_context_tokens") or 0
        return None if peak > self.cfg.carry_max_context else row["session_id"]

    def all_evidence(self, step_id: str) -> str:
        """Every red attempt's evidence for the escalation round: a fresh session sees what each
        earlier attempt tried and what stayed red, not only the last one."""
        rows = self.s.all("SELECT n, outcome, evidence FROM attempts WHERE step_id = ? AND "
                          "evidence IS NOT NULL ORDER BY n", step_id)
        parts = [f"Attempt {r['n']} ({r['outcome']}):\n{r['evidence'][-6000:]}" for r in rows]
        return ("Earlier attempts at this step were all red. Read what each one left red before "
                "you start; do not repeat an approach that already failed.\n\n" + "\n\n".join(parts))

    def restart_reasons(self, step_id: str) -> str:
        rows = self.s.all("SELECT a.outcome, a.reason FROM attempts a WHERE a.step_id = ? "
                          "ORDER BY a.created_at DESC LIMIT 3", step_id)
        return "; ".join(f"{r['outcome']}: {r['reason']}" for r in rows)

    def end_attempt(self, aid: str, outcome: str, reason: str, summary: dict, *,
                    evidence: str | None = None, gates_out: dict | None = None) -> None:
        self.s.transition("attempts", aid, "done", actor="driver", reason=outcome,
                          fields={"outcome": outcome, "reason": reason,
                                  "timings": dumps(summary | {"gates": gates_out or {}}),
                                  "session_id": summary.get("session_id"),
                                  "evidence": evidence, "ended_at": clock.wall()})
        self.event("attempt.end", attempt=aid, outcome=outcome, reason=reason[:300])

    def block_step(self, step, reason: str, detail: str, evidence: str) -> None:
        self.s.transition("steps", step["id"], "blocked", actor="driver", reason=reason,
                          fields={"blocked_reason": f"{reason}: {detail}"})
        self.s.transition("plans", step["plan_id"], "blocked", actor="driver", reason=reason,
                          fields={"blocked_reason": f"step {step['idx']}: {reason}"})
        self.s.open_needs_human(reason, f"step {step['id']}: {detail}\n{evidence[-1500:]}",
                                f"ilk2 resume {self.batch_id}", batch_id=self.batch_id)
        self.event("step.blocked", step=step["id"], reason=reason)

    # -- dispatch -----------------------------------------------------------------------

    def write_documents(self, files) -> list[str]:
        """The batch's plan documents, copied where the sandboxed worker can read them. Batches
        submitted before documents were snapshotted read them from the claimed V1 master."""
        b = self.s.get("batches", self.batch_id)
        docs = json.loads(b["snapshot"]).get("documents") or {}
        if not docs:
            master = json.loads(b["originals"] or "{}").get("v1_master")
            docs = v1_documents(Path(master)) if master else {}
        out = []
        d = files.tmp / "plan"
        for name, text in docs.items():
            p = d / Path(name).name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            out.append(str(p))
        return out

    def dispatch(self, step, aid: str, n: int, gate_list: list[Gate], evidence: str | None,
                 session: str | None) -> tuple[str, dict, str | None]:
        cfg = worker.WorkerConfig(claude_bin=self.cfg.claude_bin, max_turns=self.cfg.max_turns,
                                  sandbox=self.cfg.sandbox, sandbox_allow=self.cfg.sandbox_allow)
        resume, carried = None, False
        if n in bounds.RESUMED_ATTEMPTS and session and self.s.one(
                "SELECT 1 FROM attempts WHERE step_id = ? AND session_id = ? AND run_id = ?",
                step["id"], session, self.run_id):
            # Q6: the 2nd attempt resumes the same session. The 3rd starts a fresh one.
            resume = session
        elif n == 1 and session is None and self.cfg.carry_session:
            resume = self.carried_session(step)
            carried = resume is not None
        if resume:
            # A session's transcript lives in the config dir of the attempt that created it
            first = self.s.one("SELECT id, run_id FROM attempts WHERE session_id = ? "
                               "ORDER BY created_at LIMIT 1", resume)
            files = worker.AttemptFiles(paths.run_dir(first["run_id"]) / first["id"])
        else:
            files = worker.AttemptFiles(self.dir / aid)
        worker.prepare(cfg, files, self.root, [g.command for g in gate_list],
                       fresh_config=resume is None)
        docs = self.write_documents(files)
        if carried:
            prompt = ("The previous step of this plan is accepted and committed. Continue in this "
                      "session with the next step, same rules as before; you already have the "
                      "context you read.\n\n" + worker.brief(
                          step["title"], step["instructions"],
                          [" ".join(g.command) for g in gate_list], evidence, preload=None,
                          scope=json.loads(step["scope"]), scope_enforced=self.cfg.enforce_scope,
                          documents=docs, refreshed_at=getattr(self, "refreshed_at", None)))
        elif resume:
            prompt = "The driver's gate is red after your attempt. Fix the cause, with the same " \
                     "rules as before.\n\n" + (evidence or "")
        else:
            pre = None
            if self.cfg.preload:  # Q10
                diff = gitops.git(self.root, "diff", f"{self.base}..HEAD", check=False)[:60_000]
                pre = worker.preload(self.root, json.loads(step["scope"]), step["instructions"],
                                     [g.command for g in gate_list], diff)
            prompt = worker.brief(step["title"], step["instructions"],
                                  [" ".join(g.command) for g in gate_list], evidence, preload=pre,
                                  scope=json.loads(step["scope"]),
                                  scope_enforced=self.cfg.enforce_scope, documents=docs,
                                  refreshed_at=getattr(self, "refreshed_at", None))
        if self.s.get("plans", step["plan_id"])["verification"] and not resume:
            prompt = VERIFY_NOTE + prompt
        with self.phase("dispatch", step=step["id"], attempt=aid, n=n, resume=bool(resume),
                        carried=carried):
            d = worker.dispatch(cfg, files, self.root, prompt, resume=resume)
            outcome = d.wait(self.b.attempt_cap, self.b.term_grace, self.b.kill_grace)
        summary = d.summary()
        if d.terminated and d.terminated.get("survivors"):
            self.end_attempt(aid, "interrupted", "worker survived SIGKILL", summary)
            self.block_batch("unkillable", f"worker group {d.pgid} survived SIGKILL: "
                             f"{d.terminated['survivors']}", "kill the listed pids, then: "
                             f"ilk2 resume {self.batch_id}", release=False)
        return outcome, summary, d.session_id or session

    def api_backoff(self, kind: str) -> None:
        if kind == "quota":
            self.s.refuse(self.batch_id, "quota", "provider quota exhausted",
                          "quota reset (supervisor probes hourly)", "supervisor")
            raise Stop("quota")
        tries = getattr(self, "_api_tries", 0)
        self._api_tries = tries + 1
        wait = min(self.b.api_backoff_first * (2 ** tries), self.b.api_backoff_max)
        if self.api_spent + wait > self.b.api_budget_per_run:
            self.s.refuse(self.batch_id, "waiting-api", "model API errors for 2 h in this run",
                          "API recovers (supervisor probes every 30 min)", "supervisor")
            raise Stop("api")
        self.event("api.backoff", seconds=wait, tries=self._api_tries)
        time.sleep(wait)
        self.api_spent += wait

    # -- scope and gates ----------------------------------------------------------------

    def mirrored_copy(self) -> list[str]:
        """Added files that repeat tracked paths under a new top directory: a copy of the
        repository inside the worktree. gh-resolve P5 batch, Oct 14: a verification worker
        left 1,111 files under .base-tree/, which would have been committed. Judgment call:
        50 such files; wrong if a project vendors a copy of its own tree on purpose."""
        tracked = set(gitops.git(self.root, "ls-files", check=False).split("\n"))
        added = gitops.git(self.root, "status", "--porcelain", "-uall", check=False).splitlines()
        new = [ln[3:] for ln in added if ln[:2] in ("??", "A ", "AM")]
        mirrored = [p for p in new if "/" in p and p.split("/", 1)[1] in tracked]
        return mirrored if len(mirrored) >= 50 else []

    def out_of_scope(self, scope: list[str]) -> list[str]:
        if not scope:
            return []
        return [p for p in gitops.changed_paths(self.root) if not batches.path_matches(p, scope)]

    def run_gates(self, step, aid: str, gate_list: list[Gate], expect_red: list[str],
                  parent: str, must_pass: list[str] = ()) -> tuple[str | None, dict]:
        changed = set(gitops.changed_paths(self.root))
        if self.cfg.setup and any(Path(p).name in LOCKFILES for p in changed):
            self.setup(self.root)
        reds, verdicts, runs = [], {}, []
        for gate in gate_list:
            env = self.env()
            spec = gates.spec_version(gate, self.root, env)
            r = gates.measure(gate, self.root, env, self.dir / "gates", spec)
            for _ in range(r.timeouts):
                self.event("gate.timeout", gate=gate.name)
            base = self.base_for(gate, spec)
            runs.append(r)
            if gate.adapter == "shell":
                # Absolute: exit 0 after the step. Comparing with the base would let an
                # unchanged red (same exit, same output) pass vacuously (B5).
                base = {}
            v = gates.judge(r, base, expect_red, obligations=False)
            if r.missing_paths and gate.declares_tests and v.outcome != "unmeasured":
                # At the base an absent path is 0 tests; after a step it is a missing test
                # file the gate declares, never a silent pass (docs/06 B5).
                v.outcome = "fail"
                v.new_failures += [f"{p} (declared, absent after the step)" for p in r.missing_paths]
            inherited = []
            if v.outcome == "fail" and parent != self.base and v.new_failures:
                inherited = self.parent_probe(gate, parent, v.new_failures)
            attributions = []
            if inherited and gate.adapter in ("pytest", "vitest") and base is not None:
                attributions = self.attribute(step, gate, spec, r, inherited, parent)
                done = {a.test_id for a in attributions if a.result == "attributed"}
                v.new_failures = [t for t in v.new_failures if t not in done]
                if done and not v.new_failures:
                    v.outcome = "pass-with-attributed"
                    self.event("gate.pass-with-attributed", gate=gate.name, step=step["id"],
                               attributed=sorted(done))
            self.s.insert("gate_results", {"attempt_id": aid, "gate": gate.name,
                                           "spec_version": spec, "outcome": v.outcome,
                                           "new_failures": dumps(v.new_failures),
                                           "details": dumps({"reason": v.reason or r.reason,
                                                             "comparable_base": base is not None
                                                             and gate.name not in self.spec_changed,
                                                             "possibly_inherited": inherited,
                                                             "attribution": {a.test_id: a.to_dict()
                                                                             for a in attributions},
                                                             "duration_s": r.duration_s}),
                                           "at": clock.wall()})
            verdicts[gate.name] = {"outcome": v.outcome, "new": v.new_failures[:50],
                                   "possibly_inherited": inherited}
            if v.outcome == "unmeasured":
                self.block_step(step, "unmeasured", f"gate {gate.name}: {r.reason}", r.tail)
                raise Stop("blocked (unmeasured)", f"{gate.name}: {r.reason}")
            if v.outcome == "fail":
                lines = [f"Gate `{gate.name}` failed: `{' '.join(gate.command)}`"]
                if gate.name in self.spec_changed:
                    lines.append("(This step changed the gate's specification: failures are "
                                 "compared with the base measured under the base's own "
                                 "configuration, by test id and fingerprint.)")
                for tid in v.new_failures[:30]:
                    f = r.failures.get(tid)
                    tag = " [possibly-inherited: also failing at the step's parent]" \
                        if tid in inherited else ""
                    lines.append(f"- {tid}{tag}: {f.message if f else ''}")
                lines.append("\nOutput tail:\n" + r.tail[-2500:])
                reds.append("\n".join(lines))
        unmet, missing_reds = gates.unmet_obligations(runs, expect_red, list(must_pass))
        plan = self.s.get("plans", step["plan_id"])
        # Red-first proves a new pin can detect the defect. A repair's must_pass is the failing
        # test it must turn green, and a verification step writes no pins: neither is checked
        # (gh-resolve P5 batch, Oct 14: repair-1 edited the guard's allowlist file and was
        # judged unable to detect anything).
        if not reds and not unmet and not missing_reds and must_pass \
                and not plan["repair_of_step_id"] and not plan["verification"]:
            weak = self.red_first(gate_list, list(must_pass), parent, aid, step)
            if weak:
                verdicts["red-first"] = {"outcome": "fail", "pass_without_change": weak}
                reds.append("These must_pass tests pass without your change, so they cannot detect "
                            "the defect (run against the step's parent code with your new tests): "
                            + ", ".join(weak) + "\nMake each fail on the old code, or name only the "
                            "tests that do in must_pass.")
        if unmet or missing_reds:
            verdicts["step"] = {"outcome": "fail", "must_pass": unmet, "expected_red": missing_reds}
            reds.append("The step's own obligations are not met:\n" + "\n".join(
                [f"- {t}: must pass after this step (failing, skipped, xfailed or absent)"
                 for t in unmet] + [f"- {t}: expected red, did not fail" for t in missing_reds]))
        return ("\n\n".join(reds) if reds else None), verdicts

    def attribute(self, step, gate: Gate, spec: str, r, inherited: list[str],
                  parent: str) -> list:
        """The attribute phase (§2.5.7) for one gate's inherited failures. Attributed ones get
        a repair plan (deduplicated); the rest stay judged against the current step."""
        from .attribution import Attributor, repair_brief
        out = []
        with self.phase("attribute", step=step["id"], gate=gate.name):
            at = Attributor(self)
            for tid in inherited:
                f = r.failures[tid]
                a = at.attribute(gate, spec, tid, f.fingerprint, parent)
                out.append(a)
                self.event("attribution", test=tid, **a.to_dict())
                if a.result == "attributed":
                    self.add_repair(a, gate, repair_brief(self.s, self.root, a, gate, f.message))
        self.s.update("runs", self.run_id, {"phase": "gates", "phase_started_mono": clock.mono()},
                      actor="driver", reason="phase gates")
        return out

    def add_repair(self, a, gate: Gate, brief: str) -> None:
        clean, failing = a.edge
        row = self.s.one("SELECT plan_id FROM repairs WHERE batch_id = ? AND test_id = ? AND "
                         "fingerprint = ? AND edge_clean = ? AND edge_failing = ?", self.batch_id,
                         a.test_id, a.fingerprint, clean, failing)
        if row:  # A10: the same failure on the same edge joins the existing repair
            self.event("repair.joined", plan=row["plan_id"], test=a.test_id)
            return
        n = self.s.one("SELECT COUNT(*) n FROM plans WHERE batch_id = ? AND repair_of_step_id "
                       "IS NOT NULL", self.batch_id)["n"] + 1
        slug = f"repair-{n}"
        b = self.s.get("batches", self.batch_id)
        # The repair is judged on the test it repairs, not the whole gate: with the full suite,
        # any other open failure turned it red (gh-resolve P5 batch, Oct 14). A later full run
        # (the step's own gate, or a verification step) still catches what the repair breaks.
        if gate.adapter == "vitest":
            narrowed = replace(gate, command=vitest.with_test_ids(gate.command, [a.test_id]))
        elif gate.adapter == "pytest":
            narrowed = replace(gate, command=gates.with_test_ids(gate.command, [a.test_id],
                                                                 drop=("--deselect", "-k")))
        else:
            narrowed = gate
        batches.amend(self.s, self.batch_id, [{
            "op": "add_plan", "slug": slug, "repair_of_step_id": a.step_id,
            "steps": [{"title": f"Repair {a.test_id}", "instructions": brief,
                       "gates": [asdict(narrowed)], "must_pass": [a.test_id]}]}],
            expect_version=b["version"], actor="driver", reason="regression-attributed")
        with self.s.tx() as c:
            c.execute("INSERT INTO repairs(batch_id, test_id, fingerprint, edge_clean, edge_failing, "
                      "plan_id, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (self.batch_id, a.test_id, a.fingerprint, clean, failing,
                       f"{self.batch_id}.{slug}", clock.wall()))
        self.event("repair.added", plan=slug, test=a.test_id, repair_of=a.step_id)

    def red_first(self, gate_list: list[Gate], must_pass: list[str], parent: str,
                  aid: str, step=None) -> list[str]:
        """V1's red-first guarantee, kept inside one step (gh-resolve-e1, Oct 13): must_pass tests
        in files this step added or changed must fail on the step's parent code. They run in a
        throwaway worktree at the parent with only the new test files overlaid. An id must fail
        there; a whole file needs at least one failing test (a sensitivity control may pass).
        Returns the must_pass entries that pass without the change.

        A pin written in an earlier step of the same plan (V1 merged plans: the pins land in the
        plan's first step, "remove every xfail" comes later) is checked against the plan's
        starting commit, since that is the code it must fail on."""
        import shutil
        changed = set(gitops.changed_paths(self.root))
        start = self.plan_start(step) if step is not None else None
        if start and start != parent:
            earlier = gitops.git(self.root, "diff", "--name-only", f"{start}..{parent}",
                                 check=False).split()
            if any(t.split("::")[0] in earlier for t in must_pass):
                changed |= set(earlier)
                parent = start
        mine = [t for t in must_pass if t.split("::")[0] in changed]
        if not mine:
            return []
        files = sorted({t.split("::")[0] for t in mine})
        gate = next((g for g in gate_list if g.adapter in ("pytest", "vitest")
                     and any(f in " ".join(g.command) for f in files)), None) or \
            next((g for g in gate_list if g.adapter in ("pytest", "vitest")), None)
        if gate is None:
            return []
        where = paths.diag(self.batch_id) / f"red-first-{aid}"
        gitops.add_worktree(self.repo, where, parent)
        try:
            if self.cfg.setup:
                self.setup(where)
            for f in files:
                (where / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / f, where / f)
            ids = [t for t in mine]
            cmd = (gates.vitest.with_test_ids(gate.command, ids) if gate.adapter == "vitest"
                   else gates.with_test_ids(gate.command, ids, drop=("--deselect", "-k")))
            r = gates.measure(replace(gate, name=gate.name + "-red-first", command=cmd), where,
                              self.env(), self.dir / "gates")
            if r.outcome == "unmeasured":
                return []  # cannot run on the old code (e.g. imports the new module): not proven weak
            weak = []
            for t in mine:
                if "::" in t:
                    if t in r.passed:
                        weak.append(t)
                elif not any(k.startswith(t + "::") or k.startswith(t) for k in r.failures):
                    weak.append(t)  # every test in the file passed on the old code
            self.event("red-first", checked=mine, pass_without_change=weak)
            return weak
        finally:
            gitops.remove_worktree(self.repo, where)

    def plan_start(self, step) -> str | None:
        """The commit the plan's first attempt started from."""
        row = self.s.one("SELECT a.parent_commit FROM attempts a JOIN steps st ON st.id = a.step_id "
                         "WHERE st.plan_id = ? AND a.parent_commit IS NOT NULL ORDER BY a.created_at "
                         "LIMIT 1", step["plan_id"])
        return row["parent_commit"] if row else None

    def base_for(self, gate: Gate, spec_now: str) -> dict[str, str] | None:
        row = self.s.one("SELECT failures FROM base_measurements WHERE batch_id = ? AND gate = ? "
                         "AND spec_version = ? AND commit_sha = ?", self.batch_id, gate.name,
                         spec_now, self.base)
        if row:
            return json.loads(row["failures"])
        diag = self.diag_worktree(self.base)
        if gates.spec_version(gate, diag, self.env()) != spec_now:
            # The step changed what decides the gate (a conftest, a runner config). Judgment
            # call (Oct 14): compare with the base measured under the base's own configuration,
            # by test id and fingerprint, instead of charging every failure to the step. A
            # failure identical at the base is not new. gh-resolve P5 batch: the verification's
            # full suite could never pass, its 12 base failures counted as new. Wrong if a config
            # change can produce a new failure identical in id and fingerprint to a base one.
            self.spec_changed.add(gate.name)
        return self.measure_base(gate, diag, self.base)

    def parent_probe(self, gate: Gate, parent: str, failing: list[str]) -> list[str]:
        ids = [t for t in failing if "::" in t and not t.endswith(("<collect>", "<exit>", "<no-tests>"))]
        if not ids:
            return []
        diag = self.diag_worktree(parent)
        cmd = gates.vitest.with_test_ids(gate.command, ids) if gate.adapter == "vitest" else \
            gates.with_test_ids(gate.command, ids, drop=("--deselect", "-k"))
        probe = replace(gate, name=gate.name + "-parent-probe", command=cmd)
        r = gates.measure(probe, diag, self.env(), self.dir / "gates")
        return sorted(t for t in ids if t in r.failures)

    # -- acceptance (§2.4.1, I12) ---------------------------------------------------------

    def accept(self, step, aid: str, parent: str) -> None:
        if not lease.holder_is_driver(self.s, str(self.batch_root)):  # a lane holds its batch's
            raise Stop("lease", "the root lease is not held by the driver")
        tree = gitops.worktree_tree(self.root)
        intent = {"attempt_id": aid, "parent": parent, "tree": tree}
        self.s.transition("steps", step["id"], "accepting", actor="driver", reason="intent",
                          expect_from={"pending"}, fields={"accepting": dumps(intent)})
        faults.hit("after-intent")
        if tree == gitops.commit_tree_of(self.root, parent):
            # Nothing changed (a verification step): accepted at the parent, no empty commit.
            # A crash here recovers by undo (no trailer commit, head at the parent): re-run.
            self.finalize(step["id"], parent)
            return
        sha = gitops.find_trailer_commit(self.root, self.branch, aid)
        if sha is None:
            plan = self.s.get("plans", step["plan_id"])
            # V1's trailer too, so V1-style checks ("step 0 is committed") keep working
            msg = (f"{step['title']} [plan:{plan['slug']}#step-{step['idx']}]\n\n"
                   f"ilk2 batch {self.batch_id}, step {step['id']}")
            sha = gitops.commit_tree(self.root, tree, parent, msg, aid)
            gitops.move_branch(self.root, self.branch, sha, parent)
        faults.hit("after-commit")
        gitops.sync_index_to_head(self.root)
        faults.hit("before-finalize")
        self.finalize(step["id"], sha)

    def finalize(self, step_id: str, sha: str) -> None:
        step = self.s.get("steps", step_id)
        with self.s.tx():
            self.s.transition("steps", step_id, "accepted", actor="driver", reason="finalize",
                              expect_from={"accepting"},
                              fields={"accepted_commit": sha, "accepting": None})
            last = self.s.one("SELECT COUNT(*) AS n FROM steps WHERE plan_id = ? AND status != "
                              "'accepted'", step["plan_id"])["n"] == 0
            if last and not self.lane:  # a lane's plan ships when it has merged
                self.s.transition("plans", step["plan_id"], "shipped", actor="driver",
                                  reason="last step accepted")
            if not self.lane:  # a lane's commit reaches the batch head when it merges
                lease.set_accepted_head(self.s, str(self.batch_root), sha)
            self.event("step.accepted", step=step_id, commit=sha, plan_shipped=last)

    def recover_accepting(self) -> None:
        for step in self.s.all("SELECT * FROM steps WHERE batch_id = ? AND status = 'accepting'"
                               + (" AND plan_id = ?" if self.plan_id else ""),
                               self.batch_id, *([self.plan_id] if self.plan_id else [])):
            intent = json.loads(step["accepting"])
            aid, parent, tree = intent["attempt_id"], intent["parent"], intent["tree"]
            sha = gitops.find_trailer_commit(self.root, self.branch, aid)
            head = gitops.branch_head(self.root, self.branch)
            if sha and gitops.commit_tree_of(self.root, sha) == tree \
                    and gitops.parent_of(self.root, sha) == parent:
                if head == sha:
                    gitops.reset_hard(self.root, sha)
                    self.finalize(step["id"], sha)
                    self.event("recovery", step=step["id"], action="finalize", commit=sha)
                    continue
            elif sha is None and head == parent:
                # Undo includes the files: the accepted-but-uncommitted tree must not outlive
                # the step, e.g. into a root released by a cancel (Codex M1 review #4).
                gitops.reset_hard(self.root, parent)
                self.s.transition("steps", step["id"], "pending", actor="driver",
                                  reason="recovery: undo", expect_from={"accepting"},
                                  fields={"accepting": None})
                self.event("recovery", step=step["id"], action="undo")
                continue
            self.s.transition("steps", step["id"], "blocked", actor="driver",
                              reason="acceptance-mismatch", expect_from={"accepting"},
                              fields={"blocked_reason": "acceptance-mismatch"})
            self.s.open_needs_human("acceptance-mismatch",
                                    f"step {step['id']}: expected tree {tree[:12]} on parent "
                                    f"{parent[:12]}; found commit {sha} and head {head}",
                                    f"inspect {self.root}, then: ilk2 resume {self.batch_id}",
                                    batch_id=self.batch_id)

    # -- cancel (§2.8) ------------------------------------------------------------------

    def finish_cancel(self) -> bool:
        """End a cancelled batch's whole hierarchy, then release its root (Codex M1 review #1).
        Idempotent. While any process is still inside the root nothing is touched and the
        root stays fenced behind an `unkillable` row; False then, and a later call retries."""
        if fenced(self.s, self.batch_id, self.root):
            return False
        self.recover_accepting()
        for a in self.s.all("SELECT a.id FROM attempts a JOIN steps st ON st.id = a.step_id "
                            "WHERE st.batch_id = ? AND a.status = 'running'", self.batch_id):
            self.s.transition("attempts", a["id"], "done", actor="driver", reason="cancelled",
                              fields={"outcome": "cancelled", "reason": "batch cancelled",
                                      "ended_at": clock.wall()})
        with self.s.tx():
            for st in self.s.all("SELECT id FROM steps WHERE batch_id = ? AND status IN "
                                 "('pending', 'accepting', 'blocked')", self.batch_id):
                self.s.transition("steps", st["id"], "cancelled", actor="driver", reason="batch cancelled")
            for p in self.s.all("SELECT id FROM plans WHERE batch_id = ? AND status IN "
                                "('pending', 'active', 'blocked')", self.batch_id):
                self.s.transition("plans", p["id"], "cancelled", actor="driver", reason="batch cancelled")
        row = lease.get(self.s, str(self.batch_root))
        if row and row["holder"] == "driver" and not root_in_use_by_other(self.s, self.batch_id):
            lease.driver_release(self.s, str(self.batch_root),
                                 gitops.branch_head(self.batch_root, self.batch_branch))
        self.s.clear_needs_human(self.batch_id)
        return True


def finish_cancel(s: Store, batch_id: str, b: bounds.Bounds | None = None) -> bool:
    """Run the driver's cancel finalization outside a live run (supervisor, `ilk2 cancel`),
    under a recorded run so every transition has a driver-side actor (I1)."""
    if fenced(s, batch_id, Path(s.get("batches", batch_id)["execution_root"])):
        return False  # checked first, so a fenced root does not add a run row every tick
    rid = new_id("r")
    s.insert("runs", {"id": rid, "batch_id": batch_id, "status": "ended",
                      "terminal_reason": "cancel-recovery", "created_at": clock.wall()})
    return Driver(s, rid, b).finish_cancel()


def main(run_id: str) -> int:
    paths.ensure()
    s = Store(paths.db())
    try:
        reason = Driver(s, run_id).run()
    finally:
        s.close()
    orderly = reason in ("shipped", "parked", "drained", "cancelled") or reason.startswith("blocked")
    return 0 if orderly else 1
