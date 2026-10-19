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
  improvement   an intake row for project ilk2 that a person marked accepted          5
                (ilk2 improvement; postmortems and reviews, docs/07 §7.4.3, D14)

Nothing here plans or submits: the planner and the trigger read the candidates (rsi.plan,
rsi.tick). Finding is idempotent: run it as often as you like.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import clock, paths
from .store import StoreError

WINDOW_S = 7 * 24 * 3600   # judgment call: a week of records; wrong if defects recur more slowly
WEIGHTS = {"request": 100, "goal": 90, "engine-error": 50, "stall": 40, "needs-human": 30, "gate-cap": 25,
           "refusal": 20, "report": 10, "improvement": 5}
# A step's own red attempts are the consumer's work, not ilk2's; capacity and leases are the
# host's state, not defects.
# rsi-review and rsi-goal: RSI's own holds for a person, not defects. On Oct 19 an rsi-goal row
# (a stopped goal, cleared only by editing docs/rsi/goals.json) became a candidate and took a
# 30-min planning session.
NOT_DEFECTS_NH = {"attempts", "rsi-review", "rsi-goal"}
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
    hits: int | None = None     # when evidence is a sample (another host's): the full count

    def count(self) -> int:
        return self.hits if self.hits is not None else len(self.evidence)


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


ISSUES = {"repo": "inluck-net/ilk2", "label": "rsi:req", "author": "chad007"}
# launchd's PATH lacks Homebrew: a bare `gh` failed on all 144 supervisor ticks Oct 16-19 (docs/08)
GH_FALLBACKS = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh")


def gh_path() -> str:
    import shutil
    return shutil.which("gh") or next((g for g in GH_FALLBACKS if Path(g).is_file()), "gh")


def issues(s) -> list[Candidate]:
    """issues() for a direct caller: a failure is an event and no issues (see _issues)."""
    try:
        return _issues(s)
    except Exception as e:  # noqa: BLE001
        s.event("rsi.issues-failed", {"error": str(e)[:300]})
        return []


def _issues(s) -> list[Candidate]:
    """Chad's requests as GitHub issues (Chad, Oct 16): open issues labelled rsi:req on the ilk2
    repo, by Chad's account only, read only (RSI never comments, labels or closes). The issue
    body is the acceptance check; an empty one is skipped. An unreachable GitHub is an event,
    never a failed tick: the other sources still count (judgment call). Raises on failure."""
    import subprocess
    from .supervisor import host_config
    try:
        if not host_config().get("rsi_issues"):  # per host: the host that plans RSI reads them
            return []
    except (OSError, ValueError):
        return []
    p = subprocess.run([gh_path(), "issue", "list", "--repo", ISSUES["repo"], "--label",
                        ISSUES["label"], "--author", ISSUES["author"], "--state", "open",
                        "--json", "number,title,body,author", "--limit", "50"],
                       capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[-300:])
    rows = json.loads(p.stdout or "[]")
    out = []
    for r in rows:
        if (r.get("author") or {}).get("login") != ISSUES["author"] or not (r.get("body") or "").strip():
            continue
        out.append(Candidate(f"request:gh-{r['number']}", "request", r["title"],
                             [{"issue": r["number"], "acceptance": r["body"][:4000]}]))
    return out


def find(s, repo: Path | None = None, now: float | None = None) -> list[Candidate]:
    """Every source's candidates. A broken requests or goals file raises here (fails closed,
    never "none"); the trigger uses find_sources, which records it and runs the rest."""
    cands, _, errors = find_sources(s, repo, now)
    for name in ("records", "requests"):
        if name in errors:
            raise errors[name]
    return cands


STORE_ERRORS = (StoreError, sqlite3.Error)  # the store itself failing fails the whole tick


def find_sources(s, repo: Path | None = None, now: float | None = None):
    """(candidates, {source: "ok" or its error}, {source: exception}). Each source runs on its
    own, so one that fails is recorded and the others still count (docs/08 §8.6)."""
    now = clock.wall() if now is None else now
    status: dict[str, str] = {}
    errors: dict[str, Exception] = {}

    def run(name, fn) -> list[Candidate]:
        try:
            got = fn()
        except STORE_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - one source's failure is that source's
            status[name] = f"{type(e).__name__}: {e}"[:300]
            errors[name] = e
            s.event(f"rsi.{name}-failed" if name == "issues" else "rsi.source-failed",
                    {"source": name, "error": status[name]})
            return []
        status[name] = "ok"
        return got

    out = run("records", lambda: _records(s, now)) + run("requests", lambda: requests(repo))
    if repo is not None:  # Chad's inputs live with the ilk2 repo's host (goals: goals.tick)
        out += run("issues", lambda: _issues(s))
    return sorted(out, key=lambda c: -score(c.source, len(c.evidence))), status, errors


