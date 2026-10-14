"""RSI candidate finder (docs/06 "RSI"; Chad, Oct 14: unattended triggering of reviewed RSI)."""

from __future__ import annotations

import json

import pytest
from conftest import one_step

from ilk2 import clock, paths, rsi


def ended_run(ilk, bid, reason, error=None):
    rid = ilk.new_run(bid)
    ilk.s.update("runs", rid, {"status": "ended", "terminal_reason": reason,
                               "ended_at": clock.wall()}, actor="test", reason="end")
    if error:
        paths.run_dir(rid).mkdir(parents=True, exist_ok=True)
        (paths.run_dir(rid) / "driver-error.txt").write_text(f"Traceback ...\n{error}\n")
    return rid


def test_the_finder_ranks_ilk2_defects_and_ignores_consumer_and_host_state(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    # the same driver error twice, differing only in ids and paths: one candidate, 2 hits
    ended_run(ilk, bid, "error", "GitError: worktree add /Users/x/a b-1234567 failed")
    ended_run(ilk, bid, "error", "GitError: worktree add /Users/y/b b-89abcde failed")
    for _ in range(3):
        ended_run(ilk, bid, "stalled (spawn)")
    ended_run(ilk, bid, "shipped")                              # not a defect
    ilk.s.open_needs_human("base-unmeasured", "gate x cannot be measured", "fix", batch_id=bid)
    ilk.s.open_needs_human("attempts", "5 reds", "look", batch_id=bid)   # the consumer's work
    for i in range(3):  # a refusal that recurs: opened and cleared three times
        ilk.s.refuse(bid, "crash-backoff", f"lost its run {i}", "wait", "supervisor")
        ilk.s.clear_refusal(bid, "crash-backoff")
        ilk.s.refuse(bid, "host-load", "busy", "wait", "supervisor")    # the host's state
        ilk.s.clear_refusal(bid, "host-load")
    (tmp_path / "docs" / "rsi").mkdir(parents=True)
    (tmp_path / "docs" / "rsi" / "requests.json").write_text(json.dumps([
        {"id": "r1", "title": "show lanes in the panel", "acceptance": "xbar test shows 2 lanes"},
        {"id": "r2", "title": "no acceptance check"}]))
    got = {c.source: c for c in rsi.find(ilk.s, tmp_path)}
    assert set(got) == {"request", "engine-error", "stall", "needs-human", "refusal"}
    assert len(got["engine-error"].evidence) == 2 and len(got["stall"].evidence) == 3
    assert got["needs-human"].title == "needs_human: base-unmeasured"
    assert got["refusal"].title == "refusal recurs: crash-backoff"
    assert got["request"].id == "request:r1"                    # r2 has no acceptance check
    order = [c.source for c in rsi.find(ilk.s, tmp_path)]
    assert order[0] == "request" and order.index("engine-error") < order.index("refusal")


def test_a_broken_requests_file_stops_the_finder(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    (tmp_path / "docs" / "rsi").mkdir(parents=True)
    (tmp_path / "docs" / "rsi" / "requests.json").write_text("{not json")
    with pytest.raises(ValueError):
        rsi.find(ilk.s, tmp_path)                               # fails closed, never "none"


def test_recording_upserts_and_a_shelved_candidate_reopens_on_new_hits(ilk):
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    ended_run(ilk, bid, "lost")
    assert rsi.record(ilk.s, rsi.find(ilk.s)) == {"found": 1, "new": 1, "reopened": 0}
    (row,) = rsi.candidates(ilk.s)
    assert (row["source"], row["hits"], row["status"]) == ("stall", 1, "open")
    ilk.s.update("rsi_candidates", row["id"], {"status": "shelved"}, actor="test", reason="x")
    ended_run(ilk, bid, "lost")
    assert rsi.record(ilk.s, rsi.find(ilk.s))["reopened"] == 0   # one more hit: stays shelved
    ended_run(ilk, bid, "lost")
    assert rsi.record(ilk.s, rsi.find(ilk.s))["reopened"] == 1   # two more: back to open
    assert rsi.candidates(ilk.s)[0]["hits"] == 3


def test_rsi_cli_finds_and_lists(ilk, capsys):
    from ilk2 import cli
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    ended_run(ilk, bid, "lost")
    capsys.readouterr()
    assert cli.main(["rsi", "find"]) == 0
    assert json.loads(capsys.readouterr().out)["new"] == 1
    assert cli.main(["rsi", "candidates"]) == 0
    (c,) = json.loads(capsys.readouterr().out)["candidates"]
    assert (c["source"], c["hits"], c["status"]) == ("stall", 1, "open")
