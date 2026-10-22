"""Delivering a batch as a pull request (docs/10 §10.3).

ilk2 records that a batch wants a PR and tracks the request; an opted-in deliverer does the root
lease, base merge, pre-push, push and PR. ilk2 never pushes, and knows a deliverer only as an
opaque name it stores and compares (Chad, Oct 22: ilk2 does not recognise which consumers exist).
"""

from __future__ import annotations

from . import clock
from .store import Conflict, Store

KINDS = ("pr",)
STATES = ("waiting", "owed", "claimed", "done", "failed", "dropped")
LISTED = ("owed", "claimed")  # what `deliver list` shows without --state


class Rejected(ValueError):
    """A request the protocol refuses outright (exit 2)."""


def request_problems(cfg, deliver: str | None, base: str | None, draft: bool) -> list[str]:
    if deliver is None:
        return ["--deliver-base and --deliver-draft need --deliver pr"] if base or draft else []
    if deliver not in KINDS:
        return [f"--deliver kind must be one of {list(KINDS)}, not {deliver!r}"]
    if not cfg.deliverer:
        return ["--deliver pr needs the project's deliverer setting"]
    return []


def insert(s: Store, batch_id: str, cfg, kind: str, base: str, draft: bool) -> None:
    """Called in the submit transaction, so a batch and its request are made together."""
    s.insert("deliveries", {"id": batch_id, "kind": kind, "base": base, "draft": int(draft),
                            "deliverer": cfg.deliverer, "status": "waiting",
                            "updated_at": clock.wall()})


def get(s: Store, batch_id: str) -> dict | None:
    r = s.one("SELECT * FROM deliveries WHERE id = ?", batch_id)
    if r is None:
        return None
    return {"kind": r["kind"], "base": r["base"], "draft": bool(r["draft"]),
            "deliverer": r["deliverer"], "state": r["status"], "pr_url": r["pr_url"],
            "detail": r["detail"], "claimed_by": r["claimed_by"], "claimed_at": r["claimed_at"],
            "updated_at": r["updated_at"], "version": r["version"]}


def _move(s: Store, batch_id: str, to: str, frm: str, reason: str, **fields) -> dict:
    s.transition("deliveries", batch_id, to, actor="delivery", reason=reason, expect_from={frm},
                 fields={**fields, "updated_at": clock.wall()})
    return get(s, batch_id)


def on_shipped(s: Store, batch_id: str) -> dict | None:
    """waiting → owed, before batch.shipped is emitted, so the event carries the owed request."""
    if get(s, batch_id) is None:
        return None
    return _move(s, batch_id, "owed", "waiting", "batch shipped", owed_at=clock.wall())


def on_cancelled(s: Store, batch_id: str) -> None:
    d = get(s, batch_id)
    if d and d["state"] == "waiting":
        _move(s, batch_id, "dropped", "waiting", "batch cancelled")


def _known(s: Store, batch_id: str) -> dict:
    d = get(s, batch_id)
    if d is None:
        raise Rejected(f"no delivery was asked for batch {batch_id}"
                       if s.one("SELECT 1 FROM batches WHERE id = ?", batch_id)
                       else f"no such batch {batch_id}")
    return d


def claim(s: Store, batch_id: str, by: str) -> dict:
    d = _known(s, batch_id)
    if by != d["deliverer"]:
        raise Conflict(f"{by!r} is not the deliverer of batch {batch_id}")
    if d["state"] != "owed":
        raise Conflict(f"delivery of {batch_id} is {d['state']}"
                       + (f" (claimed by {d['claimed_by']})" if d["claimed_by"] else ""))
    out = _move(s, batch_id, "claimed", "owed", f"claimed by {by}", claimed_by=by,
                claimed_at=clock.wall())
    s.clear_needs_human(batch_id, {"delivery-owed"})
    return out


def _claimed_by(d: dict, batch_id: str, by: str) -> None:
    if d["state"] != "claimed":
        raise Conflict(f"delivery of {batch_id} is {d['state']}, not claimed")
    if by != d["claimed_by"]:
        raise Conflict(f"delivery of {batch_id} is claimed by {d['claimed_by']}, not {by}")