def _records(s, now: float) -> list[Candidate]:
    """Candidates from ilk2's own records (runs, needs_human, gates, refusals, reports, intake)."""
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
    # Accepted intake rows for ilk2, whatever their age: a person chose them (D14). Lowest weight.
    for r in s.all("SELECT * FROM improvements WHERE project_id = ? AND status = 'accepted'", PROJECT):
        add("improvement", r["fingerprint"], r["title"],
            {"improvement": r["id"], "hits": r["hits"], "at": r["updated_at"]})
    return list(found.values())


def score(source: str, hits: int) -> float:
    return round(WEIGHTS[source] + 5 * math.log2(1 + hits), 2)


def record(s, cands: list[Candidate], now: float | None = None) -> dict[str, int]:
    """Upsert the candidates. A shelved one reopens when it gains 2+ hits since it was shelved;
    a done one stays done (its evidence predates the fix)."""
    now = clock.wall() if now is None else now
    new = reopened = 0
    for c in cands:
        row = s.one("SELECT * FROM rsi_candidates WHERE id = ?", c.id)
        hits = c.count()
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


def hosts() -> list[str]:
    """Other hosts whose records feed this host's RSI: host.json "rsi_hosts" (Oct 14: kira-live
    runs on rezmac, while RSI plans and builds on chad-mbp)."""
    from .supervisor import host_config
    try:
        return list(host_config().get("rsi_hosts") or [])
    except (OSError, ValueError):
        return []


def remote(host: str) -> list[Candidate]:
    """Another host's candidates: it runs its own finder over its own store, and this host
    keeps them, tagged with the host. Their status is this host's: the other host only finds."""
    import subprocess
    from .release import REMOTE_ENV, _ssh
    cmd = (REMOTE_ENV + "I=~/.ilk2/current/venv/bin/ilk2; $I rsi find >/dev/null && "
           "$I rsi candidates --all --evidence")
    p = subprocess.run([*_ssh(), host, cmd], capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"{host}: {p.stderr.strip()[-300:]}")
    return [Candidate(f"{host}/{r['id']}", r["source"], f"[{host}] {r['title']}",
                      [{**e, "host": host} for e in r.get("evidence") or []], hits=r["hits"])
            for r in json.loads(p.stdout)["candidates"] if r["source"] in WEIGHTS]


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
GOAL_DAILY_CAP = 1             # judgment call (docs/08 §8.7): a goal round needs about a day of samples; wrong if goals should run in parallel
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

If the candidate is a goal (a metric off its target), plan the one step that most moves the
metric, grounded in the evidence and the code; say not_a_defect only if no change to ilk2 can
move it. Otherwise, first decide whether this is still a defect on main. The evidence may predate a fix: search the
code and git log for it. Then write exactly one JSON object to the file {spec_file} at the root of
this checkout, and stop. One of:

  {{"already_fixed": "<commit or file:line that fixes it, and why>"}}
  {{"not_a_defect": "<why: for example the consumer's or the host's state, not ilk2's>"}}
  {{"title": "<one line>",
    "instructions": "<what a worker must change and why, with file:line references, the design
                      rule it follows, and the test it adds; self-contained: the worker sees only
                      this text and the repository>",
    "scope": ["<files the worker may change>"],
    "must_pass": ["<test id only, e.g. tests/test_x.py::test_y>"]}}

must_pass lists only test ids (no commentary): new tests that fail before your change.

Rules for a plan: one step; a change that a reviewer can check in ten minutes; it must add a
test that fails without the change; it updates docs/02-architecture.md or docs/06-open-items.md
if the design changes. These paths are the protected kernel and must not be in scope:
{kernel}. The gate is fixed: ilk2's full suite. Do not propose changing gates, tests' strength,
bounds, or the RSI rules themselves.

