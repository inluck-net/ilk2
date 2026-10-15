"""Unattended RSI landing (Chad, Oct 15: "RSI branches may merge and release without your
review, as long as we can track back with some kind of audit logs").

A shipped RSI batch lands in four stages, one batch at a time, each recorded in `rsi_landings`,
as an event, and (the merge) as git trailers:

  merge     into main in the build checkout, only when it is clean and on main; refused (left
            for review) when the diff touches the protected kernel or removes a test or an
            assert from an existing test file, or does not merge cleanly
  verify    the project's setup and RSI gate (the full suite) on the merged commit, in a scratch
            worktree; red resets main to before the merge and leaves the batch for review
  release   build and upgrade this host, only when no run is live (a drain would interrupt
            consumer batches)
  canary    CANARY_S later: the supervisor ticks and no run ended in an error or a stall since
            the release, else roll back to the previous release and open a needs_human row
  push      SOAK_S after a verified release, to the rsi_hosts (rezmac), only when idle there

`ilk2 rsi log` reads it back: which version came from which batch, fixing which candidate.
The project setting rsi_land is the brake: off, branches wait for review as before.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from . import clock, gitops, paths

CANARY_S = 15 * 60        # judgment call: a supervisor tick and a few runs; wrong if regressions hide longer
SOAK_S = 24 * 3600        # judgment call: a day of real batches before rezmac (live consumer work)
# Governance: no RSI batch may edit these (the ilk2 project's deny_paths turn any attempt that
# does red). Machinery: RSI may fix its own code, but such a batch always lands by review
# (Chad, Oct 16: "go as what you recommended").
GOVERNANCE = ["AGENTS.md", "CLAUDE.md", "docs/rsi/**", ".github/**"]
MACHINERY = ["src/ilk2/rsi.py", "src/ilk2/rsi_land.py", "src/ilk2/release.py",
             "tests/test_rsi.py", "tests/test_rsi_land.py"]
KERNEL = GOVERNANCE + MACHINERY
TRAILER = "ilk2-rsi"


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return gitops.git(repo, *args, check=check)


def removed_tests(repo: Path, base: str, head: str) -> list[str]:
    """Lines a batch removed from existing test files that define a test or assert something:
    a suite made green by deleting or loosening a test is the classic failure."""
    out = []
    diff = _git(repo, "diff", "-U0", f"{base}..{head}", "--", "tests/", check=False)
    path = None
    for line in diff.splitlines():
        if line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("-") and not line.startswith("---") and path:
            text = line[1:].strip()
            if re.match(r"(async\s+)?def\s+test_", text) or re.match(r"assert\b", text):
                out.append(f"{path}: {text[:120]}")
    return out


def touched_kernel(repo: Path, base: str, head: str, deny: list[str]) -> list[str]:
    from .batches import path_matches
    files = _git(repo, "diff", "--name-only", f"{base}..{head}", check=False).split()
    return [f for f in files if path_matches(f, list(deny) + KERNEL)]


def _idle(s) -> bool:
    return not s.one("SELECT 1 FROM runs WHERE status IN ('starting', 'running')")


def _row(s, bid):
    return s.one("SELECT * FROM rsi_landings WHERE batch_id = ?", bid)


def _set(s, bid: str, **fields) -> None:
    with s.tx() as c:
        sets = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE rsi_landings SET {sets} WHERE batch_id = ?", (*fields.values(), bid))


def _review(s, b, why: str) -> dict:
    """Leave the batch for a human review: recorded, visible in the panel, never retried."""
    if _row(s, b["id"]) is None:
        with s.tx() as c:
            c.execute("INSERT INTO rsi_landings(batch_id, candidate_id, status, note, at) "
                      "VALUES (?, ?, 'review', ?, ?)",
                      (b["id"], _candidate(b), why[:1000], clock.wall()))
    else:
        _set(s, b["id"], status="review", note=why[:1000])
    s.event("rsi.review", {"batch": b["id"], "why": why[:500]}, batch_id=b["id"])
    s.open_needs_human("rsi-review", f"RSI batch {b['id']} needs a review: {why[:300]}",
                       f"review git log main..{b['branch']}, merge or cancel it by hand",
                       batch_id=b["id"])
    return {"did": "review", "batch": b["id"], "why": why}


def _candidate(b) -> str | None:
    try:
        return (json.loads(b["snapshot"]).get("meta") or {}).get("rsi_candidate")
    except (TypeError, ValueError):
        return None


def verify(repo: Path, commit: str, cfg) -> tuple[bool, str]:
    """The project's setup and RSI gate on `commit`, in a scratch worktree, in the pinned env."""
    from . import rsi
    from .gates.env import pinned_env
    gate = cfg.rsi_gate or rsi.SUITE_GATE
    with tempfile.TemporaryDirectory(dir=paths.data(), prefix="rsi-land-") as d:
        wt = Path(d) / "wt"
        gitops.add_worktree(repo, wt, commit)
        try:
            env = pinned_env(home=Path(d) / "home", tmp=Path(d) / "tmp",
                             path_prelude=cfg.path_prelude, allow=cfg.env_allow,
                             set_vars=cfg.env_set, home_files=cfg.home_files)
            if cfg.setup:
                p = subprocess.run(cfg.setup["command"], cwd=wt, env=env, capture_output=True,
                                   text=True, timeout=int(cfg.setup.get("cap_s", 900)))
                if p.returncode:
                    return False, f"setup failed: {(p.stdout + p.stderr)[-800:]}"
            p = subprocess.run(gate["command"], cwd=wt, env=env, capture_output=True, text=True,
                               timeout=int(gate.get("cap_s", 900)) * 2)
            return p.returncode == 0, (p.stdout + p.stderr)[-1500:]
        except subprocess.TimeoutExpired as e:
            return False, f"timed out: {e}"
        finally:
            gitops.remove_worktree(repo, wt)


