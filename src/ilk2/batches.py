"""Projects and batch submission (docs/02 §2.2, §2.8, §2.11.2).

Identity is declared once, at submission (I3): project id, batch id, execution root, branch.
The normalized spec is snapshotted; plans and steps become rows; the root lease is created
with the driver side holding it while the batch is queued or active.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import clock, gitops, lease, paths, plans
from .store import Store, dumps, new_id

DEFAULT_PATH = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


@dataclass
class ProjectConfig:
    path_prelude: list[str] = field(default_factory=lambda: list(DEFAULT_PATH))
    env_allow: list[str] = field(default_factory=list)
    env_set: dict[str, str] = field(default_factory=dict)
    home_files: list[str] = field(default_factory=list)
    setup: dict | None = None                 # {"command": [...], "cap_s": 900}
    concurrency_limit: int = 1
    notify_policy: str = "owner"              # owner | consumer
    claude_bin: str = "claude"
    max_turns: int = 400
    sandbox: bool = True
    sandbox_allow: list[str] = field(default_factory=list)   # extra writable paths (caches)
    preload: bool = False                     # Q10: decided by P1 step 4

    @staticmethod
    def from_json(text: str) -> "ProjectConfig":
        d = json.loads(text or "{}")
        known = {f for f in ProjectConfig.__dataclass_fields__}
        return ProjectConfig(**{k: v for k, v in d.items() if k in known})


def project_add(s: Store, project_id: str, repo_root: Path, config: ProjectConfig,
                default_branch: str = "main") -> None:
    s.insert("projects", {"id": project_id, "repo_root": str(repo_root.resolve()),
                          "default_branch": default_branch, "config": dumps(asdict(config)),
                          "created_at": clock.wall()})


def project_config(s: Store, project_id: str) -> ProjectConfig:
    return ProjectConfig.from_json(s.get("projects", project_id)["config"])


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
    problems += plans.check(spec, root=root or repo, path_dirs=cfg.path_prelude)
    if problems:
        raise SubmitRejected(problems)
    bid = new_id("b")
    branch = f"ilk2/{bid}"
    if root is None:
        root = gitops.add_worktree(repo, paths.worktrees() / bid, base_sha, branch=branch)
    else:
        root = root.resolve()
        if gitops.branch_head(root, branch) is None:
            gitops.git(root, "checkout", "-q", "-b", branch, base_sha)
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
    if not live:  # nothing running: release now; otherwise the supervisor stops the run first
        b = s.get("batches", batch_id)
        row = lease.get(s, b["execution_root"])
        if row and row["holder"] == "driver":
            lease.driver_release(s, b["execution_root"],
                                 gitops.branch_head(Path(b["execution_root"]), b["branch"]))
        s.clear_needs_human(batch_id)
    s.event("cancelled", {"reason": reason, "superseded_by": superseded_by}, batch_id=batch_id)