Time: this session is killed at {cap_min} minutes, and a session that writes no {spec_file} is
wasted. Write it by minute {write_by}: one narrow step you can ground now beats a complete survey.
If the evidence has earlier_rounds, those planners ran out of time; their notes are what they
found, so build on them instead of redoing that reading. Scratch files go under $TMPDIR."""


def last_notes(log: Path, n: int = 4, limit: int = 900) -> str:
    """The planner's last few text messages from its stream log, newest last, for a session that
    wrote no spec: what it had found is kept for the next round (Oct 19: 3 goal rounds each
    redid the same 25 min of reading, then hit the cap)."""
    texts: list[str] = []
    try:
        lines = log.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        try:
            ev = json.loads(line).get("ev") or {}
        except ValueError:
            continue
        if ev.get("type") != "assistant":
            continue
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                texts.append(" ".join(c["text"].split()))
    return " | ".join(texts[-n:])[-limit:]


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
                                      spec_file=SPEC_FILE, kernel=", ".join(cfg.deny_paths),
                                      cap_min=round(cap_s / 60), write_by=round(cap_s * 2 / 3 / 60))
        b = bounds.load()
        run = worker.dispatch(wcfg, files, wt, prompt)
        outcome = run.wait(cap_s, b.term_grace, b.kill_grace)
        out = wt / SPEC_FILE
        if not out.exists():
            notes = last_notes(run.log_path)
            return {"shelve": f"the planner wrote no {SPEC_FILE} (session {outcome})"
                              + (f"; its last notes: {notes}" if notes else "")}
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
        # a test id only: the first planner run copied the brief's description into each entry
        # ('tests/x.py::test_y, a new test that ...'), which no test id ever matches
        ids = [re.split(r"[\s,]", str(m).strip(), maxsplit=1)[0] for m in got.get("must_pass") or []]
        bad_ids = [m for m in ids if not re.fullmatch(r"tests/[\w./-]+\.py(::[\w\[\]./-]+)*", m)]
        if bad_ids:
            problems.append(f"must_pass entries that are not test ids: {bad_ids[:5]}")
        got["must_pass"] = ids
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
    landing = {r["batch_id"]: r["status"] for r in s.all("SELECT batch_id, status FROM rsi_landings")}
    for b in s.all("SELECT * FROM batches WHERE project_id = ? AND status = 'shipped' AND "
                   "consumer_ref LIKE 'rsi-%' ORDER BY created_at", PROJECT):
        st = landing.get(b["id"])
        if st == "abandoned":
            continue  # resolved by a person: never lands, never fills the review queue
        head = gitops.rev(repo, b["branch"])
        # an interrupted merge is in main but unverified: it waits for a person (docs/08 B1)
        if st == "merge-interrupted" or (head and main and not gitops.is_ancestor(repo, head, main)):
            out.append(b)
    return out


def kind(source: str) -> str:
    """A candidate's allowance (docs/08 §8.7): Chad's requests, goals, or defects (the rest)."""
    return source if source in ("request", "goal") else "defect"


def claim(s, cand, now: float, caps: dict, round_: dict | None = None) -> str | None:
    """One atomic claim for every candidate kind (docs/08 B2): no candidate planning, the
    selected one still open and within its allowance, then open -> planning and, for a goal,
    its round. None when claimed, else why not. The store's unique indexes guard the same rules."""
    k = kind(cand["source"])
    with s.tx() as c:
        if c.execute("SELECT 1 FROM rsi_candidates WHERE status = 'planning'").fetchone():
            return "a plan is in progress"
        if k != "request" and today_count(s, now, k) >= caps[k]:
            return f"daily cap {caps[k]} reached" if k == "defect" else f"goal cap {caps[k]} reached"
        cur = c.execute("UPDATE rsi_candidates SET status = 'planning', planned_at = ?, "
                        "version = version + 1 WHERE id = ? AND status = 'open'", (now, cand["id"]))
        if cur.rowcount != 1:
            return "the candidate changed before it was claimed"
        if round_:
            cols = ", ".join(round_)
            c.execute(f"INSERT INTO rsi_goal_rounds({cols}) VALUES ({', '.join('?' * len(round_))})",
                      tuple(round_.values()))
    return None


def today_count(s, now: float, of: str = "defect") -> int:
    """RSI batches of one kind in the last 24 h that were not cancelled. The kind is the batch's
    meta.rsi_source; a batch from before it was recorded counts as a defect unless its candidate
    is a request (a host-prefixed id such as rezmac/goal:x keeps its real source this way)."""
    n = 0
    for b in s.all("SELECT snapshot FROM batches WHERE project_id = ? AND consumer_ref LIKE "
                   "'rsi-auto-%' AND status != 'cancelled' AND created_at > ?", PROJECT, now - 86400):
        try:
            meta = json.loads(b["snapshot"]).get("meta") or {}
        except (TypeError, ValueError):
            meta = {}
        src = meta.get("rsi_source")
        if src is None:
            src = "request" if str(meta.get("rsi_candidate") or "").startswith("request:") else "defect"
        n += kind(src) == of
    return n


