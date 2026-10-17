"""Improvement intake (docs/07 §7.4.3, D7, D14): findings about a project, from postmortems,
reviews or people, one row per (project, fingerprint), so a recurring finding is one row whose
hits grow. It replaces V1's ~/.ilk-data/ilk-skills-improvements/candidates.json.

Each project reads its own rows (`ilk2 improvement list --project P`). Rows for the ilk2 project
reach RSI only once a person marks them accepted, and rank below every other RSI input
(rsi.WEIGHTS["improvement"]): a postmortem's conclusion never changes ilk2 unread. Every change
is journalled in `transitions`.
"""

from __future__ import annotations

import hashlib
import json

from . import clock

SOURCES = {"postmortem", "review", "human", "v1-backlog"}
STATUSES = {"open", "accepted", "rejected", "done"}
EVIDENCE_KEPT = 20


class IntakeError(ValueError):
    pass


def row_id(project: str, fingerprint: str) -> str:
    return "imp-" + hashlib.sha256(f"{project}\0{fingerprint}".encode()).hexdigest()[:12]


def add(s, project: str, source: str, fingerprint: str, title: str, evidence: dict,
        actor: str = "intake") -> dict:
    """Record one finding. The same fingerprint again adds a hit and its evidence; a rejected or
    done row keeps its status (a person decided), an open or accepted one stays as it is."""
    if source not in SOURCES:
        raise IntakeError(f"source must be one of {sorted(SOURCES)}")
    if not fingerprint.strip() or not title.strip():
        raise IntakeError("fingerprint and title are required")
    if s.one("SELECT 1 FROM projects WHERE id = ?", project) is None:
        raise IntakeError(f"unknown project {project!r}")
    now = clock.wall()
    rid = row_id(project, fingerprint)
    ev = {**evidence, "source": source, "at": now}
    row = s.one("SELECT * FROM improvements WHERE id = ?", rid)
    if row is None:
        s.insert("improvements", {"id": rid, "project_id": project, "source": source,
                                  "fingerprint": fingerprint, "title": title[:200],
                                  "evidence": json.dumps([ev]), "hits": 1, "status": "open",
                                  "created_at": now, "updated_at": now})
        s.update("improvements", rid, {"status": "open"}, actor=actor, reason=f"added from {source}")
        return {"id": rid, "new": True, "hits": 1, "status": "open"}
    kept = (json.loads(row["evidence"]) + [ev])[-EVIDENCE_KEPT:]
    s.update("improvements", rid, {"evidence": json.dumps(kept), "hits": row["hits"] + 1,
                                   "updated_at": now}, actor=actor, reason=f"seen again from {source}")
    return {"id": rid, "new": False, "hits": row["hits"] + 1, "status": row["status"]}


def set_status(s, rid: str, status: str, actor: str, reason: str) -> dict:
    if status not in STATUSES:
        raise IntakeError(f"status must be one of {sorted(STATUSES)}")
    if not reason.strip():
        raise IntakeError("a reason is required")
    if s.one("SELECT 1 FROM improvements WHERE id = ?", rid) is None:
        raise IntakeError(f"unknown improvement {rid}")
    s.update("improvements", rid, {"status": status, "updated_at": clock.wall()}, actor=actor,
             reason=reason)
    return dict(s.get("improvements", rid))


def rows(s, project: str | None = None, status: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM improvements WHERE 1=1", []
    if project:
        sql += " AND project_id = ?"
        args.append(project)
    if status:
        sql += " AND status = ?"
        args.append(status)
    out = []
    for r in s.all(sql + " ORDER BY hits DESC, updated_at DESC", *args):
        d = dict(r)
        d["evidence"] = json.loads(d["evidence"])
        out.append(d)
    return out
