"""Submit-side fencing (gh-resolve ADR 0116): --not-after and --fence refuse a late submit."""

from __future__ import annotations

import json

import pytest
from conftest import one_step

from ilk2 import batches, cli, clock, paths, plans


def worktrees():
    return sorted(p.name for p in paths.worktrees().glob("*")) if paths.worktrees().exists() else []


def test_a_submit_after_its_deadline_is_refused_and_leaves_nothing(ilk):
    ilk.make_repo()
    ilk.project()
    before = worktrees()
    with pytest.raises(batches.SubmitRejected) as e:
        batches.submit(ilk.s, "proj", plans.from_dict(one_step()), not_after=clock.wall() - 1)
    assert e.value.problems[0].startswith("refused:not-after")
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0 and worktrees() == before
    assert batches.submit(ilk.s, "proj", plans.from_dict(one_step()), not_after=clock.wall() + 60)


def test_the_deadline_is_checked_in_the_transaction_that_makes_the_batch_durable(ilk, monkeypatch):
    ilk.make_repo()
    ilk.project()
    before = worktrees()
    real, calls = batches._fenced, []

    def late_on_the_second_check(s, not_after, fence):
        calls.append(1)
        return None if len(calls) == 1 else "refused:not-after: crossed while preparing"
    monkeypatch.setattr(batches, "_fenced", late_on_the_second_check)
    with pytest.raises(batches.SubmitRejected, match="refused:not-after"):
        batches.submit(ilk.s, "proj", plans.from_dict(one_step()), not_after=clock.wall() + 1)
    assert len(calls) == 2 and ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0
    assert worktrees() == before                                  # the worktree it made is gone
    monkeypatch.setattr(batches, "_fenced", real)


def test_an_older_fence_token_is_refused_and_a_passing_one_is_recorded(ilk):
    ilk.make_repo()
    ilk.project()
    assert batches.advance_fence(ilk.s, "claim:7", 5) == 5
    assert batches.advance_fence(ilk.s, "claim:7", 3) == 5        # tokens only move forward
    with pytest.raises(batches.SubmitRejected, match="refused:fence"):
        batches.submit(ilk.s, "proj", plans.from_dict(one_step()), fence=("claim:7", 4))
    batches.submit(ilk.s, "proj", plans.from_dict(one_step()), fence=("claim:7", 6))
    with pytest.raises(batches.SubmitRejected, match="refused:fence"):
        batches.submit(ilk.s, "proj", plans.from_dict(one_step()), fence=("claim:7", 5))


def test_the_cli_takes_not_after_and_fence_and_check_creates_nothing(ilk, capsys):
    ilk.make_repo()
    ilk.project()
    f = ilk.tmp / "spec.json"
    f.write_text(json.dumps(one_step()))
    late = str(clock.wall() - 1)
    for extra in (["--check"], []):
        assert cli.main(["submit", "--project", "proj", "--not-after", late, *extra, str(f)]) == 2
        out = json.loads(capsys.readouterr().out)
        assert out["error"] == "rejected" and out["problems"][0].startswith("refused:not-after")
    assert cli.main(["fence", "--key", "claim:9", "--token", "2"]) in (0, None)
    assert json.loads(capsys.readouterr().out) == {"api_version": 1, "key": "claim:9", "token": 2}
    assert cli.main(["submit", "--project", "proj", "--fence", "claim:9", "1", "--check", str(f)]) == 2
    assert "refused:fence" in capsys.readouterr().out
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0


def test_a_fence_advanced_between_the_early_check_and_the_commit_refuses_the_submit(ilk, monkeypatch):
    # gh-resolve claim-lock §7: another process runs `ilk2 fence --token g+1` while a fenced
    # submit with token g is between its early check and its commit. Only the in-transaction
    # check can see it; the consumer cannot inject that pause, so it is pinned here.
    from ilk2 import paths
    from ilk2.store import Store
    ilk.make_repo()
    ilk.project()
    batches.advance_fence(ilk.s, "claim:7", 3)
    before = worktrees()
    real, seen = batches._fenced, []

    def early_then_advance(s, not_after, fence):
        out = real(s, not_after, fence)
        if not seen:                                   # the early check passes, then another
            seen.append(out)                           # process advances the fence
            other = Store(paths.db())
            try:
                batches.advance_fence(other, "claim:7", 4)
            finally:
                other.close()
        return out
    monkeypatch.setattr(batches, "_fenced", early_then_advance)
    with pytest.raises(batches.SubmitRejected, match="refused:fence: token 3 .* older than 4"):
        batches.submit(ilk.s, "proj", plans.from_dict(one_step()), fence=("claim:7", 3))
    assert seen == [None]                              # the early check had passed
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0 and worktrees() == before