def tick(s, now: float | None = None) -> dict:
    """One trigger pass; its outcome is kept in meta 'rsi_last_tick' for the panel. Each pass
    records rsi.tick-started, then rsi.tick-completed (with each source's result) or
    rsi.tick-failed, under one tick_id: health.py watches them from outside (docs/08 §8.6)."""
    import os
    import uuid
    tick_id = uuid.uuid4().hex[:12]
    s.event("rsi.tick-started", {"tick_id": tick_id, "pid": os.getpid()})
    sources: dict[str, str] = {}
    try:
        out = _tick(s, now, sources, {"tick_id": tick_id, "pid": os.getpid()})
    except BaseException as e:
        try:
            s.event("rsi.tick-failed", {"tick_id": tick_id, "error": f"{type(e).__name__}: {e}"[:500]})
        except Exception:  # noqa: BLE001 - the store itself may be what failed
            pass
        raise
    with s.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES ('rsi_last_tick', ?) ON CONFLICT(key) DO "
                  "UPDATE SET value = excluded.value",
                  (json.dumps({"at": clock.wall(), "did": out.get("did"), "why": out.get("why"),
                               "batch": out.get("batch")}),))
    s.event("rsi.tick-completed", {"tick_id": tick_id, "did": out.get("did"), "why": out.get("why"),
                                   "sources": sources})
    return out | {"sources": sources}