def merge(s, b, repo: Path, cfg) -> dict:
    head = gitops.rev(repo, b["branch"])
    main = gitops.rev(repo, "main")
    if _git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False) != "main":
        return {"did": "nothing", "why": "the build checkout is not on main"}
    if _git(repo, "status", "--porcelain", "--untracked-files=no", check=False):
        return {"did": "nothing", "why": "the build checkout has uncommitted changes"}
    base = _git(repo, "merge-base", main, head)
    bad = touched_kernel(repo, base, head, cfg.deny_paths)
    if bad:
        from .batches import path_matches
        what = "RSI's own machinery" if all(path_matches(f, MACHINERY) for f in bad) \
            else "the protected kernel"
        return _review(s, b, f"touches {what}: {bad[:10]}")
    weak = removed_tests(repo, base, head)
    if weak:
        return _review(s, b, f"removes tests or asserts: {weak[:10]}")
    cand = _candidate(b)
    msg = (f"Merge RSI batch {b['id']} ({b['consumer_ref']}), landed unattended\n\n"
           f"{TRAILER}-batch: {b['id']}\n{TRAILER}-candidate: {cand}\n{TRAILER}-branch: {b['branch']}\n")
    out = _git(repo, "merge", "--no-ff", "-m", msg, b["branch"], check=False)
    merged = gitops.rev(repo, "HEAD")
    if merged == main or not gitops.is_ancestor(repo, head, merged):
        _git(repo, "merge", "--abort", check=False)
        return _review(s, b, f"does not merge cleanly into main: {out[-300:]}")
    ok, tail = verify(repo, merged, cfg)
    if not ok:
        if gitops.rev(repo, "HEAD") == merged:
            _git(repo, "reset", "-q", "--keep", main)  # undo only our own merge
        return _review(s, b, f"the suite is red on the merged commit: {tail[-600:]}")
    with s.tx() as c:
        c.execute("INSERT OR REPLACE INTO rsi_landings(batch_id, candidate_id, merge_commit, "
                  "pre_merge, status, at, merged_at) VALUES (?, ?, ?, ?, 'merged', ?, ?)",
                  (b["id"], cand, merged, main, clock.wall(), clock.wall()))
    s.event("rsi.merged", {"batch": b["id"], "candidate": cand, "commit": merged}, batch_id=b["id"])
    return {"did": "merged", "batch": b["id"], "commit": merged}


def release_landed(s, row, repo: Path) -> dict:
    from . import release
    if not _idle(s):
        return {"did": "nothing", "why": "a run is live: the release waits for idle"}
    previous = Path(release.current().resolve()).name if release.current().exists() else None
    if gitops.rev(repo, "HEAD") != row["merge_commit"] and not gitops.is_ancestor(
            repo, row["merge_commit"], gitops.rev(repo, "HEAD")):
        return {"did": "nothing", "why": "main no longer contains the merge"}
    built = release.build(repo)
    version = built.name
    out = release.upgrade(version)
    _set(s, row["batch_id"], status="released", version=version, previous_version=previous,
         released_at=clock.wall())
    s.event("rsi.released", {"batch": row["batch_id"], "version": version, "previous": previous,
                             "drain": out.get("drain")}, batch_id=row["batch_id"])
    return {"did": "released", "batch": row["batch_id"], "version": version}


def canary(s, row, now: float) -> dict:
    from . import bounds, release
    if now - row["released_at"] < CANARY_S:
        return {"did": "nothing", "why": "canary running"}
    b = bounds.load()
    sup = s.one("SELECT tick_wall FROM supervisor WHERE id = 1")
    ticking = sup and sup["tick_wall"] and now - sup["tick_wall"] < b.supervisor_stale
    bad = s.all("SELECT id, terminal_reason FROM runs WHERE ended_at > ? AND (terminal_reason = "
                "'error' OR terminal_reason LIKE 'stalled%')", row["released_at"])
    if ticking and not bad:
        _set(s, row["batch_id"], status="verified", verified_at=now)
        s.event("rsi.verified", {"batch": row["batch_id"], "version": row["version"]})
        _close_candidate(s, row, "done", f"landed in {row['version']} (batch {row['batch_id']})")
        return {"did": "verified", "batch": row["batch_id"]}
    why = "the supervisor is not ticking" if not ticking else \
        f"runs ended badly since the release: {[dict(r) for r in bad][:5]}"
    if row["previous_version"]:
        release.upgrade(row["previous_version"])
    _set(s, row["batch_id"], status="rolled-back", rolled_back_at=now, note=why[:1000])
    _close_candidate(s, row, "shelved", f"rolled back from {row['version']}: {why[:300]}")
    s.event("rsi.rolled-back", {"batch": row["batch_id"], "version": row["version"],
                                "to": row["previous_version"], "why": why[:500]})
    s.open_needs_human("rsi-rollback", f"RSI release {row['version']} (batch {row['batch_id']}) "
                       f"was rolled back to {row['previous_version']}: {why[:300]}",
                       "find the cause, then revert or fix the merge on main by hand")
    return {"did": "rolled-back", "batch": row["batch_id"], "why": why}


