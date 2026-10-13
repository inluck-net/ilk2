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
                 "plan-done", "plan-blocked"}  # a lane run ends when its plan does


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
    open_ = [p for p in plans if p["status"] in ("pending", "active") and p["id"] not in taken]
    repairs = [p for p in plans if p["repair_of_step_id"] and p["status"] != "shipped"]
    if repairs:
        return [p for p in open_ if p["repair_of_step_id"]]
    work_left = any(p["status"] != "shipped" for p in plans if not is_root_plan(p))
    return [p for p in open_
            if all(status.get(d) == "shipped" for d in json.loads(p["depends_on"]))
            and not (p["verification"] and work_left)]  # verification checks the merged whole
