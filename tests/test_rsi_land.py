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
    assert out["did"] == "review" and "protected kernel" in out["why"]


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
