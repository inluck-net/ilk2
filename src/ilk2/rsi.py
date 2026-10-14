"""RSI: ilk2 finds its own improvements (docs/06 "RSI", Chad Oct 14: "Reviewed RSI first").

The finder turns ilk2's own records into ranked candidates in `rsi_candidates`, one row per
fingerprint, so a recurring defect is one candidate whose hits grow. Sources, by weight:

  request       Chad's requests in docs/rsi/requests.json (protected kernel)       100
  engine-error  a driver that ended in an error (driver-error.txt)                    50
  stall         a run the supervisor called stalled or lost                           40
  needs-human   a needs_human reason other than a step's own red attempts             30
  refusal       a refusal reason that recurs (3+), other than capacity and leases     20
  report        a shipped batch whose base phase took longer than its workers         10

Nothing here plans or submits: the planner and the trigger read the candidates (rsi.plan,
rsi.tick). Finding is idempotent: run it as often as you like.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import clock, paths

WINDOW_S = 7 * 24 * 3600   # judgment call: a week of records; wrong if defects recur more slowly
WEIGHTS = {"request": 100, "engine-error": 50, "stall": 40, "needs-human": 30, "refusal": 20,
           "report": 10}
# A step's own red attempts are the consumer's work, not ilk2's; capacity and leases are the
# host's state, not defects.
NOT_DEFECTS_NH = {"attempts"}
NOT_DEFECTS_REFUSAL = {"project-limit", "host-load", "host-budget", "root-lease", "waiting-lease",
                       "waiting-api", "quota", "reserve"}
STALL_REASONS = re.compile(r"^(stalled|lost)")
OPEN = ("open",)


@dataclass
class Candidate:
    id: str
    source: str
    title: str
    evidence: list[dict] = field(default_factory=list)


def normalize(text: str) -> str:
    """A message without what differs between occurrences: paths, ids, numbers, colours."""
    t = re.sub(r"\x1b\[[0-9;]*m|\[[0-9;]+m", "", text or "")
    t = re.sub(r"(/[\w.@+-]+)+", "<path>", t)
    t = re.sub(r"\b[0-9a-f]{7,}\b", "<id>", t)
    t = re.sub(r"\b[a-z]-[0-9a-f]{6,}\b", "<id>", t)
    t = re.sub(r"\d+(\.\d+)?", "<n>", t)
    return " ".join(t.split())[:200]


def _fp(source: str, key: str) -> str:
    import hashlib
    return f"{source}:{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def requests(repo: Path | None) -> list[Candidate]:
    """Chad's requests: docs/rsi/requests.json, [{"id", "title", "acceptance"}]. The file is in
    the protected kernel (RSI batches cannot edit it). Unparseable is an error, never 'none'."""
    if repo is None:
        return []
    f = repo / "docs" / "rsi" / "requests.json"
    if not f.exists():
        return []
    rows = json.loads(f.read_text())  # fails closed: a broken file stops the finder
    out = []
    for r in rows:
        if not r.get("acceptance"):
            continue  # a request with no acceptance check is not planned (V1 RSI rule 5)
        out.append(Candidate(f"request:{r['id']}", "request", r["title"],
                             [{"request": r["id"], "acceptance": r["acceptance"]}]))
    return out


def find(s, repo: Path | None = None, now: float | None = None) -> list[Candidate]:
    now = clock.wall() if now is None else now
    since = now - WINDOW_S
    found: dict[str, Candidate] = {}

    def add(source: str, key: str, title: str, ev: dict) -> None:
        cid = _fp(source, key)
        found.setdefault(cid, Candidate(cid, source, title)).evidence.append(ev)

    for r in s.all("SELECT id, batch_id, terminal_reason, ended_at FROM runs WHERE created_at > ?",
                   since):
        reason = r["terminal_reason"] or ""
        err = paths.run_dir(r["id"]) / "driver-error.txt"
        if reason == "error" or (err.exists() and err.stat().st_size):
            last = err.read_text().strip().splitlines()[-1] if err.exists() and err.stat().st_size \
                else reason
            sig = normalize(last)
            add("engine-error", sig, f"driver error: {sig[:90]}",
                {"run": r["id"], "batch": r["batch_id"], "at": r["ended_at"], "error": last[:300]})
        elif STALL_REASONS.match(reason):
            add("stall", reason, f"runs end {reason}",
                {"run": r["id"], "batch": r["batch_id"], "at": r["ended_at"]})
    for r in s.all("SELECT * FROM needs_human WHERE created_at > ?", since):
        if r["reason"] in NOT_DEFECTS_NH:
            continue
        add("needs-human", r["reason"], f"needs_human: {r['reason']}",
            {"batch": r["batch_id"], "at": r["created_at"], "detail": r["detail"][:300]})
    by_reason: dict[str, list] = {}
    for r in s.all("SELECT * FROM refusals WHERE opened_at > ?", since):
        if r["reason"] not in NOT_DEFECTS_REFUSAL:
            by_reason.setdefault(r["reason"], []).append(r)
    for reason, rows in by_reason.items():
        if len(rows) >= 3:  # judgment call: a refusal that recurs; one-offs are the host's state
            for r in rows:
                add("refusal", reason, f"refusal recurs: {reason}",
                    {"batch": r["batch_id"], "at": r["opened_at"], "detail": r["detail"][:300]})
    from . import report
    for b in s.all("SELECT id, project_id FROM batches WHERE status = 'shipped' AND "
                   "created_at > ?", since):
        ph = report.phase_minutes(s, b["id"])
        if ph.get("base", 0) > max(ph.get("dispatch", 0), 1.0):
            add("report", f"base>dispatch:{b['project_id']}",
                f"{b['project_id']}: the base phase takes longer than the workers",
                {"batch": b["id"], "base_min": ph.get("base"), "dispatch_min": ph.get("dispatch")})
    out = list(found.values()) + requests(repo)
    return sorted(out, key=lambda c: -score(c.source, len(c.evidence)))


def score(source: str, hits: int) -> float:
    return round(WEIGHTS[source] + 5 * math.log2(1 + hits), 2)


def record(s, cands: list[Candidate], now: float | None = None) -> dict[str, int]:
    """Upsert the candidates. A shelved one reopens when it gains 2+ hits since it was shelved;
    a done one stays done (its evidence predates the fix)."""
    now = clock.wall() if now is None else now
    new = reopened = 0
    for c in cands:
        row = s.one("SELECT * FROM rsi_candidates WHERE id = ?", c.id)
        hits = len(c.evidence)
        ev = json.dumps(c.evidence[-20:])
        if row is None:
            s.insert("rsi_candidates", {"id": c.id, "source": c.source, "title": c.title,
                                        "evidence": ev, "hits": hits, "score": score(c.source, hits),
                                        "status": "open", "first_seen": now, "last_seen": now})
            new += 1
            continue
        fields = {"evidence": ev, "score": score(c.source, hits), "last_seen": now}
        if row["status"] != "shelved":
            fields["hits"] = hits
        elif hits >= row["hits"] + 2:  # a shelved row keeps its hits from when it was shelved
            fields |= {"status": "open", "hits": hits}
            reopened += 1
        s.update("rsi_candidates", c.id, fields, actor="rsi", reason="found")
    return {"found": len(cands), "new": new, "reopened": reopened}


def candidates(s, status: tuple[str, ...] | None = OPEN) -> list:
    sql = "SELECT * FROM rsi_candidates"
    if status:
        sql += f" WHERE status IN ({','.join('?' * len(status))})"
    return s.all(sql + " ORDER BY score DESC, last_seen DESC", *(status or ()))
