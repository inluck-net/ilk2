"""RSI: ilk2 finds its own improvements (docs/06 "RSI", Chad Oct 14: "Reviewed RSI first").

The finder turns ilk2's own records into ranked candidates in `rsi_candidates`, one row per
fingerprint, so a recurring defect is one candidate whose hits grow. Sources, by weight:

  request       Chad's requests in docs/rsi/requests.json (protected kernel)       100
  engine-error  a driver that ended in an error (driver-error.txt)                    50
  stall         a run the supervisor called stalled or lost                           40
  needs-human   a needs_human reason other than a step's own red attempts             30
  gate-cap      a gate whose full run keeps hitting its time cap                      25
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
WEIGHTS = {"request": 100, "engine-error": 50, "stall": 40, "needs-human": 30, "gate-cap": 25,
           "refusal": 20, "report": 10}
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
    # A gate whose full run hits its cap costs a second full run (gates.measure's transient
    # re-run) every time: on Oct 14 a kira convex suite ran near its 900 s cap twice over.
    hits: dict[tuple[str, str], list] = {}
    for r in s.all("SELECT e.batch_id, e.kind, e.payload, e.at, b.project_id FROM events e "
                   "JOIN batches b ON b.id = e.batch_id WHERE e.at > ? AND e.kind IN "
                   "('gate.timeout', 'base.measured') ORDER BY e.cursor", since):
        payload = json.loads(r["payload"] or "{}")
        name = payload.get("gate")
        if not name:
            continue
        if r["kind"] == "base.measured" and (payload.get("timeouts") or 0) < 1:
            continue  # a measurement that ran once within its cap is not a cap problem
        hits.setdefault((r["project_id"], name), []).append(
            {"batch": r["batch_id"], "gate": name, "at": r["at"], "kind": r["kind"]})
    for (project, name), evs in hits.items():
        if len(evs) < 2:
            continue  # judgment call: one capped run is noise; two is a pattern
        for ev in evs:
            add("gate-cap", f"{project}:{name}",
                f"{project}: gate {name} hits its time cap", ev)
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


# -- the planner and the trigger ------------------------------------------------------------

PROJECT = "ilk2"               # the ilk2 project that builds ilk2
DAILY_CAP = 3                  # judgment call: the review queue is the limit; wrong if reviews keep up easily
REVIEW_CAP = 2                 # unreviewed shipped RSI branches before the trigger waits
PLAN_CAP_S = 30 * 60           # one planning session
SUITE_GATE = {"name": "suite", "cap_s": 900,
              "command": [".venv/bin/python", "-m", "pytest", "-q", "-n", "4", "-p",
                          "no:cacheprovider", "tests"]}
SPEC_FILE = ".rsi-spec.json"

PLANNER_BRIEF = """You are the planner for ilk2, a loop engine that is improving itself. You are in
a read-only checkout of ilk2's main branch. Read whatever you need (start with AGENTS.md,
docs/README.md and the code the evidence points at). Do NOT change any file except the one
named below.

The candidate, found by ilk2 in its own records:

  {title}

  evidence (most recent last): {evidence}

First decide whether this is still a defect on main. The evidence may predate a fix: search the
code and git log for it. Then write exactly one JSON object to the file {spec_file} at the root of
this checkout, and stop. One of:

  {{"already_fixed": "<commit or file:line that fixes it, and why>"}}
  {{"not_a_defect": "<why: for example the consumer's or the host's state, not ilk2's>"}}
  {{"title": "<one line>",
    "instructions": "<what a worker must change and why, with file:line references, the design
                      rule it follows, and the test it adds; self-contained: the worker sees only
                      this text and the repository>",
    "scope": ["<files the worker may change>"],
    "must_pass": ["tests/<file>.py::<test name>, a new test that fails before the change"]}}

