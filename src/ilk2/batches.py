"""Projects and batch submission (docs/02 §2.2, §2.8, §2.11.2).

Identity is declared once, at submission (I3): project id, batch id, execution root, branch.
The normalized spec is snapshotted; plans and steps become rows; the root lease is created
with the driver side holding it while the batch is queued or active.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import clock, gitops, lease, paths, plans, procs
from .store import Conflict, Store, dumps, new_id

DEFAULT_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
META_MAX = 16_384


WORKER_EFFORTS = ("low", "medium", "high", "xhigh", "max")   # claude --effort's levels


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
    # The address every ilk2 commit in this project must carry (docs/02 §2.4 step 2); None
    # checks only that the repository is not bare.
    commit_email: str | None = None
    reserve: int = 0
    # Lanes (docs/06 "Parallel plans", Chad's go Oct 14): independent work plans of one batch
    # run at once, each in its own worktree and lane branch, merged into the batch branch when
    # the plan's steps are accepted. 1 keeps the serial path exactly.
    max_lanes: int = 1
    # Paths (gitattributes patterns) whose lane-merge conflicts resolve as a union of both
    # sides instead of a repair plan: docs every plan appends to. gh-resolve P6, Oct 14: 2 of 3
    # merge repairs were docs/design/raw-ledger-readers.md alone, about 22 worker-minutes each.
    # Opt-in (judgment call): wrong for files whose order or uniqueness matters, which a union
    # garbles without a conflict to show it; the re-gate still runs.
    union_merge: list[str] = field(default_factory=list)
    # RSI (docs/06 "RSI"): the ilk2 project's trigger plans and submits ilk2's own improvements
    # when idle (ilk2 rsi tick). Off unless set; shipped RSI branches are still reviewed by hand.
    rsi_auto: bool = False
    rsi_gate: dict | None = None   # the gate every RSI step gets (default: rsi.SUITE_GATE)
    # Unattended landing (Chad, Oct 15): shipped RSI branches merge, verify, release, canary
    # and push by themselves, audited in rsi_landings (rsi_land.py). Off: they wait for review.
    rsi_land: bool = False
    # `ilk2 project remove`: no new batches, no V1 claims; the row and its history stay.
    retired: bool = False
    # A shadow project (docs/04 §4.2, P5): a separate clone that cannot push, running the same
    # work as the repo it shadows, with no outward effect. Its roots are never leased to a
    # consumer, and its events carry shadow: true.
    shadow_of: str | None = None
    # The project's batch verification under ilk2: its full suite, run at the batch head. A V1
    # batch-verify plan's gates call V1's own tooling (~/.ilk/current scripts writing V1
    # records); on import they are replaced by this command (gh-resolve's P5 master, Oct 13).
    verify_command: list[str] | None = None
    # The worker's model and effort (docs/03 §3.4; built Oct 18 after batch 18a's retrospective).
    # None keeps the worker template's model and claude's default effort. Recorded per attempt.
    worker_model: str | None = None
    worker_effort: str | None = None   # one of WORKER_EFFORTS
    # Step k of a plan continues step k-1's session (Oct 13: 19 of 27.5 min of a gh-resolve step
    # went to re-exploring). Judgment call: on, unless that session passed carry_max_context.
    carry_session: bool = True
    # A step's scope as a hard limit (ilk2's own plans) or as advice (projects planned with V1's
    # /ilk-plan, whose scope_paths V1 itself never enforced: plan_lint and the ship report only).
    # deny_paths stay hard either way. Set False when a project is taken over from V1.
    enforce_scope: bool = True
    carry_max_context: int = 400_000

    @staticmethod
    def from_json(text: str) -> "ProjectConfig":
        d = json.loads(text or "{}")
        known = {f for f in ProjectConfig.__dataclass_fields__}
        return ProjectConfig(**{k: v for k, v in d.items() if k in known})


def _glob_re(pattern: str) -> "re.Pattern[str]":
    """A path glob with gh-resolve's semantics (admission._matches_any): `**/` matches zero or
    more directories, `/**` at the end everything below, `*` and `?` stay within one segment."""
    import re
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matches(path: str, patterns: list[str]) -> bool:
    for s in patterns:
        if not any(c in s for c in "*?["):
            d = s.rstrip("/")
            if path == d or path.startswith(d + "/"):
                return True  # a plain path: itself and everything under it (scope_paths style)
            continue
        if _glob_re(s).match(path):
            return True
    return False


def redacted(cfg: "ProjectConfig") -> dict:
    """A project's settings for display (`ilk2 project show`): every field, but environment
    values are replaced by "<set>", since they can carry credentials (AGENTS.md)."""
    d = asdict(cfg)
    d["env_set"] = {k: "<set>" for k in d.get("env_set") or {}}
    if isinstance(d.get("setup"), dict) and isinstance(d["setup"].get("env"), dict):
        d["setup"] = {**d["setup"], "env": {k: "<set>" for k in d["setup"]["env"]}}
    return d


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
                        if g.adapter == "shell" and g.from_v1:
                            # A V1 local check whose script is not a test runner (typecheck,
                            # lint, build) is an exit-code check, as in V1: `bun run
                            # typecheck:convex` resolved to tsc and the submit was refused as a
                            # shell test gate (gh-resolve on rezmac kira-live, Oct 14, run
                            # a564c9f5). A JSON spec's test gate is still refused (§2.5.1).
                            g.declares_tests = False


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


V1_TOOLING = ("verification_record.py", "verify_attribution.py")  # V1's batch-verify scripts


def _v1_tooling(g: plans.Gate) -> bool:
    """A gate that runs V1's own tooling: through ~/.ilk/ or ~/.ilk-data, or V1's batch-verify
    scripts by name (ilk-skills, V1's own repo, calls them by a repo-relative path)."""
    return any("/.ilk/" in a or ".ilk-data" in a or Path(a).name in V1_TOOLING for a in g.command)


def translate_v1_tooling(spec: plans.BatchSpec, cfg: "ProjectConfig") -> list[str]:
    """Drop gates that run V1's own tooling; a step left without gates gets the project's
    verify_command. Returns problems when a step needs it and the project has none."""
    problems = []
    for p in spec.plans:
        for i, st in enumerate(p.steps):
            v1 = [g for g in st.gates if _v1_tooling(g)]
            if not v1:
                continue
            st.gates = [g for g in st.gates if not _v1_tooling(g)]
            if st.gates:
                continue
            if not cfg.verify_command:
                problems.append(f"plan {p.slug} step {i}: its gates run V1's tooling ({' '.join(v1[0].command)[:120]}); "
                                "set the project's verify_command (its full suite) for ilk2 to verify instead")
                continue
            cmd = list(cfg.verify_command)
            st.gates = [plans.Gate(name="batch-verify", adapter=plans.adapter_for(cmd), command=cmd,
                                   cap_s=3600, declares_tests=plans.adapter_for(cmd) != "shell")]
    return problems


class SubmitRejected(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


BASE_BEHIND_WARN = (50, 2.0)  # judgment call (gh-resolve agreed, Oct 16): commits, days


def base_behind(repo: Path, base_sha: str, default_branch: str) -> dict | None:
    """How far a batch's base is behind origin/<default branch>: commits and days (the tip's
    commit time minus the base's). gh-resolve D-555, Oct 16: #8644 built on a stale dev commit
    whose consistency check and chunk test were already red. Fetched first, best effort: an
    unreachable remote measures against the last fetched tip. None when there is no remote tip."""
    gitops.git(repo, "fetch", "-q", "origin", default_branch, check=False)
    tip = gitops.rev(repo, f"origin/{default_branch}")
    if not tip or not base_sha:
        return None
    n = gitops.git(repo, "rev-list", "--count", f"{base_sha}..{tip}", check=False)
    try:
        t_tip = int(gitops.git(repo, "show", "-s", "--format=%ct", tip))
        t_base = int(gitops.git(repo, "show", "-s", "--format=%ct", base_sha))
    except (ValueError, gitops.GitError):
        return None
    return {"commits": int(n or 0), "days": round(max(0, t_tip - t_base) / 86400, 1),
            "default": f"origin/{default_branch}", "tip": tip[:12]}


def base_warning(bb: dict | None) -> str | None:
    if not bb:
        return None
    if bb["commits"] >= BASE_BEHIND_WARN[0] or bb["days"] >= BASE_BEHIND_WARN[1]:
        return (f"base is {bb['commits']} commits and {bb['days']} days behind {bb['default']} "
                f"({bb['tip']}): a run inherits its breakage; refresh the branch first")
    return None


def submit(s: Store, project_id: str, spec: plans.BatchSpec, *, base: str | None = None,
           priority: int = 0, consumer_ref: str | None = None, root: Path | None = None,
           originals: dict | None = None, branch: str | None = None,
           deny_paths: list[str] | None = None, meta: dict | None = None,
           check_only: bool = False, not_after: float | None = None,
           fence: tuple[str, int] | None = None) -> str:
    """Validate and create a batch. check_only validates exactly as a submit would and creates
    nothing: a consumer can test its rendered plans before it goes live (gh-resolve on rezmac,
    Oct 14: two live dispatches were rejected one after the other)."""
    proj = s.get("projects", project_id)
    cfg = ProjectConfig.from_json(proj["config"])
    if cfg.retired:
        raise SubmitRejected([f"project {project_id} is retired (ilk2 project remove)"])
    repo = Path(proj["repo_root"])
    problems = []
    existing = gitops.branch_head(repo, branch) if branch else None
    if existing:
        # B1 (gh-resolve rework on resolver-<issue>): the batch builds on the branch's head. With
        # base refresh off, the consumer merges its base into the branch before it submits.
        if base and gitops.rev(repo, base) not in (None, existing):
            problems.append(f"branch {branch} is at {existing[:12]}, not the requested base "
                            f"{base[:12]}: merge the base into the branch first, or omit --base")
        base = existing
    if cfg.shadow_of:
        # A shadow clone's remote refs are only as fresh as its last fetch: kira-shadow's
        # origin/main was July's, and a worker's own fetch cannot write the shared .git from its
        # sandbox (kira-cloudflare-a2, Oct 14: wrong "on main?" verdicts). Refresh all of them.
        gitops.git(repo, "fetch", "-q", "--prune", "origin", check=False)
    base_sha = gitops.rev(repo, base or proj["default_branch"])
    if base_sha is None and cfg.shadow_of:
        # B6: a shadow clone may not have the base yet; fetch only (its push URL is disabled)
        gitops.git(repo, "fetch", "-q", "origin", check=False)
        if base:
            gitops.git(repo, "fetch", "-q", "origin", base, check=False)
        base_sha = gitops.rev(repo, base or proj["default_branch"])
    if base_sha is None:
        problems.append(f"base {base or proj['default_branch']!r} is not a commit")
    if base_sha:
        resolve_scripts(spec, repo, base_sha)
    problems += translate_v1_tooling(spec, cfg)
    plans.merge_v1_ceremony(spec)
    problems += plans.check(spec, root=root or repo, path_dirs=cfg.path_prelude)
    deny = list(cfg.deny_paths) + list(deny_paths or [])
    for p in spec.plans:
        for i, st in enumerate(p.steps):
            # a scope entry or Write/Edit bullet inside a denied path (this replaces V1's
            # safety-kernel admission, docs/07 §7.4.1); a broader scope is fine, since the
            # attempt-time check turns any edit under a denied path red
            bad = [x for x in st.scope + plans.write_bullets(st.instructions) if path_matches(x, deny)]
            if bad:
                problems.append(f"plan {p.slug} step {i}: declares writes to denied paths {bad} "
                                f"(deny_paths {deny})")
    if not problems and not cfg.setup:
        problems += preflight(spec, repo, base_sha, cfg)
    if problems:
        raise SubmitRejected(problems)
    path_warns = gate_path_warnings(spec, repo, base_sha)
    late = _fenced(s, not_after, fence)  # early, so a refused submit makes no worktree
    if late:
        raise SubmitRejected([late])
    if check_only:
        return ""
    bid = new_id("b")
    branch = branch or f"ilk2/{bid}"
    made = None  # a worktree this submit created, removed if the submit is refused after all
    if root is None and existing:
        prior = s.one("SELECT execution_root FROM batches WHERE project_id = ? AND branch = ? "
                      "ORDER BY created_at DESC LIMIT 1", project_id, branch)
        if prior and Path(prior["execution_root"]).exists():
            root = Path(prior["execution_root"])  # rework runs in the branch's root (R4)
        else:
            root = paths.worktrees() / bid
            gitops.git(repo, "worktree", "add", "-q", str(root), branch)
    elif root is None:
        root = gitops.add_worktree(repo, paths.worktrees() / bid, base_sha, branch=branch)
        made = root
    else:
        # An existing root may be held by a consumer or another batch: nothing is written to it
        # here. The driver checks out this batch's branch once it holds the lease (R4).
        root = root.resolve()
    snapshot = spec.to_dict()
    snapshot["base_behind"] = base_behind(repo, base_sha, proj["default_branch"])
    if deny_paths:
        snapshot["deny_paths"] = list(deny_paths)  # submit --deny-path: this batch only, additive
    if meta is not None:
        # The consumer's own record of the batch (gh-resolve: issue, run id, rework context),
        # returned unchanged by status; ilk2 never reads it (option (c), Oct 14).
        if not isinstance(meta, dict) or len(json.dumps(meta)) > META_MAX:
            raise SubmitRejected([f"--meta must be a JSON object of at most {META_MAX} bytes"])
        snapshot["meta"] = meta
    try:
        _commit(s, bid, project_id, base_sha, branch, root, priority, consumer_ref, snapshot,
                originals, spec, meta, path_warns, not_after, fence)
    except SubmitRejected:
        if made is not None:
            gitops.remove_worktree(repo, made)
        raise
    return bid


def _fenced(s, not_after: float | None, fence: tuple[str, int] | None) -> str | None:
    """Submit-side fencing (gh-resolve ADR 0116): a submit processed after its deadline, or with
    a fence token older than one already seen for its key, is refused. The authoritative check
    runs in the transaction that makes the batch durable (_commit); this one only saves work."""
    if not_after is not None and clock.wall() > not_after:
        return f"refused:not-after: processed at {clock.wall():.3f}, after {not_after:.3f}"
    if fence is not None:
        row = s.one("SELECT token FROM submit_fences WHERE key = ?", fence[0])
        if row and row["token"] > fence[1]:
            return f"refused:fence: token {fence[1]} for {fence[0]!r} is older than {row['token']}"
    return None


def advance_fence(s, key: str, token: int) -> int:
    """Record a fence token for a key; tokens only move forward. Returns the stored token."""
    with s.tx() as c:
        c.execute("INSERT INTO submit_fences(key, token, at) VALUES (?, ?, ?) ON CONFLICT(key) DO "
                  "UPDATE SET token = MAX(token, excluded.token), at = excluded.at",
                  (key, int(token), clock.wall()))
        return c.execute("SELECT token FROM submit_fences WHERE key = ?", (key,)).fetchone()["token"]


def _commit(s, bid, project_id, base_sha, branch, root, priority, consumer_ref, snapshot,
            originals, spec, meta, path_warns, not_after, fence) -> None:
    with s.tx():
        late = _fenced(s, not_after, fence)  # in the same transaction as the insert
        if late:
            raise SubmitRejected([late])
        if fence is not None:
            advance_fence(s, *fence)
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
        for w in plan_warnings(spec) + path_warns:
            s.event("submit.warning", w, batch_id=bid)
        # An RSI goal round that closed while this batch was planned (docs/08 B2): the batch is
        # recorded, then cancelled in the same transaction, so the round's close table sees a
        # cancellation transition and the caller sees a cancelled batch.
        rid = (meta or {}).get("rsi_goal_round_id")
        if rid:
            r = s.one("SELECT closed_at FROM rsi_goal_rounds WHERE id = ?", rid)
            if r is None or r["closed_at"] is not None:
                s.transition("batches", bid, "cancelled", actor="rsi", reason="rsi-round-closed")


# -- plan-shape warnings (docs/07 §7.4.1): telemetry, never a refusal -----------------------

# Per plan, per step: a V1 sub-plan's steps share its preamble and plan-level scope, so a step's
# own section (0.9-1.9k chars in both 17d and 17e) says nothing about its size. Over 53 gh-resolve
# plans whose steps all shipped (Oct 17), files per step and characters per step each rank with
# minutes per step at Spearman 0.45-0.48; 18 plans averaged > 40 min per step.
FILES_PER_STEP_MAX = 6        # judgment call: flags 16 of 53 plans, 10 of them slow; misses 8 slow
CHARS_PER_STEP_MAX = 12_000   # judgment call: flags 15 of 53, 10 of them slow; misses 8 slow


def _own(instructions: str) -> str:
    """A V1 step's own section; a JSON step's instructions are all its own."""
    i = instructions.rfind("\n### Step ")
    return instructions[i:] if i >= 0 else instructions


def plan_warnings(spec: plans.BatchSpec) -> list[dict]:
    """Shapes that made batch 17d slow (docs/evidence E5). Wrong if plans past these bounds prove
    as fast and reliable per step; they are measured, not enforced."""
    out = []
    for p in spec.plans:
        if p.verification or not p.steps:
            continue
        n = len(p.steps)
        files = len({x for st in p.steps for x in st.scope}) / n
        shared = len(p.steps[0].instructions) - len(_own(p.steps[0].instructions))
        chars = (shared + sum(len(_own(st.instructions)) for st in p.steps)) / n
        if files > FILES_PER_STEP_MAX:
            out.append({"kind": "files-per-step", "plan": p.slug, "steps": n, "value": round(files, 1),
                        "limit": FILES_PER_STEP_MAX})
        if chars > CHARS_PER_STEP_MAX:
            out.append({"kind": "chars-per-step", "plan": p.slug, "steps": n, "value": round(chars),
                        "limit": CHARS_PER_STEP_MAX})
    chain = serial_chain(spec)
    if chain:
        out.append({"kind": "serial-chain", "disjoint": chain})
    return out


def gate_path_warnings(spec: plans.BatchSpec, repo: Path, base_sha: str | None) -> list[dict]:
    """Test paths a gate names that are absent at the base and that no plan of the batch
    declares in its scope. At the base such a path is 0 tests, but after any step it is
    "declared, absent after the step", so the gate is red every time (gh-resolve 2026-10-19b:
    gates named tests that existed only on a running batch's branch, and --check said ok).
    A warning, not a refusal: a scope list can be incomplete (judgment call; wrong if such
    gates keep reaching runs, then refuse)."""
    if not base_sha:
        return []
    from .gates import _split_missing
    files = set(gitops.git(repo, "ls-tree", "-r", "--name-only", base_sha, check=False).splitlines())
    dirs = {str(Path(f).parent) for f in files}
    for d in list(dirs):
        dirs.update(str(x) for x in Path(d).parents)
    scopes = [x for p in spec.plans for st in p.steps for x in st.scope]
    out, seen = [], set()
    for p in spec.plans:
        for st in p.steps:
            for g in st.gates:
                if g.adapter != "pytest" or not g.declares_tests:
                    continue

                def exists(rel: str, cwd: str = g.cwd or ".") -> bool:
                    full = os.path.normpath(os.path.join(cwd, rel))
                    return full in files or full in dirs

                _, missing = _split_missing(g.command, Path(g.cwd or "."), exists)
                for m in missing:
                    full = os.path.normpath(os.path.join(g.cwd or ".", m.split("::")[0]))
                    if (p.slug, g.name, full) in seen or path_matches(full, scopes):
                        continue
                    seen.add((p.slug, g.name, full))
                    out.append({"kind": "gate-path-absent", "plan": p.slug, "gate": g.name,
                                "path": full, "base": base_sha[:12]})
    return out


def serial_chain(spec: plans.BatchSpec) -> list[list[str]]:
    """Pairs of work plans that dependencies alone keep apart although their scopes are disjoint,
    when the dependencies (followed transitively) put every work plan in one line, so no two
    can run in lanes. Direct lists (17d: each plan names every earlier one) and chains
    (B→A, C→B) count alike. [] when any two plans are unordered, or every ordered pair shares
    files."""
    work = [p for p in spec.plans if not p.verification]
    deps = {p.slug: set(p.depends_on) for p in work}

    def reach(a: str) -> set[str]:
        seen, todo = set(), list(deps.get(a, ()))
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo.extend(deps.get(d, ()))
        return seen

    up = {p.slug: reach(p.slug) for p in work}
    pairs = [(a, b) for i, a in enumerate(work) for b in work[i + 1:]]
    if len(work) < 2 or any(a.slug not in up[b.slug] and b.slug not in up[a.slug] for a, b in pairs):
        return []

    def overlap(x: list[str], y: list[str]) -> bool:
        return any(path_matches(u, [v]) or path_matches(v, [u]) for u in x for v in y)

    scope = {p.slug: [x for st in p.steps for x in st.scope] for p in work}
    return [[a.slug, b.slug] for a, b in pairs
            if scope[a.slug] and scope[b.slug] and not overlap(scope[a.slug], scope[b.slug])]


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
    if s.get("batches", batch_id)["status"] == "cancelled":
        return  # B8: idempotent; a shipped batch still refuses (Conflict, exit 3)
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
      {"op": "merge_ceremony", "plan": <slug>}  (plans.merge_v1_ceremony on a plan not started)
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
            elif op.get("op") == "merge_ceremony":
                # A batch imported before the merge existed (gh-resolve P5, Oct 13: 18 steps,
                # 5 of them red-first steps of plans that had not started).
                p = c.execute("SELECT * FROM plans WHERE batch_id = ? AND slug = ?",
                              (batch_id, op["plan"])).fetchone()
                rows = c.execute("SELECT * FROM steps WHERE plan_id = ? ORDER BY idx",
                                 (p["id"],)).fetchall() if p else []
                if not p or p["status"] != "pending" or any(
                        r["status"] != "pending" or r["attempt_count"] for r in rows):
                    problems.append(f"plan {op['plan']} is missing or has started")
                    continue
                spec = plans.from_dict({"plans": [{"slug": p["slug"], "verification": bool(
                    p["verification"]), "steps": [
                    {"title": r["title"], "instructions": r["instructions"],
                     "gates": json.loads(r["gates"]), "scope": json.loads(r["scope"]),
                     "expect_red": json.loads(r["expected_reds"]),
                     "must_pass": json.loads(r["must_pass"])} for r in rows]}]})
                merged = plans.merge_v1_ceremony(spec).plans[0].steps
                if len(merged) == len(rows):
                    continue
                c.execute("DELETE FROM steps WHERE plan_id = ?", (p["id"],))
                for idx, stp in enumerate(merged):
                    s.insert("steps", {"id": f"{p['id']}.{idx}", "plan_id": p["id"],
                                       "batch_id": batch_id, "idx": idx, "title": stp.title,
                                       "instructions": stp.instructions,
                                       "gates": dumps([asdict(g) for g in stp.gates]),
                                       "expected_reds": dumps(stp.expect_red),
                                       "scope": dumps(stp.scope),
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
