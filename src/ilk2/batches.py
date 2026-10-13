"""Projects and batch submission (docs/02 §2.2, §2.8, §2.11.2).

Identity is declared once, at submission (I3): project id, batch id, execution root, branch.
The normalized spec is snapshotted; plans and steps become rows; the root lease is created
with the driver side holding it while the batch is queued or active.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from . import clock, gitops, lease, paths, plans, procs
from .store import Conflict, Store, dumps, new_id

DEFAULT_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


@dataclass
class ProjectConfig:
    path_prelude: list[str] = field(default_factory=lambda: list(DEFAULT_PATH))
    env_allow: list[str] = field(default_factory=list)
    env_set: dict[str, str] = field(default_factory=dict)
    home_files: list[str] = field(default_factory=list)
    setup: dict | None = None                 # {"command": [...], "cap_s": 900}
    concurrency_limit: int = 1
    # record | owner | consumer. Pages are opt-in (Q12, Chad Oct 13): `record` keeps the durable
    # needs_human row and its events, and pages nobody.
    notify_policy: str = "record"
    claude_bin: str = "claude"
    max_turns: int = 400
    sandbox: bool = True
    sandbox_allow: list[str] = field(default_factory=list)   # extra writable paths (caches)
    preload: bool = True                      # Q10: adopted after P1 step 4 (docs/spikes/p1.md)
    # Paths no batch may change (docs/06 R8), e.g. gh-resolve's write-target denylist
    # ["convex/migrations/"]: a directory prefix or a glob. Checked on every attempt.
    deny_paths: list[str] = field(default_factory=list)
    # The V1 data dir key of a project taken over from V1: queued masters /ilk-plan writes
    # there are claimed and submitted by the supervisor (docs/04 §4.3, ilk2/v1.py).
    v1_key: str | None = None
    # P5 contract (docs/06 R2, R5). max_lease_s: the longest consumer lease on this project's
    # roots; None leaves it uncapped (judgment call: keeps today's callers working; gh-resolve
    # sets about 3600). reserve: host runs kept free for this project even while it is idle.
    max_lease_s: float | None = None
    reserve: int = 0
    # A shadow project (docs/04 §4.2, P5): a separate clone that cannot push, running the same
    # work as the repo it shadows, with no outward effect. Its roots are never leased to a
    # consumer, and its events carry shadow: true.
    shadow_of: str | None = None

    @staticmethod
    def from_json(text: str) -> "ProjectConfig":
        d = json.loads(text or "{}")
        known = {f for f in ProjectConfig.__dataclass_fields__}
        return ProjectConfig(**{k: v for k, v in d.items() if k in known})


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(path == s or path.startswith(s.rstrip("/") + "/") or fnmatch(path, s) for s in patterns)


def project_add(s: Store, project_id: str, repo_root: Path, config: ProjectConfig,
                default_branch: str = "main") -> None:
    s.insert("projects", {"id": project_id, "repo_root": str(repo_root.resolve()),
                          "default_branch": default_branch, "config": dumps(asdict(config)),
                          "created_at": clock.wall()})


def project_config(s: Store, project_id: str) -> ProjectConfig:
    return ProjectConfig.from_json(s.get("projects", project_id)["config"])


def resolve_scripts(spec: plans.BatchSpec, repo: Path, commit: str) -> None:
    """Resolve package-script gates to their runner, reading package.json at the base (§2.5.1)."""
    from .gates import vitest

    def read(rel: str) -> str | None:
        out = gitops.git(repo, "show", f"{commit}:{rel.lstrip('./')}", check=False)
        return out or None

    for p in spec.plans:
        for st in p.steps:
            for g in st.gates:
                if g.adapter == "script":
                    r = vitest.resolve(g.command, g.cwd, read)
                    if r:
                        g.adapter, g.command = r["adapter"], r["command"]
                        g.script, g.script_entry = r["script"], r["script_entry"]
                        g.env = {**r.get("env", {}), **g.env}


def preflight(spec: plans.BatchSpec, repo: Path, base_sha: str, cfg: "ProjectConfig") -> list[str]:
    """Collect-only run of every distinct pytest gate at the base, in the pinned environment,
    so a gate that cannot run is a synchronous rejection, not a later base-unmeasured block
    (docs/06 "Self-resolution"). Projects with a setup hook are skipped: their gates may need
    the install first (judgment call)."""
    import tempfile
    from . import gates
    from .gates.env import pinned_env
    seen, todo = set(), []
    for p in spec.plans:
        for st in p.steps:
            for g in st.gates:
                key = json.dumps(asdict(g), sort_keys=True)
                if g.adapter == "pytest" and key not in seen:
                    seen.add(key)
                    todo.append(g)
    if not todo:
        return []
    problems = []
    paths.data().mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=paths.data(), prefix="preflight-") as d:
        wt = Path(d) / "wt"
        gitops.add_worktree(repo, wt, base_sha)
        try:
            for i, g in enumerate(todo):
                env = pinned_env(home=Path(d) / f"home{i}", tmp=Path(d) / "tmp",
                                 path_prelude=cfg.path_prelude, allow=cfg.env_allow,
                                 set_vars=cfg.env_set, home_files=cfg.home_files)
                probe = plans.Gate(**{**asdict(g), "command": g.command + ["--collect-only", "-q"]})
                r = gates.measure(probe, wt, env, Path(d) / "work")
                if r.outcome == "unmeasured":
                    problems.append(f"gate {g.name} cannot run at the base {base_sha[:12]}: "
                                    f"{r.reason}: {r.tail[-300:].strip()}")
        finally:
            gitops.remove_worktree(repo, wt)
    return problems


class SubmitRejected(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def submit(s: Store, project_id: str, spec: plans.BatchSpec, *, base: str | None = None,
           priority: int = 0, consumer_ref: str | None = None, root: Path | None = None,
           originals: dict | None = None) -> str:
    proj = s.get("projects", project_id)
    cfg = ProjectConfig.from_json(proj["config"])
    repo = Path(proj["repo_root"])
    base_sha = gitops.rev(repo, base or proj["default_branch"])
    problems = [] if base_sha else [f"base {base or proj['default_branch']!r} is not a commit"]
    if base_sha:
        resolve_scripts(spec, repo, base_sha)
    problems += plans.check(spec, root=root or repo, path_dirs=cfg.path_prelude)
    for p in spec.plans:
        for i, st in enumerate(p.steps):
            # a scope entry inside a denied path; a broader scope is fine, since the attempt-time
            # check turns any edit under a denied path red
            bad = [x for x in st.scope if path_matches(x, cfg.deny_paths)]
            if bad:
                problems.append(f"plan {p.slug} step {i}: scope names denied paths {bad} "
                                f"(project deny_paths {cfg.deny_paths})")
    if not problems and not cfg.setup:
        problems += preflight(spec, repo, base_sha, cfg)
    if problems:
        raise SubmitRejected(problems)
    bid = new_id("b")
    branch = f"ilk2/{bid}"
    if root is None:
        root = gitops.add_worktree(repo, paths.worktrees() / bid, base_sha, branch=branch)
    else:
        # An existing root may be held by a consumer or another batch: nothing is written to it
        # here. The driver checks out this batch's branch once it holds the lease (R4).
        root = root.resolve()
    snapshot = spec.to_dict()
    with s.tx():
        s.insert("batches", {"id": bid, "project_id": project_id, "base_commit": base_sha,
                             "branch": branch, "execution_root": str(root), "priority": priority,
                             "consumer_ref": consumer_ref, "status": "queued",
                             "snapshot": dumps(snapshot), "originals": dumps(originals or {}),
                             "created_at": clock.wall()})
        for po, p in enumerate(spec.plans):
            pid = f"{bid}.{p.slug}"
            s.insert("plans", {"id": pid, "batch_id": bid, "slug": p.slug, "ord": po,
                               "depends_on": dumps(p.depends_on), "status": "pending",
                               "verification": int(p.verification)})
            for si, st in enumerate(p.steps):
                s.insert("steps", {"id": f"{pid}.{si}", "plan_id": pid, "batch_id": bid, "idx": si,
                                   "title": st.title, "instructions": st.instructions,
                                   "gates": dumps([asdict(g) for g in st.gates]),
                                   "expected_reds": dumps(st.expect_red), "scope": dumps(st.scope),
                                   "must_pass": dumps(st.must_pass),
                                   "status": "pending"})
        lease.init_driver(s, str(root), base_sha)
        s.event("submitted", {"priority": priority, "plans": len(spec.plans),
                              "steps": sum(len(p.steps) for p in spec.plans)}, batch_id=bid)
    return bid


# -- human and consumer controls (§2.8, §2.11.1) --------------------------------------------

def park(s: Store, batch_id: str, now: bool = False, actor: str = "human") -> None:
    s.transition("batches", batch_id, "parked", actor=actor, reason="park --now" if now else "park",
                 expect_from={"queued", "active"}, fields={"control": "park-now" if now else None})


def resume(s: Store, batch_id: str, actor: str = "human") -> dict:
    """Unblock or unpark: blocked steps and plans go back to pending with fresh counters, the
    batch is queued again, its human rows clear, and the driver side takes the lease back."""
    b = s.get("batches", batch_id)
    root = b["execution_root"]
    row = lease.get(s, root)
    if row["holder"] in ("consumer", "consumer-expired"):
        raise SubmitRejected([f"root lease is held by {row['holder']} {row['owner']}"])
    found = procs.strays(Path(root))
    if found:  # the later process check that lifts an `unkillable` fence (Codex M1 review #5)
        raise SubmitRejected([f"processes are still inside {root}: {found}"])
    head = gitops.branch_head(Path(root), b["branch"])
    out = {"consumer_commits": False}
    with s.tx():
        for st in s.all("SELECT * FROM steps WHERE batch_id = ? AND status = 'blocked'", batch_id):
            s.transition("steps", st["id"], "pending", actor=actor, reason="resume",
                         fields={"attempt_count": 0, "restart_count": 0, "blocked_reason": None})
        for p in s.all("SELECT * FROM plans WHERE batch_id = ? AND status = 'blocked'", batch_id):
            s.transition("plans", p["id"], "pending", actor=actor, reason="resume",
                         fields={"blocked_reason": None})
        s.transition("batches", batch_id, "queued", actor=actor, reason="resume",
                     expect_from={"blocked", "parked"},
                     fields={"blocked_reason": None, "control": None})
        s.clear_needs_human(batch_id)
        for r in s.open_refusals(batch_id):
            s.clear_refusal(batch_id, r["reason"])
        if row["holder"] == "released":
            lease.driver_acquire(s, root, row["generation"])
            if head and head != row["accepted_head"]:
                out["consumer_commits"] = True
                s.event("consumer-commits", {"from": row["accepted_head"], "to": head},
                        batch_id=batch_id)
                lease.set_accepted_head(s, root, head)
        s.event("resumed", {"by": actor}, batch_id=batch_id)
    return out


CANCEL_REASONS = {"issue-closed", "claim-released", "stand-down", "superseded", "human"}


def cancel(s: Store, batch_id: str, reason: str, superseded_by: str | None = None,
           actor: str = "human") -> None:
    if reason not in CANCEL_REASONS:
        raise SubmitRejected([f"cancel reason must be one of {sorted(CANCEL_REASONS)}"])
    s.transition("batches", batch_id, "cancelled", actor=actor, reason=reason,
                 expect_from={"queued", "active", "blocked", "parked"},
                 fields={"terminal_reason": f"cancelled ({reason})",
                         "blocked_reason": f"superseded by {superseded_by}" if superseded_by else None})
    live = s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status IN ('starting', 'running')", batch_id)
    if not live:  # nothing running: finish now; otherwise the supervisor stops the run first
        from .driver import finish_cancel
        finish_cancel(s, batch_id)
    s.event("cancelled", {"reason": reason, "superseded_by": superseded_by}, batch_id=batch_id)


def amend(s: Store, batch_id: str, ops: list[dict], *, expect_version: int, actor: str = "human",
          reason: str = "amend") -> int:
    """Amend pending steps (Q3): version-checked against the batch, journalled in an
    append-only table; the submitted snapshot is never edited. Ops:
      {"op": "replace", "step": <step id>, "instructions"?, "gates"?, "must_pass"?, "scope"?, "title"?}
      {"op": "add", "plan": <slug>, "title", "instructions", "gates", ...}  (appended to the plan)
    Returns the new batch version."""
    problems = []
    with s.tx() as c:
        b = c.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if b["version"] != expect_version:
            raise Conflict(f"batch version is {b['version']}, not {expect_version}")
        if b["status"] in ("shipped", "cancelled"):
            raise Conflict(f"batch is {b['status']}")
        for op in ops:
            if op.get("op") == "replace":
                st = c.execute("SELECT * FROM steps WHERE id = ? AND batch_id = ?",
                               (op["step"], batch_id)).fetchone()
                if st is None or st["status"] != "pending" or st["attempt_count"] > 0:
                    problems.append(f"step {op['step']} has started or does not exist")
                    continue
                fields = {}
                for k in ("title", "instructions", "scope", "must_pass"):
                    if k in op:
                        fields[k] = op[k] if k in ("title", "instructions") else dumps(op[k])
                if "gates" in op:
                    spec = plans.from_dict({"plans": [{"slug": "x", "steps": [
                        {"instructions": "x", "gates": op["gates"]}]}]})
                    probs = plans.check(spec)
                    problems += [f"step {op['step']}: {p}" for p in probs]
                    fields["gates"] = dumps([asdict(g) for g in spec.plans[0].steps[0].gates])
                if fields and not problems:
                    s.update("steps", st["id"], fields, actor=actor, reason=f"amend: {reason}")
            elif op.get("op") == "add":
                p = c.execute("SELECT * FROM plans WHERE batch_id = ? AND slug = ?",
                              (batch_id, op["plan"])).fetchone()
                if p is None or p["status"] == "shipped":
                    problems.append(f"plan {op['plan']} is missing or shipped")
                    continue
                spec = plans.from_dict({"plans": [{"slug": op["plan"], "steps": [op]}]})
                probs = plans.check(spec)
                if probs:
                    problems += probs
                    continue
                stp = spec.plans[0].steps[0]
                idx = c.execute("SELECT COALESCE(MAX(idx), -1) + 1 FROM steps WHERE plan_id = ?",
                                (p["id"],)).fetchone()[0]
                s.insert("steps", {"id": f"{p['id']}.{idx}", "plan_id": p["id"], "batch_id": batch_id,
                                   "idx": idx, "title": stp.title, "instructions": stp.instructions,
                                   "gates": dumps([asdict(g) for g in stp.gates]),
                                   "expected_reds": dumps(stp.expect_red), "scope": dumps(stp.scope),
                                   "must_pass": dumps(stp.must_pass), "status": "pending"})
            elif op.get("op") == "add_plan":
                # A whole new plan, e.g. a driver repair plan (§2.5.7); it may name the step it
                # repairs and sorts before every submitted plan.
                spec = plans.from_dict({"plans": [{"slug": op["slug"], "steps": op["steps"]}]})
                probs = plans.check(spec)
                if c.execute("SELECT 1 FROM plans WHERE batch_id = ? AND slug = ?",
                             (batch_id, op["slug"])).fetchone():
                    probs.append(f"plan {op['slug']} exists")
                if probs:
                    problems += probs
                    continue
                ord_ = c.execute("SELECT COALESCE(MIN(ord), 0) - 1 FROM plans WHERE batch_id = ?",
                                 (batch_id,)).fetchone()[0]
                pid = f"{batch_id}.{op['slug']}"
                s.insert("plans", {"id": pid, "batch_id": batch_id, "slug": op["slug"], "ord": ord_,
                                   "depends_on": "[]", "status": "pending",
                                   "repair_of_step_id": op.get("repair_of_step_id")})
                for idx, stp in enumerate(spec.plans[0].steps):
                    s.insert("steps", {"id": f"{pid}.{idx}", "plan_id": pid, "batch_id": batch_id,
                                       "idx": idx, "title": stp.title, "instructions": stp.instructions,
                                       "gates": dumps([asdict(g) for g in stp.gates]),
                                       "expected_reds": dumps(stp.expect_red), "scope": dumps(stp.scope),
                                       "must_pass": dumps(stp.must_pass), "status": "pending"})
            else:
                problems.append(f"unknown op {op.get('op')!r}")
        if problems:
            raise SubmitRejected(problems)
        s.update("batches", batch_id, {"priority": b["priority"]}, actor=actor, reason=f"amend: {reason}")
        after = expect_version + 1
        c.execute("INSERT INTO amendments(batch_id, actor, reason, ops, batch_version_before, "
                  "batch_version_after, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (batch_id, actor, reason, dumps(ops), expect_version, after, clock.wall()))
        s.event("amended", {"ops": len(ops), "version": after}, batch_id=batch_id)
        return after


def export_v1(s: Store, batch_id: str, out_dir: Path) -> list[Path]:
    """Write the batch's not-yet-accepted steps as V1 sub-plan Markdown (§2.8, §4.3: the way back
    to V1 between batches). Accepted steps are listed as done; gates become local_checks."""
    import shlex
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    b = s.get("batches", batch_id)
    for p in s.all("SELECT * FROM plans WHERE batch_id = ? ORDER BY ord", batch_id):
        steps = s.all("SELECT * FROM steps WHERE plan_id = ? ORDER BY idx", p["id"])
        todo = [st for st in steps if st["status"] != "accepted"]
        done = [st for st in steps if st["status"] == "accepted"]
        scope = sorted({x for st in steps for x in json.loads(st["scope"])})
        lines = ["---", f"plan: {p['slug']}", "status: queued", "current_step: 0",
                 f"depends_on: {p['depends_on']}", f"scope_paths: {json.dumps(scope)}", "---", "",
                 f"# Sub-plan: {p['slug']}", "",
                 f"Exported from ilk2 batch {batch_id} (base {b['base_commit'][:12]}, branch {b['branch']}).", ""]
        if done:
            lines += ["## Already accepted on ilk2", ""]
            lines += [f"- {st['title']} ({(st['accepted_commit'] or '')[:12]})" for st in done]
            lines.append("")
        lines += ["## Steps", ""]
        for i, st in enumerate(todo):
            lines += [f"### Step {i} — {st['title']}", "", "```yaml", "local_checks:"]
            for g in json.loads(st["gates"]):
                lines += [f'  - command: {json.dumps(shlex.join(g["command"]))}', f"    timeout: {g['cap_s']}"]
            lines += ["```", "", st["instructions"], ""]
        f = out_dir / f"{p['slug']}.md"
        f.write_text("\n".join(lines))
        written.append(f)
    return written
