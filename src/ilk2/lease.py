"""The execution root's writer lease (docs/02 §2.4.2).

Holder is `driver`, `consumer`, `consumer-expired` or `released`. Every change is a
compare-and-set on the generation. Only the holder writes the root's branch. Consumer expiry
fences the driver instead of releasing (Codex review 3).
"""

from __future__ import annotations

from . import clock
from .store import Conflict, Store


def get(s: Store, root: str):
    return s.one("SELECT * FROM root_leases WHERE root = ?", root)


def init_driver(s: Store, root: str, head: str) -> int:
    """Create the lease for a new execution root, held by the driver side (batch queued)."""
    with s.tx() as c:
        row = c.execute("SELECT generation FROM root_leases WHERE root = ?", (root,)).fetchone()
        if row:
            return row["generation"]
        c.execute("INSERT INTO root_leases(root, holder, owner, generation, expected_head, "
                  "accepted_head, updated_at) VALUES (?, 'driver', 'ilk2', 1, ?, ?, ?)",
                  (root, head, head, clock.wall()))
        return 1


def _cas(s: Store, root: str, generation: int, holder_from: set[str], holder_to: str,
         owner: str | None = None, **fields) -> int:
    with s.tx() as c:
        row = c.execute("SELECT holder, generation FROM root_leases WHERE root = ?", (root,)).fetchone()
        if row is None:
            raise Conflict(f"no lease for {root}")
        if row["generation"] != generation:
            raise Conflict(f"lease generation is {row['generation']}, not {generation}")
        if row["holder"] not in holder_from:
            raise Conflict(f"lease holder is {row['holder']}, expected {sorted(holder_from)}")
        sets = {"holder": holder_to, "owner": owner, "generation": generation + 1,
                "updated_at": clock.wall(), **fields}
        assigns = ", ".join(f"{k} = ?" for k in sets)
        cur = c.execute(f"UPDATE root_leases SET {assigns} WHERE root = ? AND generation = ?",
                        (*sets.values(), root, generation))
        if cur.rowcount != 1:
            raise Conflict("lease changed concurrently")
        return generation + 1


def driver_release(s: Store, root: str, accepted_head: str) -> int:
    row = get(s, root)
    return _cas(s, root, row["generation"], {"driver"}, "released", "ilk2",
                accepted_head=accepted_head, expected_head=accepted_head, expiry_wall=None)


def driver_acquire(s: Store, root: str, generation: int) -> int:
    return _cas(s, root, generation, {"released"}, "driver", "ilk2", expiry_wall=None)


def _max_lease(s: Store, root: str) -> float | None:
    from . import batches
    b = s.one("SELECT project_id FROM batches WHERE execution_root = ? ORDER BY created_at DESC "
              "LIMIT 1", root)
    return batches.project_config(s, b["project_id"]).max_lease_s if b else None


def _check_length(s: Store, root: str, length_s: float) -> None:
    cap = _max_lease(s, root)
    if cap is not None and length_s > cap:
        raise Conflict(f"lease length {length_s:.0f}s exceeds the project's max_lease_s {cap:.0f}s (R2)")


def consumer_acquire(s: Store, root: str, generation: int, expected_head: str, owner: str,
                     actual_head: str, length_s: float) -> int:
    from . import batches
    b = s.one("SELECT project_id FROM batches WHERE execution_root = ? ORDER BY created_at DESC "
              "LIMIT 1", root)
    if b and batches.project_config(s, b["project_id"]).shadow_of:
        raise Conflict(f"{root} belongs to a shadow project: never leased to a consumer (§4.2)")
    _check_length(s, root, length_s)
    if expected_head != actual_head:
        raise Conflict(f"root head is {actual_head[:12]}, not the expected {expected_head[:12]}")
    return _cas(s, root, generation, {"released"}, "consumer", owner,
                expected_head=expected_head, expiry_wall=clock.wall() + length_s)


def consumer_renew(s: Store, root: str, generation: int, length_s: float) -> int:
    _check_length(s, root, length_s)
    row = get(s, root)
    return _cas(s, root, generation, {"consumer"}, "consumer", row["owner"],
                expiry_wall=clock.wall() + length_s)


def consumer_release(s: Store, root: str, generation: int, new_head: str) -> int:
    row = get(s, root)
    return _cas(s, root, generation, {"consumer", "consumer-expired"}, "released", row["owner"],
                expected_head=new_head, expiry_wall=None)


def expire_due(s: Store) -> list[str]:
    """consumer -> consumer-expired for leases past expiry (still fences the driver)."""
    out = []
    for r in s.all("SELECT * FROM root_leases WHERE holder = 'consumer' AND expiry_wall < ?",
                   clock.wall()):
        try:
            _cas(s, r["root"], r["generation"], {"consumer"}, "consumer-expired", r["owner"],
                 expiry_wall=r["expiry_wall"])
            out.append(r["root"])
        except Conflict:
            pass
    return out


def recover(s: Store, root: str, generation: int, new_head: str) -> int:
    """R1: after the consumer's own reap crashed holding the lease. Accepted only when no process
    has its working directory inside the root."""
    from pathlib import Path
    from . import procs
    found = procs.strays(Path(root))
    if found:
        raise Conflict(f"processes are still inside {root}: {found} (R1)")
    return _cas(s, root, generation, {"consumer-expired"}, "released", None,
                expected_head=new_head, expiry_wall=None)


def holder_is_driver(s: Store, root: str) -> bool:
    row = get(s, root)
    return row is not None and row["holder"] == "driver"


def set_accepted_head(s: Store, root: str, sha: str) -> None:
    with s.tx() as c:
        c.execute("UPDATE root_leases SET accepted_head = ?, expected_head = ?, updated_at = ? "
                  "WHERE root = ?", (sha, sha, clock.wall(), root))