def done(s: Store, batch_id: str, by: str, pr_url: str) -> dict:
    d = _known(s, batch_id)
    if not pr_url.startswith("https://"):
        raise Rejected(f"--pr must be an https:// URL, not {pr_url!r}")
    _claimed_by(d, batch_id, by)
    out = _move(s, batch_id, "done", "claimed", f"delivered by {by}", pr_url=pr_url, detail=None)
    s.clear_needs_human(batch_id, {"delivery-failed"})
    return out


def _fail(s: Store, batch_id: str, detail: str, reason: str) -> dict:
    out = _move(s, batch_id, "failed", "claimed", reason, detail=detail[:2000])
    s.open_needs_human("delivery-failed", f"delivery of {batch_id} failed: {detail[:1500]}",
                       f"fix the cause, then: ilk2 deliver retry --batch {batch_id}",
                       batch_id=batch_id)
    return out


def failed(s: Store, batch_id: str, by: str, detail: str) -> dict:
    d = _known(s, batch_id)
    _claimed_by(d, batch_id, by)
    return _fail(s, batch_id, detail, f"failed by {by}")


def retry(s: Store, batch_id: str) -> dict:
    d = _known(s, batch_id)
    if d["state"] != "failed":
        raise Conflict(f"delivery of {batch_id} is {d['state']}, not failed")
    out = _move(s, batch_id, "owed", "failed", "retry", claimed_by=None, claimed_at=None,
                owed_at=clock.wall())
    s.clear_needs_human(batch_id, {"delivery-failed"})
    return out


def listing(s: Store, by: str | None, state: str | None) -> list[dict]:
    """What a deliverer needs per delivery without a second call (§10.3.4)."""
    from . import lease
    if state is not None and state not in STATES:
        raise Rejected(f"--state must be one of {list(STATES)}")
    states = (state,) if state else LISTED
    q = (f"SELECT d.id FROM deliveries d WHERE d.status IN ({', '.join('?' for _ in states)})"
         + (" AND d.deliverer = ?" if by else "") + " ORDER BY d.updated_at")
    out = []
    for r in s.all(q, *states, *((by,) if by else ())):
        b = s.get("batches", r["id"])
        lr = lease.get(s, b["execution_root"])
        out.append({"batch": b["id"], "project": b["project_id"], "branch": b["branch"],
                    "execution_root": b["execution_root"],
                    "accepted_head": lr["accepted_head"] if lr else None,
                    "root_lease": {"holder": lr["holder"], "generation": lr["generation"]} if lr
                    else None,
                    "delivery": get(s, b["id"])})
    return out


def tick(s: Store, now: float) -> None:
    """The bounds (I11): an owed request nobody claims pages a person; a claim held too long
    fails, so a deliverer that died mid-delivery is visible and retryable."""
    from . import batches
    for r in s.all("SELECT d.id, d.status, d.owed_at, d.claimed_at, d.claimed_by, b.project_id "
                   "FROM deliveries d JOIN batches b ON b.id = d.id "
                   "WHERE d.status IN ('owed', 'claimed')"):
        cfg = batches.project_config(s, r["project_id"])
        if r["status"] == "owed" and now - (r["owed_at"] or now) > cfg.deliver_owed_after_s:
            s.open_needs_human("delivery-owed",
                               f"batch {r['id']} shipped and asked for a PR, and its deliverer "
                               f"has not claimed it for {cfg.deliver_owed_after_s:.0f} s",
                               "check that the deliverer is running and opted in",
                               batch_id=r["id"])
        elif r["status"] == "claimed" and now - (r["claimed_at"] or now) > cfg.deliver_claim_max_s:
            try:
                _fail(s, r["id"], f"claimed by {r['claimed_by']} for more than "
                      f"{cfg.deliver_claim_max_s:.0f} s", "claim expired")
            except Conflict:
                pass  # the deliverer closed it in the meantime