Rules for a plan: one step; a change that a reviewer can check in ten minutes; it must add a
test that fails without the change; it updates docs/02-architecture.md or docs/06-open-items.md
if the design changes. These paths are the protected kernel and must not be in scope:
{kernel}. The gate is fixed: ilk2's full suite. Do not propose changing gates, tests' strength,
bounds, or the RSI rules themselves."""


def plan(s, cand, repo: Path, cfg, cap_s: float = PLAN_CAP_S) -> dict:
    """One planning session for one candidate. Returns {"spec": <batch dict>} or {"shelve":
    reason} or {"done": reason}. The planner writes only a step's text: ilk2 sets the gate."""
    from . import batches, bounds, gitops, worker
    stamp = f"{cand['id'].replace(':', '-')}-{int(clock.wall())}"
    d = paths.data() / "rsi" / stamp
    wt = d / "wt"
    gitops.add_worktree(repo, wt, gitops.rev(repo, "main") or gitops.head(repo))
    try:
        wcfg = worker.WorkerConfig(claude_bin=cfg.claude_bin, max_turns=cfg.max_turns,
                                   sandbox=cfg.sandbox, sandbox_allow=cfg.sandbox_allow)
        files = worker.AttemptFiles(d / "attempt")
        gate = cfg.rsi_gate or SUITE_GATE
        worker.prepare(wcfg, files, wt, [gate["command"]])
        prompt = PLANNER_BRIEF.format(title=cand["title"], evidence=cand["evidence"][:6000],
                                      spec_file=SPEC_FILE, kernel=", ".join(cfg.deny_paths))
        b = bounds.load()
        run = worker.dispatch(wcfg, files, wt, prompt)
        outcome = run.wait(cap_s, b.term_grace, b.kill_grace)
        out = wt / SPEC_FILE
        if not out.exists():
            return {"shelve": f"the planner wrote no {SPEC_FILE} (session {outcome})"}
        try:
            got = json.loads(out.read_text())
        except ValueError as e:
            return {"shelve": f"{SPEC_FILE} is not JSON: {e}"}
        changed = [p for p in gitops.changed_paths(wt) if p != SPEC_FILE]
        if changed:
            return {"shelve": f"the planner changed files it must not: {changed[:10]}"}
        if "already_fixed" in got:
            return {"done": f"already fixed: {got['already_fixed']}"[:1000]}
        if "not_a_defect" in got:
            return {"done": f"not a defect: {got['not_a_defect']}"[:1000]}
        problems = []
        for k in ("title", "instructions", "scope", "must_pass"):
            if not got.get(k):
                problems.append(f"no {k}")
        if len(got.get("instructions") or "") < 200:
            problems.append("instructions under 200 characters")
        bad = [p for p in got.get("scope") or [] if batches.path_matches(p, cfg.deny_paths)]
        if bad:
            problems.append(f"scope names the protected kernel: {bad}")
        if problems:
            return {"shelve": "plan rejected: " + "; ".join(problems)}
        return {"spec": {"plans": [{"slug": re.sub(r"[^a-z0-9]+", "-", got["title"].lower())[:40]
                                    .strip("-") or "rsi", "steps": [{
                                        "title": got["title"], "instructions": got["instructions"],
                                        "scope": list(got["scope"]),
                                        "must_pass": list(got["must_pass"]),
                                        "gates": [gate]}]}]}}
    finally:
        gitops.remove_worktree(repo, wt)


def pending(s) -> list:
    """Shipped RSI batches whose branch is not on main yet: the review queue."""
    from . import gitops
    proj = s.one("SELECT repo_root FROM projects WHERE id = ?", PROJECT)
    if not proj:
        return []
    repo = Path(proj["repo_root"])
    main = gitops.rev(repo, "main")
    out = []
    for b in s.all("SELECT * FROM batches WHERE project_id = ? AND status = 'shipped' AND "
                   "consumer_ref LIKE 'rsi-%' ORDER BY created_at", PROJECT):
        head = gitops.rev(repo, b["branch"])
        if head and main and not gitops.is_ancestor(repo, head, main):
            out.append(b)
    return out


