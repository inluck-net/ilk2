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
from dataclasses import asdict
from fnmatch import fnmatch
from pathlib import Path

from . import batches, bounds, clock, faults, gates, gitops, lease, paths, procs, worker
from .gates.env import pinned_env
from .plans import Gate
from .store import Conflict, Store, StoreBusy, dumps, new_id

LOCKFILES = {"package-lock.json", "bun.lockb", "bun.lock", "yarn.lock", "pnpm-lock.yaml",
             "uv.lock", "poetry.lock", "Pipfile.lock"}


class Stop(Exception):
    """End the run with a terminal reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


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
        self.dir = paths.run_dir(run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.api_spent = 0.0
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
            with self.phase("recover"):
                self.recover_accepting()
            self.activate()
            reason = self.loop()
        except Stop as e:
            reason = e.reason
            self.event("run.stop", reason=e.reason, detail=e.detail)
            if reason in ("parked", "cancelled"):  # §2.4.2: the driver releases at these states
                row = lease.get(self.s, str(self.root))
                if row and row["holder"] == "driver" and not self.s.one(
                        "SELECT 1 FROM steps WHERE batch_id = ? AND status = 'accepting'", self.batch_id):
                    lease.driver_release(self.s, str(self.root),
                                         gitops.branch_head(self.root, self.branch))
        except StoreBusy:
            reason = "store-busy"
        except Exception:  # noqa: BLE001 - recorded, the supervisor requeues
            reason = "error"
            (self.dir / "driver-error.txt").write_text(traceback.format_exc())
        try:
            self.s.transition("runs", self.run_id, "ended", actor="driver", reason=reason,
                              fields={"terminal_reason": reason, "ended_at": clock.wall()})
        except Exception:  # noqa: BLE001 - the supervisor reconciles a run left open
            pass
        return reason

    # -- activation: setup and base measurement (§2.5.3, §2.11.2) -------------------------

    def activate(self) -> None:
        done = self.s.one("SELECT 1 FROM base_measurements WHERE batch_id = ? LIMIT 1", self.batch_id)
        if done:
            return
        diag = self.diag_worktree(self.base)
        if self.cfg.setup:
            with self.phase("setup"):
                self.setup(diag)
                self.setup(self.root)
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
        plans = self.s.all("SELECT * FROM plans WHERE batch_id = ? ORDER BY ord", self.batch_id)
        status = {p["slug"]: p["status"] for p in plans}
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
        head = gitops.branch_head(self.root, self.branch)
        self.s.transition("batches", self.batch_id, "shipped", actor="driver", reason="all plans shipped",
                          expect_from={"active"}, fields={"terminal_reason": "shipped"})
        lease.driver_release(self.s, str(self.root), head)
        self.cleanup_diag()
        self.event("batch.shipped", head=head)

    def finish_blocked(self) -> None:
        blocked = [f"{p['slug']}: {p['blocked_reason']}" for p in
                   self.s.all("SELECT * FROM plans WHERE batch_id = ? AND status = 'blocked'",
                              self.batch_id)]
        self.s.transition("batches", self.batch_id, "blocked", actor="driver",
                          reason="nothing runnable", expect_from={"active"},
                          fields={"blocked_reason": "; ".join(blocked)})
        head = gitops.branch_head(self.root, self.branch)
        lease.driver_release(self.s, str(self.root), head)
        self.event("batch.blocked", plans=blocked)

    def block_batch(self, reason: str, detail: str, clear: str) -> None:
        self.s.transition("batches", self.batch_id, "blocked", actor="driver", reason=reason,
                          fields={"blocked_reason": f"{reason}: {detail[:500]}"})
        self.s.open_needs_human(reason, detail[:2000], clear, batch_id=self.batch_id)
        head = gitops.branch_head(self.root, self.branch)
        try:
            lease.driver_release(self.s, str(self.root), head)
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
        if step["restart_count"] >= bounds.RESTARTS_PER_STEP:
            self.block_step(step, "crash-loop", "3 restarts of this step",
                            self.restart_reasons(step["id"]))
            return
        gate_list = [Gate(**g) for g in json.loads(step["gates"])]
        expect_red = json.loads(step["expected_reds"])
        must_pass = json.loads(step["must_pass"])
        scope = json.loads(step["scope"])
        with self.phase("prepare", step=step["id"]):
            head = gitops.branch_head(self.root, self.branch)
            if gitops.changed_paths(self.root):
                (self.dir / f"leftover-{step['id']}.diff").write_text(gitops.diff_text(self.root))
                gitops.reset_hard(self.root, head)
            parent = head
        n = step["attempt_count"]
        last = self.s.one("SELECT evidence FROM attempts WHERE step_id = ? AND evidence IS NOT NULL "
                          "ORDER BY created_at DESC LIMIT 1", step["id"])
        evidence = last["evidence"] if last else None
        session = None
        while n < bounds.ATTEMPTS_PER_STEP:
            self.check_control()
            n += 1
            aid = new_id("a")
            self.s.insert("attempts", {"id": aid, "run_id": self.run_id, "step_id": step["id"],
                                       "n": n, "status": "running", "parent_commit": parent,
                                       "created_at": clock.wall()})
            self.s.update("steps", step["id"], {"attempt_count": n}, actor="driver",
                          reason=f"attempt {n}")
            outcome, summary, session = self.dispatch(step, aid, n, gate_list, evidence, session)
            if outcome in ("api", "quota"):
                n -= 1
                self.s.update("steps", step["id"], {"attempt_count": n}, actor="driver",
                              reason=f"{outcome} error, not counted")
                self.end_attempt(aid, outcome, f"{outcome} error (not counted)", summary)
                self.api_backoff(outcome)
                continue
            red = None
            with self.phase("scope", step=step["id"]):
                out_of_scope = self.out_of_scope(scope)
                if out_of_scope:
                    red = "Changes outside the step's declared scope: " + ", ".join(out_of_scope[:20])
            verdicts: dict = {}
            if red is None:
                with self.phase("gates", step=step["id"]):
                    red, verdicts = self.run_gates(step, aid, gate_list, expect_red, parent,
                                                   must_pass)
            if red is None:
                with self.phase("accept", step=step["id"]):
                    self.accept(step, aid, parent)
                self.end_attempt(aid, "accepted", "", summary, gates_out=verdicts)
                return
            evidence = red
            self.end_attempt(aid, "red", red[:300], summary, evidence=red, gates_out=verdicts)
        self.block_step(step, "attempts", f"{bounds.ATTEMPTS_PER_STEP} attempts red", evidence or "")

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

    def dispatch(self, step, aid: str, n: int, gate_list: list[Gate], evidence: str | None,
                 session: str | None) -> tuple[str, dict, str | None]:
        cfg = worker.WorkerConfig(claude_bin=self.cfg.claude_bin, max_turns=self.cfg.max_turns,
                                  sandbox=self.cfg.sandbox, sandbox_allow=self.cfg.sandbox_allow)
        resume = None
        prev = self.s.one("SELECT id FROM attempts WHERE step_id = ? AND session_id = ? AND "
                          "run_id = ? ORDER BY created_at DESC LIMIT 1", step["id"], session,
                          self.run_id) if (n == 2 and session) else None
        if prev:
            # Q6: the 2nd attempt resumes the same session, whose transcript lives in the
            # previous attempt's config dir. The 3rd starts a fresh session.
            resume = session
            files = worker.AttemptFiles(self.dir / prev["id"])
        else:
            files = worker.AttemptFiles(self.dir / aid)
        worker.prepare(cfg, files, self.root, [g.command for g in gate_list],
                       fresh_config=resume is None)
        if resume:
            prompt = "The driver's gate is red after your attempt. Fix the cause, with the same " \
                     "rules as before.\n\n" + (evidence or "")
        else:
            pre = None
            if self.cfg.preload:  # Q10
                diff = gitops.git(self.root, "diff", f"{self.base}..HEAD", check=False)[:60_000]
                pre = worker.preload(self.root, json.loads(step["scope"]), step["instructions"],
                                     [g.command for g in gate_list], diff)
            prompt = worker.brief(step["title"], step["instructions"],
                                  [" ".join(g.command) for g in gate_list], evidence, preload=pre)
        with self.phase("dispatch", step=step["id"], attempt=aid, n=n, resume=bool(resume)):
            d = worker.dispatch(cfg, files, self.root, prompt, resume=resume)
            outcome = d.wait(self.b.attempt_cap, self.b.term_grace, self.b.kill_grace)
        summary = d.summary()
        if d.terminated and d.terminated.get("survivors"):
            self.s.open_needs_human("unkillable", f"worker group {d.pgid} survived SIGKILL: "
                                    f"{d.terminated['survivors']}", "kill the listed pids, then: "
                                    f"ilk2 resume {self.batch_id}", batch_id=self.batch_id)
            raise Stop("blocked (unkillable)")
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

    def out_of_scope(self, scope: list[str]) -> list[str]:
        if not scope:
            return []
        out = []
        for p in gitops.changed_paths(self.root):
            if not any(p == s or p.startswith(s.rstrip("/") + "/") or fnmatch(p, s) for s in scope):
                out.append(p)
        return out

    def run_gates(self, step, aid: str, gate_list: list[Gate], expect_red: list[str],
                  parent: str, must_pass: list[str] = ()) -> tuple[str | None, dict]:
        changed = set(gitops.changed_paths(self.root))
        if self.cfg.setup and any(Path(p).name in LOCKFILES for p in changed):
            self.setup(self.root)
        reds, verdicts = [], {}
        for gate in gate_list:
            env = self.env()
            spec = gates.spec_version(gate, self.root, env)
            r = gates.measure(gate, self.root, env, self.dir / "gates", spec)
            for _ in range(r.timeouts):
                self.event("gate.timeout", gate=gate.name)
            base = self.base_for(gate, spec)
            v = gates.judge(r, base, expect_red, list(must_pass))
            inherited = []
            if v.outcome == "fail" and parent != self.base and v.new_failures:
                inherited = self.parent_probe(gate, parent, v.new_failures)
            self.s.insert("gate_results", {"attempt_id": aid, "gate": gate.name,
                                           "spec_version": spec, "outcome": v.outcome,
                                           "new_failures": dumps(v.new_failures),
                                           "details": dumps({"reason": v.reason or r.reason,
                                                             "comparable_base": base is not None,
                                                             "possibly_inherited": inherited,
                                                             "duration_s": r.duration_s}),
                                           "at": clock.wall()})
            verdicts[gate.name] = {"outcome": v.outcome, "new": v.new_failures[:50],
                                   "possibly_inherited": inherited}
            if v.outcome == "unmeasured":
                self.block_step(step, "unmeasured", f"gate {gate.name}: {r.reason}", r.tail)
                raise Stop("blocked (unmeasured)", f"{gate.name}: {r.reason}")
            if v.outcome == "fail":
                lines = [f"Gate `{gate.name}` failed: `{' '.join(gate.command)}`"]
                if base is None:
                    lines.append("(The gate's specification changed in this step, so there is "
                                 "no comparable base: every failure counts.)")
                for tid in v.new_failures[:30]:
                    f = r.failures.get(tid)
                    tag = " [possibly-inherited: also failing at the step's parent]" \
                        if tid in inherited else ""
                    lines.append(f"- {tid}{tag}: {f.message if f else ''}")
                lines.append("\nOutput tail:\n" + r.tail[-2500:])
                reds.append("\n".join(lines))
        return ("\n\n".join(reds) if reds else None), verdicts

    def base_for(self, gate: Gate, spec_now: str) -> dict[str, str] | None:
        row = self.s.one("SELECT failures FROM base_measurements WHERE batch_id = ? AND gate = ? "
                         "AND spec_version = ? AND commit_sha = ?", self.batch_id, gate.name,
                         spec_now, self.base)
        if row:
            return json.loads(row["failures"])
        diag = self.diag_worktree(self.base)
        if gates.spec_version(gate, diag, self.env()) != spec_now:
            return None  # no comparable base (§2.5.4)
        return self.measure_base(gate, diag, self.base)

    def parent_probe(self, gate: Gate, parent: str, failing: list[str]) -> list[str]:
        ids = [t for t in failing if "::" in t and not t.endswith(("<collect>", "<exit>", "<no-tests>"))]
        if not ids:
            return []
        diag = self.diag_worktree(parent)
        probe = Gate(name=gate.name + "-parent-probe", adapter=gate.adapter,
                     command=gates.with_test_ids(gate.command, ids, drop=("--deselect", "-k")),
                     cwd=gate.cwd, cap_s=gate.cap_s)
        r = gates.measure(probe, diag, self.env(), self.dir / "gates")
        return sorted(t for t in ids if t in r.failures)

    # -- acceptance (§2.4.1, I12) ---------------------------------------------------------

    def accept(self, step, aid: str, parent: str) -> None:
        if not lease.holder_is_driver(self.s, str(self.root)):
            raise Stop("lease", "the root lease is not held by the driver")
        tree = gitops.worktree_tree(self.root)
        intent = {"attempt_id": aid, "parent": parent, "tree": tree}
        self.s.transition("steps", step["id"], "accepting", actor="driver", reason="intent",
                          expect_from={"pending"}, fields={"accepting": dumps(intent)})
        faults.hit("after-intent")
        sha = gitops.find_trailer_commit(self.root, self.branch, aid)
        if sha is None:
            msg = f"{step['title']}\n\nilk2 batch {self.batch_id}, step {step['id']}"
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
            if last:
                self.s.transition("plans", step["plan_id"], "shipped", actor="driver",
                                  reason="last step accepted")
            lease.set_accepted_head(self.s, str(self.root), sha)
            self.event("step.accepted", step=step_id, commit=sha, plan_shipped=last)

    def recover_accepting(self) -> None:
        for step in self.s.all("SELECT * FROM steps WHERE batch_id = ? AND status = 'accepting'",
                               self.batch_id):
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


def main(run_id: str) -> int:
    paths.ensure()
    s = Store(paths.db())
    try:
        reason = Driver(s, run_id).run()
    finally:
        s.close()
    orderly = reason in ("shipped", "parked", "drained", "cancelled") or reason.startswith("blocked")
    return 0 if orderly else 1