def _tick(s, now: float | None = None, sources: dict | None = None, tick_info: dict | None = None) -> dict:
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
    landed = {"did": "nothing"}
    if cfg.rsi_land:
        from . import rsi_land
        try:
            landed = rsi_land.land(s, now)
        except Exception as e:  # noqa: BLE001 - a landing error is recorded, never fatal to the tick
            s.event("rsi.land-failed", {"error": f"{type(e).__name__}: {e}"[:500]})
            landed = {"did": "failed", "why": str(e)[:300]}
        if landed.get("did") not in ("nothing", "failed"):
            s.event("rsi.land", {k: v for k, v in landed.items() if k != "did"} | {"stage": landed["did"]})
    for r in s.all("SELECT id FROM rsi_candidates WHERE status = 'planning' AND planned_at < ?",
                   now - 2 * PLAN_CAP_S):
        # a tick that died mid-plan: the candidate goes back to open, never stuck
        s.update("rsi_candidates", r["id"], {"status": "open"}, actor="rsi", reason="stale planning")
    sources = {} if sources is None else sources
    goal_defs: dict = {}
    if repo is not None:  # standing goals (docs/08 B2): reconcile rounds, derive, project
        from . import goals as goals_mod
        try:
            ok, _ = goals_mod.load(s, repo)
            goal_defs = {g["id"]: g for g in ok}
            goals_mod.tick(s, repo, now, PLAN_CAP_S)
            sources["goals"] = "ok"
        except STORE_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - one source's failure is that source's
            sources["goals"] = f"{type(e).__name__}: {e}"[:300]
            s.event("rsi.source-failed", {"source": "goals", "error": sources["goals"]})
    found, status, _ = find_sources(s, repo, now)
    sources.update(status)
    rec = record(s, found, now)
    for host in hosts():
        try:
            got = record(s, remote(host), now)
            rec = {k: rec.get(k, 0) + got.get(k, 0) for k in rec}
            sources[f"remote:{host}"] = "ok"
        except STORE_ERRORS:
            raise
        except Exception as e:  # noqa: BLE001 - an unreachable host is reported, never fatal
            sources[f"remote:{host}"] = f"{type(e).__name__}: {e}"[:300]
            s.event("rsi.remote-failed", {"host": host, "error": str(e)[:300]})
    if s.one("SELECT 1 FROM rsi_candidates WHERE status = 'planning'"):
        # one plan at a time: a manual tick and the supervisor's would plan two candidates at
        # once (Oct 14, the first live tick); a stale one was reopened above
        return {"did": "nothing", "why": "a plan is in progress", **rec}
    if s.one("SELECT 1 FROM batches WHERE project_id = ? AND status IN ('queued', 'active', "
             "'parked', 'blocked')", PROJECT):
        return {"did": "nothing", "why": "an ilk2 batch is in flight", **rec}
    # nothing is planned from a main that may hold an unverified merge: one interrupted, or one
    # still verifying in another process (Codex review of 3d92d9f #3)
    held = s.one("SELECT status FROM rsi_landings WHERE status IN ('merge-interrupted', 'merging') "
                 "ORDER BY status = 'merge-interrupted' DESC LIMIT 1")
    if held:
        why = ("an interrupted merge waits for resolution" if held["status"] == "merge-interrupted"
               else "a merge is being verified")
        return {"did": "nothing", "why": why, **rec}
    waiting = pending(s)
    if len(waiting) >= REVIEW_CAP:
        return {"did": "nothing", "why": f"{len(waiting)} RSI branches wait for review", **rec}
    open_ = candidates(s)
    if not open_:
        return {"did": "nothing", "why": "no open candidate", **rec}
    # Judgment call (Oct 15): the cap limits RSI's self-found work, so Chad's requests skip it,
    # and a cancelled batch uses no slot (the first request's batch was cancelled for a planner
    # brief defect, and the cap then held the second request for half a day). Wrong if Chad
    # wants RSI's total volume capped. Goals and defects have separate allowances, and a capped
    # candidate yields to the next one whose allowance is free (docs/08 §8.7).
    caps = {"goal": GOAL_DAILY_CAP, "defect": DAILY_CAP}
    used: dict[str, int] = {}
    top = None
    for c in open_:
        k = kind(c["source"])
        if k == "goal" and c["id"].split(":", 1)[1] not in goal_defs:
            continue  # a goal with no definition here (removed, or another host's): never planned
        if k != "request":
            used.setdefault(k, today_count(s, now, k))
            if used[k] >= caps[k]:
                continue
        top = c
        break
    if top is None:
        why = "; ".join(f"daily cap {caps[k]} reached" if k == "defect" else f"goal cap {caps[k]} reached"
                        for k in sorted(used))
        return {"did": "nothing", "why": why, **rec}
    round_ = None
    if top["source"] == "goal":
        from . import goals as goals_mod
        round_ = goals_mod.new_round(s, goal_defs[top["id"].split(":", 1)[1]], tick_info or {}, now)
    why = claim(s, top, now, caps, round_)
    if why:
        return {"did": "nothing", "why": why, **rec}
    s.event("rsi.planning", {"candidate": top["id"], "title": top["title"],
                             **({"round": round_["id"]} if round_ else {})})
    try:
        res = plan(s, top, repo, cfg)
    except Exception as e:  # noqa: BLE001 - a failed plan shelves the candidate, never wedges it
        res = {"shelve": f"planning failed: {type(e).__name__}: {e}"[:500]}
    if "spec" in res:
        from . import plans as plans_mod
        n = s.one("SELECT COUNT(*) n FROM batches WHERE consumer_ref LIKE 'rsi-auto-%'")["n"] + 1
        meta = {"rsi_candidate": top["id"], "rsi_source": top["source"],
                **({"rsi_goal_round_id": round_["id"]} if round_ else {})}
        try:
            bid = batches.submit(s, PROJECT, plans_mod.from_dict(res["spec"]),
                                 consumer_ref=f"rsi-auto-{n}", meta=meta)
        except batches.SubmitRejected as e:
            res = {"shelve": "submit rejected: " + "; ".join(e.problems)[:500]}
        else:
            if s.get("batches", bid)["status"] == "cancelled":
                # its round closed while it was planned (docs/08 B2): submitted as cancelled
                return {"did": "nothing", "why": f"{bid} was cancelled: its round closed", **rec}
            if round_:
                with s.tx() as c:
                    c.execute("UPDATE rsi_goal_rounds SET batch_id = ? WHERE id = ?", (bid, round_["id"]))
            s.update("rsi_candidates", top["id"], {"status": "submitted", "batch_id": bid},
                     actor="rsi", reason="submitted")
            s.event("rsi.submitted", {"candidate": top["id"], "batch": bid}, batch_id=bid)
            return {"did": "submitted", "batch": bid, "candidate": top["id"], **rec}
    status = "done" if "done" in res else "shelved"
    note = res.get("done") or res.get("shelve")
    if round_:  # the round's result first, before the candidate row (docs/08 B2)
        from . import goals as goals_mod
        goals_mod.close(s, round_["id"], "answered" if status == "done" else "plan-failed",
                        "miss" if status == "done" else "retry", clock.wall(), note or "")
    s.update("rsi_candidates", top["id"], {"status": status, "note": note}, actor="rsi",
             reason=status)
    s.event(f"rsi.{status}", {"candidate": top["id"], "note": note})
    return {"did": status, "candidate": top["id"], "note": note, **rec}