def push(s, row, repo: Path, hosts: list[str], now: float) -> dict:
    from . import release
    if now - (row["verified_at"] or now) < SOAK_S - CANARY_S:
        return {"did": "nothing", "why": "soaking before the other hosts"}
    done = []
    for h in hosts:
        # A host already running a release that contains this merge needs nothing, and pushing
        # this landing's older version would downgrade it (Oct 16: 0.3.0 went to rezmac ahead
        # of the soaking landings).
        have = release.host_version(h) or ""
        sha = have.rsplit("+g", 1)[1] if "+g" in have else None
        if sha and row["merge_commit"] and gitops.rev(repo, sha) and gitops.is_ancestor(
                repo, row["merge_commit"], gitops.rev(repo, sha)):
            done.append(f"{h} (already has it: {have})")
            continue
        idle = subprocess.run([*release._ssh(), h, "sqlite3 ~/.ilk2/data/state.db \"select count(*) "
                               "from runs where status in ('starting','running')\""],
                              capture_output=True, text=True, timeout=60).stdout.strip()
        if idle != "0":
            return {"did": "nothing", "why": f"{h} has live runs: the push waits"}
        r = release.push(h, repo, row["version"])
        if not r["ok"]:
            return {"did": "nothing", "why": f"push to {h} failed: {r['error']}"}
        done.append(h)
    _set(s, row["batch_id"], status="pushed", pushed_at=now)
    s.event("rsi.pushed", {"batch": row["batch_id"], "version": row["version"], "hosts": done})
    return {"did": "pushed", "batch": row["batch_id"], "hosts": done}


def land(s, now: float | None = None) -> dict:
    """One stage of one landing per call; the earliest unfinished landing first."""
    from . import batches, rsi
    now = clock.wall() if now is None else now
    proj = s.one("SELECT * FROM projects WHERE id = ?", rsi.PROJECT)
    if not proj:
        return {"did": "nothing", "why": "no ilk2 project"}
    cfg = batches.project_config(s, rsi.PROJECT)
    if not cfg.rsi_land:
        return {"did": "nothing", "why": "rsi_land is off: branches wait for review"}
    repo = Path(proj["repo_root"])
    # One landing between merge and the end of its canary, so a canary judges one change.
    row = s.one("SELECT * FROM rsi_landings WHERE status IN ('merged', 'released') ORDER BY at LIMIT 1")
    if row and row["status"] == "merged":
        return release_landed(s, row, repo)
    if row:
        return canary(s, row, now)
    # A verified landing soaking before the other hosts does not hold the next one: on Oct 15
    # the first verified landing's 24-hour soak held every later batch for a day.
    row = s.one("SELECT * FROM rsi_landings WHERE status = 'verified' ORDER BY at LIMIT 1")
    if row:
        if not rsi.hosts():  # no other host: the landing is done here
            _set(s, row["batch_id"], status="pushed", pushed_at=now)
            s.event("rsi.pushed", {"batch": row["batch_id"], "version": row["version"], "hosts": []})
            return {"did": "pushed", "batch": row["batch_id"], "hosts": []}
        res = push(s, row, repo, rsi.hosts(), now)
        if res["did"] != "nothing":
            return res
    landed = {r["batch_id"] for r in s.all("SELECT batch_id FROM rsi_landings")}
    for b in rsi.pending(s):
        if b["id"] not in landed:
            return merge(s, b, repo, cfg)
    return {"did": "nothing", "why": "nothing to land"}


def _close_candidate(s, row, status: str, note: str) -> None:
    """A landed candidate is done (its batch is in a verified release); a rolled-back one is
    shelved with why. Before Oct 15 they stayed 'submitted' forever."""
    cid = row["candidate_id"]
    if cid and s.one("SELECT 1 FROM rsi_candidates WHERE id = ?", cid):
        s.update("rsi_candidates", cid, {"status": status, "note": note[:1000]}, actor="rsi",
                 reason=f"landing {status}")


def log(s) -> list[dict]:
    rows = s.all("SELECT l.*, c.title FROM rsi_landings l LEFT JOIN rsi_candidates c ON "
                 "c.id = l.candidate_id ORDER BY l.at")
    return [dict(r) for r in rows]
