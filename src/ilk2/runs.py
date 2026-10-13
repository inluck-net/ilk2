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

NOT_A_RESTART = {"shipped", "parked", "drained", "cancelled", "quota", "api", "waiting-lease"}


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


