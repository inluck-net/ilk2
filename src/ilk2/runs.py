"""Run bookkeeping the supervisor and the driver share.

Kept out of driver.py so the supervisor does not load the driver, the worker or the gates at
start: an upgrade drains only when code the supervisor loaded changed (docs/02 §2.7), and most
releases change the driver side only. The supervisor imports Driver lazily, for cancel and
park recovery.
"""

from __future__ import annotations

from pathlib import Path

from . import lease, procs
from .store import Store

NOT_A_RESTART = {"shipped", "parked", "drained", "cancelled", "quota", "api", "waiting-lease",
                 "plan-done", "plan-blocked", "plan-yield"}  # a lane run ends when its plan does,
                                                           # or yields to repairs (§2.5.7)


def counts_as_restart(reason: str | None) -> bool:
    return not (reason in NOT_A_RESTART or (reason or "").startswith("blocked"))


class Stop(Exception):
    """End the run with a terminal reason."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail



def fenced(s: Store, batch_id: str, root: Path) -> bool:
    """True, with an open `unkillable` row, while any process other than ours is in root."""
    found = procs.strays(root)
    if found:
        s.open_needs_human("unkillable", f"processes still inside {root}: {found}",
                           "kill the listed pids; the supervisor then carries on",
                           batch_id=batch_id)
    return bool(found)


def root_in_use_by_other(s: Store, batch_id: str) -> bool:
    """Another queued or active batch works on this batch's root: the root's lease is not this
    batch's to release (R4, shared resolver-<issue> roots)."""
    b = s.get("batches", batch_id)
    return s.one("SELECT 1 FROM batches WHERE execution_root = ? AND id != ? AND status IN "
                 "('queued', 'active')", b["execution_root"], batch_id) is not None


def free_root(s: Store, root: Path, batch_id: str) -> None:
    """A finished batch's ilk2-made root lets go of its branch, so a consumer can write it from
    another worktree (gh-resolve-84, Oct 21: conflict rework refuses a branch checked out in a
    worktree). The worktree stays until gc_worktrees. A root another unfinished batch uses, or
    one leased to anyone, is left alone; the driver (_take_batch_root) and `lease acquire`
    check the branch out again before anything is written there."""
    from . import gitops, paths
    if root.parent != paths.worktrees() or not root.exists():
        return
    if s.one("SELECT 1 FROM batches WHERE execution_root = ? AND id != ? AND status NOT IN "
             "('shipped', 'cancelled')", str(root), batch_id):
        return
    row = lease.get(s, str(root))
    if row and row["holder"] != "released":
        return
    gitops.git(root, "checkout", "-q", "--detach", check=False)


def has_unfinished_cancel(s: Store, batch_id: str) -> bool:
    """A cancelled batch with anything left to end: a live child row or a held root."""
    if s.one("SELECT 1 FROM steps WHERE batch_id = ? AND status IN ('pending', 'accepting', "
             "'blocked') UNION SELECT 1 FROM plans WHERE batch_id = ? AND status IN ('pending', "
             "'active', 'blocked') UNION SELECT 1 FROM attempts a JOIN steps st ON "
             "st.id = a.step_id WHERE st.batch_id = ? AND a.status = 'running'",
             batch_id, batch_id, batch_id):
        return True
    b = s.get("batches", batch_id)
    row = lease.get(s, b["execution_root"])
    return bool(row and row["holder"] == "driver") and not root_in_use_by_other(s, batch_id)




# -- lanes (docs/06 "Parallel plans", Chad's go Oct 14) -----------------------------------------

def is_root_plan(plan) -> bool:
    """Repair and verification plans act on the batch branch itself: they run in the batch root,
    never in a lane."""
    return bool(plan["repair_of_step_id"]) or bool(plan["verification"])


def lane_names(batch, plan_slug: str) -> tuple[Path, str]:
    """A work plan's lane worktree and lane branch."""
    from . import paths
    return paths.worktrees() / f"{batch['id']}.{plan_slug}", f"{batch['branch']}--{plan_slug}"


def ready_plans(s: Store, batch_id: str) -> list:
    """Plans a new lane run may take now, in plan order. Repair plans are a batch barrier
    (§2.5.7): while one is unshipped, only repairs are ready. A plan is taken when a live run is
    bound to it. Work plans need every dependency shipped (merged, under lanes)."""
    import json
    plans = s.all("SELECT * FROM plans WHERE batch_id = ? ORDER BY ord", batch_id)
    taken = {r["plan_id"] for r in s.all("SELECT plan_id FROM runs WHERE batch_id = ? AND status IN "
                                         "('starting', 'running') AND plan_id IS NOT NULL", batch_id)}
    status = {p["slug"]: p["status"] for p in plans}
    if any(is_root_plan(p) for p in plans if p["id"] in taken):
        # A repair or verification run holds the batch root and its merge lock for its whole
        # run: nothing else starts beside it. gh-resolve P6, Oct 14: a second repair admitted
        # beside the first waited on the lock until its spawn was called stalled, three times.
        return []
    open_ = [p for p in plans if p["status"] in ("pending", "active") and p["id"] not in taken]
    repairs = [p for p in plans if p["repair_of_step_id"] and p["status"] != "shipped"]
    if repairs:
        return [p for p in open_ if p["repair_of_step_id"]]
    work_left = any(p["status"] != "shipped" for p in plans if not is_root_plan(p))
    return [p for p in open_
            if all(status.get(d) == "shipped" for d in json.loads(p["depends_on"]))
            and not (p["verification"] and work_left)]  # verification checks the merged whole