def tick(s, now: float | None = None) -> dict:
    """Find, then, if the trigger may, plan and submit the top open candidate. Every exit says
    why it did nothing (a quiet trigger must be tellable from a stuck one)."""
    from . import batches
    now = clock.wall() if now is None else now
    proj = s.one("SELECT * FROM projects WHERE id = ?", PROJECT)
    if not proj:
        return {"did": "nothing", "why": f"no project {PROJECT!r}"}
    cfg = batches.project_config(s, PROJECT)
    if cfg.retired or not cfg.rsi_auto:
        return {"did": "nothing", "why": "rsi_auto is off for the ilk2 project"}
    repo = Path(proj["repo_root"])
    for r in s.all("SELECT id FROM rsi_candidates WHERE status = 'planning' AND planned_at < ?",
                   now - 2 * PLAN_CAP_S):
        # a tick that died mid-plan: the candidate goes back to open, never stuck
        s.update("rsi_candidates", r["id"], {"status": "open"}, actor="rsi", reason="stale planning")
    rec = record(s, find(s, repo, now), now)
    if s.one("SELECT 1 FROM rsi_candidates WHERE status = 'planning'"):
        # one plan at a time: a manual tick and the supervisor's would plan two candidates at
        # once (Oct 14, the first live tick); a stale one was reopened above
        return {"did": "nothing", "why": "a plan is in progress", **rec}
    if s.one("SELECT 1 FROM batches WHERE project_id = ? AND status IN ('queued', 'active', "
             "'parked', 'blocked')", PROJECT):
        return {"did": "nothing", "why": "an ilk2 batch is in flight", **rec}
    waiting = pending(s)
    if len(waiting) >= REVIEW_CAP:
        return {"did": "nothing", "why": f"{len(waiting)} RSI branches wait for review", **rec}
    today = s.one("SELECT COUNT(*) n FROM batches WHERE project_id = ? AND consumer_ref LIKE "
                  "'rsi-auto-%' AND created_at > ?", PROJECT, now - 86400)["n"]
    if today >= DAILY_CAP:
        return {"did": "nothing", "why": f"daily cap {DAILY_CAP} reached", **rec}
    top = next(iter(candidates(s)), None)
    if top is None:
        return {"did": "nothing", "why": "no open candidate", **rec}
    s.update("rsi_candidates", top["id"], {"status": "planning", "planned_at": now}, actor="rsi",
             reason="tick")
    s.event("rsi.planning", {"candidate": top["id"], "title": top["title"]})
    try:
        res = plan(s, top, repo, cfg)
    except Exception as e:  # noqa: BLE001 - a failed plan shelves the candidate, never wedges it
        res = {"shelve": f"planning failed: {type(e).__name__}: {e}"[:500]}
    if "spec" in res:
        from . import plans as plans_mod
        n = s.one("SELECT COUNT(*) n FROM batches WHERE consumer_ref LIKE 'rsi-auto-%'")["n"] + 1
        try:
            bid = batches.submit(s, PROJECT, plans_mod.from_dict(res["spec"]),
                                 consumer_ref=f"rsi-auto-{n}", meta={"rsi_candidate": top["id"]})
        except batches.SubmitRejected as e:
            res = {"shelve": "submit rejected: " + "; ".join(e.problems)[:500]}
        else:
            s.update("rsi_candidates", top["id"], {"status": "submitted", "batch_id": bid},
                     actor="rsi", reason="submitted")
            s.event("rsi.submitted", {"candidate": top["id"], "batch": bid}, batch_id=bid)
            return {"did": "submitted", "batch": bid, "candidate": top["id"], **rec}
    status = "done" if "done" in res else "shelved"
    note = res.get("done") or res.get("shelve")
    s.update("rsi_candidates", top["id"], {"status": status, "note": note}, actor="rsi",
             reason=status)
    s.event(f"rsi.{status}", {"candidate": top["id"], "note": note})
    return {"did": status, "candidate": top["id"], "note": note, **rec}
