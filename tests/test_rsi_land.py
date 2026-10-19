"""Unattended RSI landing (Chad, Oct 15): merge, verify, release, canary, push, audited."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import PY_BIN, STUB, gate

from ilk2 import batches, clock, plans, release, rsi, rsi_land

GOOD = "def add(a, b):\n    return a + b\n"
TESTS = ("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\n"
         "def test_zero():\n    assert add(0, 0) == 0\n")
FILES = {"calc.py": GOOD, "tests/test_calc.py": TESTS,
         "tests/conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"}


@pytest.fixture
def land(ilk, monkeypatch):
    ilk.make_repo(FILES)
    c = batches.ProjectConfig(claude_bin=STUB, sandbox_allow=[str(ilk.tmp)],
                              path_prelude=[PY_BIN, "/usr/bin", "/bin"], rsi_auto=True,
                              rsi_land=True, deny_paths=["AGENTS.md"],
                              rsi_gate=gate("tests/test_calc.py", name="suite"))
    batches.project_add(ilk.s, "ilk2", ilk.repo, c)
    calls = []
    monkeypatch.setattr(release, "build", lambda repo, version=None: Path(
        f"/x/0.2.0+g{ilk.git(repo, 'rev-parse', '--short=8', 'HEAD')}"))
    monkeypatch.setattr(release, "upgrade", lambda v: calls.append(v) or {"drain": False})
    cur = ilk.tmp / "current"
    (ilk.tmp / "0.2.0+gprev").mkdir()
    cur.symlink_to(ilk.tmp / "0.2.0+gprev")
    monkeypatch.setattr(release, "current", lambda: cur)
    ilk.release_calls = calls
    return ilk


def shipped_rsi_batch(ilk, actions):
    spec = {"plans": [{"slug": "fix", "steps": [{"title": "t", "instructions": "x",
                                                  "gates": [gate("tests/test_calc.py")]}]}]}
    bid = batches.submit(ilk.s, "ilk2", plans.from_dict(spec), consumer_ref="rsi-auto-1",
                         meta={"rsi_candidate": "stall:abc"})
    ilk.script(actions)
    assert ilk.drive_inprocess(bid) == "shipped"
    return bid


def tick_supervisor(ilk, at):
    with ilk.s.tx() as c:
        c.execute("UPDATE supervisor SET tick_wall = ? WHERE id = 1", (at,))


def test_a_shipped_rsi_batch_lands_unattended_and_is_audited(land):
    ilk = land
    bid = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "rsi\n"}])
    out = rsi_land.land(ilk.s)
    assert out["did"] == "merged", out
    msg = ilk.git(ilk.repo, "log", "-1", "--format=%B", "main")
    assert f"ilk2-rsi-batch: {bid}" in msg and "ilk2-rsi-candidate: stall:abc" in msg
    out = rsi_land.land(ilk.s)
    assert out["did"] == "released" and ilk.release_calls == [out["version"]]
    row = rsi_land.log(ilk.s)[0]
    assert (row["previous_version"], row["status"]) == ("0.2.0+gprev", "released")
    assert rsi_land.land(ilk.s)["why"] == "canary running"
    later = row["released_at"] + rsi_land.CANARY_S + 1
    tick_supervisor(ilk, later)
    assert rsi_land.land(ilk.s, later)["did"] == "verified"
    assert rsi_land.land(ilk.s, later)["did"] == "pushed"         # no other host: done here
    (row,) = rsi_land.log(ilk.s)
    assert row["status"] == "pushed" and row["candidate_id"] == "stall:abc" and row["merge_commit"]

    kinds = [e["kind"] for e in ilk.s.events_since(0) if e["kind"].startswith("rsi.")]
    assert kinds[-3:] == ["rsi.released", "rsi.verified", "rsi.pushed"] and "rsi.merged" in kinds


def test_a_batch_that_removes_a_test_or_an_assert_waits_for_review(land):
    ilk = land
    weaker = TESTS.replace("\ndef test_zero():\n    assert add(0, 0) == 0\n", "")
    bid = shipped_rsi_batch(ilk, [{"write": "tests/test_calc.py", "text": weaker}])
    main = ilk.git(ilk.repo, "rev-parse", "main")
    out = rsi_land.land(ilk.s)
    assert out["did"] == "review" and "removes tests or asserts" in out["why"]
    assert ilk.git(ilk.repo, "rev-parse", "main") == main            # nothing merged
    assert ilk.s.one("SELECT 1 FROM needs_human WHERE reason = 'rsi-review' AND batch_id = ?", bid)
    assert rsi_land.land(ilk.s)["why"] == "nothing to land"         # never retried


def test_a_batch_that_touches_the_kernel_waits_for_review(land):
    ilk = land
    shipped_rsi_batch(ilk, [{"write": "src/ilk2/rsi.py", "text": "CAP = 99\n"}])
    out = rsi_land.land(ilk.s)
    # RSI may fix its own machinery (Oct 16), but never without a review
    assert out["did"] == "review" and "RSI's own machinery" in out["why"]


def test_a_red_suite_on_the_merged_commit_undoes_the_merge(land, monkeypatch):
    ilk = land
    shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "x\n"}])
    main = ilk.git(ilk.repo, "rev-parse", "main")
    monkeypatch.setattr(rsi_land, "verify", lambda repo, commit, cfg: (False, "1 failed"))
    out = rsi_land.land(ilk.s)
    assert out["did"] == "review" and "red on the merged commit" in out["why"]
    assert ilk.git(ilk.repo, "rev-parse", "main") == main


def test_landing_waits_while_the_build_checkout_has_changes(land):
    ilk = land
    shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "x\n"}])
    (ilk.repo / "calc.py").write_text(GOOD + "# editing\n")
    assert rsi_land.land(ilk.s)["why"] == "the build checkout has uncommitted changes"


def test_a_bad_canary_rolls_back_to_the_previous_release(land):
    ilk = land
    shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "x\n"}])
    rsi_land.land(ilk.s)
    rsi_land.land(ilk.s)
    row = rsi_land.log(ilk.s)[0]
    rid = ilk.new_run(ilk.s.one("SELECT id FROM batches LIMIT 1")["id"])
    ilk.s.update("runs", rid, {"status": "ended", "terminal_reason": "stalled (gates)",
                               "ended_at": row["released_at"] + 60}, actor="t", reason="t")
    later = row["released_at"] + rsi_land.CANARY_S + 1
    tick_supervisor(ilk, later)
    out = rsi_land.land(ilk.s, later)
    assert out["did"] == "rolled-back" and ilk.release_calls[-1] == "0.2.0+gprev"
    assert ilk.s.one("SELECT 1 FROM needs_human WHERE reason = 'rsi-rollback'")
    assert rsi_land.log(ilk.s)[0]["status"] == "rolled-back"


def test_the_brake_stops_landing(land):
    ilk = land
    shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "x\n"}])
    cfg = json.loads(ilk.s.get("projects", "ilk2")["config"])
    cfg["rsi_land"] = False
    ilk.s.update("projects", "ilk2", {"config": json.dumps(cfg)}, actor="t", reason="t")
    assert rsi_land.land(ilk.s)["why"] == "rsi_land is off: branches wait for review"


def test_removed_tests_reads_only_definitions_and_asserts(land):
    ilk = land
    base = ilk.git(ilk.repo, "rev-parse", "HEAD")
    (ilk.repo / "tests" / "test_calc.py").write_text(TESTS.replace("assert add(0, 0) == 0",
                                                                    "assert add(0, 0) == 0  # same"))
    ilk.git(ilk.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "comment")
    got = rsi_land.removed_tests(ilk.repo, base, ilk.git(ilk.repo, "rev-parse", "HEAD"))
    assert got == ["tests/test_calc.py: assert add(0, 0) == 0"]       # a changed assert counts


def test_a_verified_landing_closes_its_candidate(land):
    # Oct 15: landed candidates stayed 'submitted' forever.
    ilk = land
    rsi.record(ilk.s, [rsi.Candidate("stall:abc", "stall", "runs end lost", [{}])])
    shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "x\n"}])
    rsi_land.land(ilk.s)
    rsi_land.land(ilk.s)
    row = rsi_land.log(ilk.s)[0]
    later = row["released_at"] + rsi_land.CANARY_S + 1
    tick_supervisor(ilk, later)
    rsi_land.land(ilk.s, later)
    c = ilk.s.get("rsi_candidates", "stall:abc")
    assert c["status"] == "done" and row["version"] in c["note"]


def test_a_soaking_landing_does_not_hold_the_next_one(land, monkeypatch):
    # Oct 15: the first verified landing's 24-hour soak before rezmac held rsi-auto-4 for hours.
    ilk = land
    monkeypatch.setattr(rsi, "hosts", lambda: ["rezmac"])
    first = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "1\n"}])
    rsi_land.land(ilk.s)
    rsi_land.land(ilk.s)
    row = rsi_land.log(ilk.s)[0]
    later = row["released_at"] + rsi_land.CANARY_S + 1
    tick_supervisor(ilk, later)
    assert rsi_land.land(ilk.s, later)["did"] == "verified"
    second = shipped_rsi_batch(ilk, [{"write": "more.md", "text": "2\n"}])
    out = rsi_land.land(ilk.s, later + 60)             # the first soaks; the second merges
    assert out["did"] == "merged" and out["batch"] == second
    assert {r["batch_id"]: r["status"] for r in rsi_land.log(ilk.s)} == {first: "verified",
                                                                           second: "merged"}


def test_a_host_that_already_has_the_merge_is_not_pushed_an_older_version(land, monkeypatch):
    # Oct 16: 0.3.0 went to rezmac by hand while two landings soaked; pushing their older
    # versions afterwards would have downgraded it.
    ilk = land
    monkeypatch.setattr(rsi, "hosts", lambda: ["rezmac"])
    shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "1\n"}])
    rsi_land.land(ilk.s)
    rsi_land.land(ilk.s)
    row = rsi_land.log(ilk.s)[0]
    later = row["released_at"] + rsi_land.CANARY_S + 1
    tick_supervisor(ilk, later)
    rsi_land.land(ilk.s, later)                             # verified
    ilk.git(ilk.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "later")
    newer = ilk.git(ilk.repo, "rev-parse", "--short=8", "HEAD")
    monkeypatch.setattr(release, "host_version", lambda h: f"0.3.0+g{newer}")
    pushed = []
    monkeypatch.setattr(release, "push", lambda *a, **k: pushed.append(a) or {"ok": True})
    out = rsi_land.land(ilk.s, later + rsi_land.SOAK_S)
    assert out["did"] == "pushed" and pushed == []           # nothing sent: rezmac has it
    assert "already has it" in out["hosts"][0]


# -- docs/08 B1: the landing lock, interrupted merges, and resolving a held landing ----------

def merging_row(ilk, bid, pid, start):
    with ilk.s.tx() as c:
        c.execute("INSERT INTO rsi_landings(batch_id, status, at, merge_pid, merge_proc_start) "
                  "VALUES (?, 'merging', ?, ?, ?)", (bid, clock.wall(), pid, start))


def test_a_live_merge_holds_the_lock_and_a_dead_one_becomes_a_hard_stop(land):
    import os
    from ilk2 import procs
    ilk = land
    first = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])
    second = shipped_rsi_batch(ilk, [{"write": "more.md", "text": "b\n"}])
    main = ilk.git(ilk.repo, "rev-parse", "main")
    merging_row(ilk, first, os.getpid(), procs.start_time(os.getpid()))      # alive
    assert rsi_land.land(ilk.s)["why"] == "a merge is in progress"          # second waits
    assert rsi_land._acquire(ilk.s, ilk.s.get("batches", second), None, main) == "another landing holds the lock"
    rsi_land._set(ilk.s, first, merge_proc_start="a reused pid")             # pid reuse: dead
    for _ in range(2):                                                       # stays stopped
        assert rsi_land.land(ilk.s)["why"] == "an interrupted merge waits: ilk2 rsi resolve"
    assert rsi_land._row(ilk.s, first)["status"] == "merge-interrupted"
    assert ilk.git(ilk.repo, "rev-parse", "main") == main                   # nothing merged
    (nh,) = ilk.s.all("SELECT * FROM needs_human WHERE reason = 'rsi-review' AND batch_id = ?", first)
    assert "ilk2 rsi resolve" in nh["clear_command"]
    assert first in [b["id"] for b in rsi.pending(ilk.s)]
    assert rsi.tick(ilk.s)["why"] == "an interrupted merge waits for resolution"


def interrupt_after_merge(ilk, monkeypatch, bid):
    """A merge that dies between `git merge` and its verification: main holds it, unverified."""
    def die(repo, commit, cfg):
        raise KeyboardInterrupt
    monkeypatch.setattr(rsi_land, "verify", die)
    with pytest.raises(KeyboardInterrupt):
        rsi_land.land(ilk.s)
    rsi_land._set(ilk.s, bid, merge_pid=999999, merge_proc_start="gone")
    assert rsi_land.land(ilk.s)["why"].startswith("an interrupted merge")
    from ilk2 import gitops
    head = gitops.rev(ilk.repo, f"ilk2/{bid}")
    assert gitops.is_ancestor(ilk.repo, head, gitops.rev(ilk.repo, "main"))   # main holds it


def test_keep_verifies_main_then_the_normal_release_and_canary_follow(land, monkeypatch):
    ilk = land
    bid = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])
    interrupt_after_merge(ilk, monkeypatch, bid)
    (nh,) = ilk.s.all("SELECT id FROM needs_human WHERE reason = 'rsi-review' AND batch_id = ?", bid)
    ilk.s.ack(nh["id"], "chad")                                              # acked, still active
    monkeypatch.setattr(rsi_land, "verify", lambda repo, commit, cfg: (False, "1 failed"))
    assert rsi_land.resolve(ilk.s, bid, keep=True)["did"] == "nothing"      # red: unchanged
    assert rsi_land._row(ilk.s, bid)["status"] == "merge-interrupted"

    def moves_main(repo, commit, cfg):
        (ilk.repo / "late.md").write_text("x\n")
        ilk.git(ilk.repo, "add", "late.md")
        ilk.git(ilk.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "late")
        return True, "ok"
    monkeypatch.setattr(rsi_land, "verify", moves_main)
    assert "main moved" in rsi_land.resolve(ilk.s, bid, keep=True)["why"]   # moving head: unchanged
    monkeypatch.setattr(rsi_land, "verify", lambda repo, commit, cfg: (True, "ok"))
    assert rsi_land.resolve(ilk.s, bid, keep=True)["how"] == "keep"
    row = rsi_land._row(ilk.s, bid)
    assert row["status"] == "merged" and row["merge_commit"]
    assert ilk.s.get("needs_human", nh["id"])["state"] == "cleared"
    assert rsi_land.land(ilk.s)["did"] == "released"                        # the normal path
    (ev,) = [e for e in ilk.s.events_since(0) if e["kind"] == "rsi.resolved"]
    assert ev["payload"]["verified"] == ev["payload"]["main"]


def test_reverted_needs_a_real_revert_and_discarded_needs_the_merge_out_of_main(land, monkeypatch):
    ilk = land
    bid = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])
    other = shipped_rsi_batch(ilk, [{"write": "more.md", "text": "b\n"}])
    interrupt_after_merge(ilk, monkeypatch, bid)
    with pytest.raises(rsi_land.ResolveError):
        rsi_land.resolve(ilk.s, bid, discarded=True)                        # the merge is in main
    (ilk.repo / "unrelated.md").write_text("u\n")
    ilk.git(ilk.repo, "add", "unrelated.md")
    ilk.git(ilk.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "unrelated")
    with pytest.raises(rsi_land.ResolveError, match="not undone at HEAD"):
        rsi_land.resolve(ilk.s, bid, reverted="HEAD")
    merge = ilk.git(ilk.repo, "log", "--merges", "-1", "--format=%H", "main")
    ilk.git(ilk.repo, "-c", "user.name=t", "-c", "user.email=t@t", "revert", "--no-edit", "-m", "1", merge)
    assert rsi_land.resolve(ilk.s, bid, reverted="HEAD")["how"] == "reverted"
    assert rsi_land._row(ilk.s, bid)["status"] == "abandoned"
    assert [b["id"] for b in rsi.pending(ilk.s)] == [other]                 # abandoned: not queued
    monkeypatch.setattr(rsi_land, "verify", lambda repo, commit, cfg: (True, "ok"))
    assert rsi_land.land(ilk.s)["did"] == "merged"                          # the stop is lifted


def test_a_discarded_review_never_fills_the_review_queue(land):
    ilk = land
    weaker = TESTS.replace("\ndef test_zero():\n    assert add(0, 0) == 0\n", "")
    bid = shipped_rsi_batch(ilk, [{"write": "tests/test_calc.py", "text": weaker}])
    assert rsi_land.land(ilk.s)["did"] == "review"
    assert rsi_land.resolve(ilk.s, bid, discarded=True)["how"] == "discarded"
    assert rsi.pending(ilk.s) == []
    assert not ilk.s.one("SELECT 1 FROM needs_human WHERE reason = 'rsi-review' AND state != 'cleared'")


# -- Codex review of 3d92d9f ---------------------------------------------------------------

def test_an_interrupted_merge_commits_its_status_event_and_hold_together(land, monkeypatch):
    ilk = land
    bid = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])
    merging_row(ilk, bid, 999999, "gone")

    def crash(*a, **k):
        raise RuntimeError("crash between the status and the hold")
    monkeypatch.setattr(ilk.s, "open_needs_human", crash)
    with pytest.raises(RuntimeError):
        rsi_land.interrupted(ilk.s)
    assert rsi_land._row(ilk.s, bid)["status"] == "merging"                 # rolled back
    monkeypatch.undo()
    assert rsi_land.interrupted(ilk.s) == [bid]
    assert ilk.s.one("SELECT 1 FROM needs_human WHERE reason = 'rsi-review' AND batch_id = ?", bid)


def test_a_resolution_commits_its_status_clear_and_event_together(land, monkeypatch):
    ilk = land
    weaker = TESTS.replace("\ndef test_zero():\n    assert add(0, 0) == 0\n", "")
    bid = shipped_rsi_batch(ilk, [{"write": "tests/test_calc.py", "text": weaker}])
    assert rsi_land.land(ilk.s)["did"] == "review"
    real = ilk.s.event

    def crash(kind, *a, **k):
        if kind == "rsi.resolved":
            raise RuntimeError("crash before the audit event")
        return real(kind, *a, **k)
    monkeypatch.setattr(ilk.s, "event", crash)
    with pytest.raises(RuntimeError):
        rsi_land.resolve(ilk.s, bid, discarded=True)
    assert rsi_land._row(ilk.s, bid)["status"] == "review"                  # still resolvable
    assert ilk.s.one("SELECT 1 FROM needs_human WHERE reason = 'rsi-review' AND state = 'open'")


def test_a_review_is_recorded_once_on_re_entry(land):
    ilk = land
    weaker = TESTS.replace("\ndef test_zero():\n    assert add(0, 0) == 0\n", "")
    bid = shipped_rsi_batch(ilk, [{"write": "tests/test_calc.py", "text": weaker}])
    b = ilk.s.get("batches", bid)
    rsi_land._review(ilk.s, b, "first")
    rsi_land._review(ilk.s, b, "again")
    assert len([e for e in ilk.s.events_since(0) if e["kind"] == "rsi.review"]) == 1


def test_a_live_merge_in_another_process_stops_planning(land, monkeypatch):
    import subprocess
    from ilk2 import procs
    ilk = land
    first = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])
    other = subprocess.Popen(["sleep", "60"])
    try:
        merging_row(ilk, first, other.pid, procs.start_time(other.pid))     # alive, not us
        monkeypatch.setattr(rsi, "plan", lambda *a, **k: pytest.fail("planned during a merge"))
        assert rsi.tick(ilk.s)["why"] == "a merge is being verified"
    finally:
        other.kill()


def test_concurrent_acquires_take_the_lock_once(land):
    import threading
    from ilk2 import gitops, paths
    from ilk2.store import Store
    ilk = land
    a = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])
    b = shipped_rsi_batch(ilk, [{"write": "more.md", "text": "b\n"}])
    main = gitops.rev(ilk.repo, "main")
    got = {}

    def take(bid):
        st = Store(paths.db())
        try:
            got[bid] = rsi_land._acquire(st, st.get("batches", bid), None, main)
        finally:
            st.close()
    ts = [threading.Thread(target=take, args=(x,)) for x in (a, b)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(v is True for v in got.values()) == [False, True]


def test_no_lock_without_this_processs_start_time(land, monkeypatch):
    from ilk2 import gitops, procs
    ilk = land
    bid = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])
    monkeypatch.setattr(procs, "start_time", lambda pid: None)
    held = rsi_land._acquire(ilk.s, ilk.s.get("batches", bid), None, gitops.rev(ilk.repo, "main"))
    assert "start time" in held and rsi_land._row(ilk.s, bid) is None


def test_a_fast_forward_hand_merge_can_be_resolved_as_reverted_unless_reintroduced(land):
    ilk = land
    weaker = TESTS.replace("\ndef test_zero():\n    assert add(0, 0) == 0\n", "")
    bid = shipped_rsi_batch(ilk, [{"write": "tests/test_calc.py", "text": weaker}])
    assert rsi_land.land(ilk.s)["did"] == "review"
    git = lambda *a: ilk.git(ilk.repo, "-c", "user.name=t", "-c", "user.email=t@t", *a)
    git("merge", "--ff-only", f"ilk2/{bid}")                                # a hand fast-forward
    git("revert", "--no-edit", "HEAD")
    revert = ilk.git(ilk.repo, "rev-parse", "HEAD")
    (ilk.repo / "tests" / "test_calc.py").write_text(weaker)                # reintroduced later
    git("commit", "-qam", "again")
    with pytest.raises(rsi_land.ResolveError, match="not undone at main"):
        rsi_land.resolve(ilk.s, bid, reverted=revert)
    git("revert", "--no-edit", "HEAD")
    assert rsi_land.resolve(ilk.s, bid, reverted=revert)["how"] == "reverted"
    (ev,) = [e for e in ilk.s.events_since(0) if e["kind"] == "rsi.resolved"]
    assert ev["payload"]["how"] == "reverted"
    assert not ilk.s.one("SELECT 1 FROM needs_human WHERE reason = 'rsi-review' AND state != 'cleared'")


def test_a_kept_landing_releases_only_what_was_verified_and_canaries(land, monkeypatch):
    ilk = land
    bid = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])
    interrupt_after_merge(ilk, monkeypatch, bid)
    verified = []
    monkeypatch.setattr(rsi_land, "verify", lambda repo, commit, cfg: verified.append(commit) or (True, "ok"))
    assert rsi_land.resolve(ilk.s, bid, keep=True)["how"] == "keep"
    (ilk.repo / "late.md").write_text("x\n")                                # main moves after keep
    ilk.git(ilk.repo, "add", "late.md")
    ilk.git(ilk.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "late")
    head = ilk.git(ilk.repo, "rev-parse", "HEAD")
    assert rsi_land.land(ilk.s)["did"] == "released"
    assert verified[-1] == head and rsi_land._row(ilk.s, bid)["verified_sha"] == head  # re-verified
    row = rsi_land._row(ilk.s, bid)
    later = row["released_at"] + rsi_land.CANARY_S + 1
    tick_supervisor(ilk, later)
    assert rsi_land.land(ilk.s, later)["did"] == "verified"


def test_the_cli_resolves_and_pending_shows_the_hold(land, capsys):
    from ilk2 import cli
    ilk = land
    weaker = TESTS.replace("\ndef test_zero():\n    assert add(0, 0) == 0\n", "")
    bid = shipped_rsi_batch(ilk, [{"write": "tests/test_calc.py", "text": weaker}])
    assert rsi_land.land(ilk.s)["did"] == "review"
    capsys.readouterr()
    cli.main(["rsi", "pending"])
    (p,) = json.loads(capsys.readouterr().out)["pending"]
    assert p["landing"] == "review" and "ilk2 rsi resolve" in p["resolve"]
    assert cli.main(["rsi", "resolve", bid, "--discarded"]) in (0, None)
    assert json.loads(capsys.readouterr().out)["how"] == "discarded"
    assert cli.main(["rsi", "resolve", bid, "--discarded"]) == 2           # no longer held
