"""Standing goals for RSI (docs/08 stage B2): validation, metrics, the round ledger, derived state."""

from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import one_step
from test_rsi import ilk2_project
from test_rsi_land import land, shipped_rsi_batch  # noqa: F401 - the fixture

from ilk2 import batches, clock, goals, plans, rsi

INPUTS = {"id": "rsi-inputs", "project": "ilk2", "metric": "rsi_input_failures_last_tick", "max": 0,
          "window_days": 7, "min_samples": 1}


def write_goals(ilk, *gs):
    d = ilk.repo / "docs" / "rsi"
    d.mkdir(parents=True, exist_ok=True)
    (d / "goals.json").write_text(json.dumps(list(gs)))


def tick_done(ilk, failing=("issues",)):
    src = {k: ("RuntimeError: x" if k in failing else "ok") for k in ("records", "requests", "issues")}
    ilk.s.event("rsi.tick-completed", {"tick_id": "t", "did": "nothing", "sources": src})


def failing_inputs(monkeypatch):
    """Every real tick then reports a failed `issues` source: the goal's metric stays at 1."""
    monkeypatch.setattr(rsi, "find_sources", lambda s, repo, now: ([], {"issues": "RuntimeError: x"}, {}))


def rows(ilk, reason="rsi-goal"):
    return ilk.s.all("SELECT * FROM needs_human WHERE reason = ? AND state IN ('open', 'acked')", reason)


def test_goals_json_is_validated_goal_by_goal(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    ok = dict(INPUTS)
    write_goals(ilk, ok, {**ok, "id": "dup"}, {**ok, "id": "dup"}, {**ok, "id": "nobound", "max": None},
                {**ok, "id": "two", "min": 1}, {**ok, "id": "word", "max": "0"},
                {**ok, "id": "win", "window_days": 0}, {**ok, "id": "m", "metric": "turns"},
                {**ok, "id": "far", "host": "rezmac"}, {**ok, "id": "nop", "project": "nope"})
    good, bad = goals.load(ilk.s, ilk.repo)
    assert [g["id"] for g in good] == ["rsi-inputs"]
    assert set(bad) == {"dup", "nobound", "two", "word", "win", "m", "far", "nop"}


def test_metrics_count_only_records_inside_the_range(ilk):
    ilk.make_repo()
    ilk.project()
    from test_driver import GOOD
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    at = ilk.s.one("SELECT at FROM transitions WHERE entity = 'steps' AND to_state = 'accepted'")["at"]
    value, n = goals.metric(ilk.s, "proj", "minutes_per_accepted_step", at - 1, at + 1)
    assert n == 1 and value is not None
    assert goals.metric(ilk.s, "proj", "minutes_per_accepted_step", at + 1, at + 99) == (None, 0)
    assert goals.metric(ilk.s, "other", "minutes_per_accepted_step", at - 1, at + 1) == (None, 0)


def test_an_off_target_goal_is_open_and_one_on_target_is_met(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    write_goals(ilk, INPUTS)
    tick_done(ilk, failing=())
    assert goals.tick(ilk.s, ilk.repo, clock.wall(), rsi.PLAN_CAP_S) == {"rsi-inputs": "met"}
    tick_done(ilk)
    assert goals.tick(ilk.s, ilk.repo, clock.wall(), rsi.PLAN_CAP_S) == {"rsi-inputs": "eligible"}
    c = ilk.s.get("rsi_candidates", "goal:rsi-inputs")
    assert c["status"] == "open" and "is 1, target <= 0" in c["title"]


def test_two_answered_rounds_stop_a_goal_and_a_reset_restarts_it(ilk, monkeypatch):
    ilk.make_repo()
    ilk2_project(ilk)
    failing_inputs(monkeypatch)
    monkeypatch.setattr(rsi, "pending", lambda s: [])
    monkeypatch.setattr(rsi, "plan", lambda s, cand, *a, **k: {"done": "not a defect: the host"})
    write_goals(ilk, INPUTS)
    tick_done(ilk)
    for _ in range(2):                     # each tick plans once; its own completion is a sample
        out = rsi.tick(ilk.s)
        assert out["did"] == "done" and out["candidate"] == "goal:rsi-inputs", out
    rs = goals.rounds(ilk.s, "rsi-inputs")
    assert [(r["result_kind"], r["verdict"]) for r in rs] == [("answered", "miss")] * 2
    assert rsi.tick(ilk.s)["why"] == "no open candidate"
    assert ilk.s.get("rsi_candidates", "goal:rsi-inputs")["status"] == "stopped"
    (row,) = rows(ilk)
    assert "stopped" in row["detail"]
    write_goals(ilk, {**INPUTS, "reset": 1})                         # Chad restarts it
    assert rsi.tick(ilk.s)["did"] == "done"                          # a new hash: eligible again
    assert not rows(ilk)


def test_a_landed_round_is_judged_on_new_samples_only(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    write_goals(ilk, INPUTS)
    tick_done(ilk)                                                  # before: 1 failing source
    now = clock.wall()
    r = goals.new_round(ilk.s, INPUTS, {"tick_id": "t"}, now)
    assert r["before_value"] == 1
    ilk.s.insert("rsi_goal_rounds", {**r, "result_kind": "landed", "closed_at": clock.wall()})
    goals.reconcile(ilk.s, ilk.repo, {"rsi-inputs": INPUTS}, clock.wall(), rsi.PLAN_CAP_S)
    assert goals.rounds(ilk.s, "rsi-inputs")[0]["verdict"] is None  # no sample after it yet
    assert goals.state(ilk.s, ilk.repo, INPUTS, clock.wall())[0] == "judging"
    tick_done(ilk, failing=())
    goals.reconcile(ilk.s, ilk.repo, {"rsi-inputs": INPUTS}, clock.wall() + 5, rsi.PLAN_CAP_S)
    (done,) = goals.rounds(ilk.s, "rsi-inputs")
    assert (done["after_value"], done["verdict"]) == (0, "improved")


def test_an_unjudgeable_round_plans_nothing_until_a_reset(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    tick_done(ilk)
    now = clock.wall()
    r = goals.new_round(ilk.s, INPUTS, {}, now)
    ilk.s.insert("rsi_goal_rounds", {**r, "registry_version": 99, "result_kind": "landed",
                                     "closed_at": now})
    assert goals.state(ilk.s, ilk.repo, INPUTS, now + 1)[0] == "unjudgeable"
    assert goals.state(ilk.s, ilk.repo, {**INPUTS, "reset": 1}, now + 1)[0] == "eligible"


def test_one_open_round_per_goal_and_one_planning_candidate(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    now = clock.wall()
    r = goals.new_round(ilk.s, INPUTS, {}, now)
    ilk.s.insert("rsi_goal_rounds", r)
    with pytest.raises(sqlite3.IntegrityError):
        ilk.s.insert("rsi_goal_rounds", {**r, "id": "gr-other"})
    for cid in ("stall:a", "stall:b"):
        ilk.s.insert("rsi_candidates", {"id": cid, "source": "stall", "title": cid, "evidence": "[]",
                                        "hits": 1, "score": 40, "status": "open", "first_seen": now,
                                        "last_seen": now})
    caps = {"goal": 1, "defect": 3}
    assert rsi.claim(ilk.s, ilk.s.get("rsi_candidates", "stall:a"), now, caps) is None
    assert rsi.claim(ilk.s, ilk.s.get("rsi_candidates", "stall:b"), now, caps) == "a plan is in progress"


def test_an_orphaned_round_closes_only_when_its_tick_is_dead(ilk):
    import os
    from ilk2 import procs
    ilk.make_repo()
    ilk2_project(ilk)
    old = clock.wall() - 3 * rsi.PLAN_CAP_S
    alive = {**goals.new_round(ilk.s, INPUTS, {"pid": os.getpid()}, old), "id": "gr-alive"}
    ilk.s.insert("rsi_goal_rounds", alive)
    goals.reconcile(ilk.s, ilk.repo, {}, clock.wall(), rsi.PLAN_CAP_S)
    assert goals.rounds(ilk.s, "rsi-inputs")[0]["closed_at"] is None   # its tick still lives
    with ilk.s.tx() as c:
        c.execute("UPDATE rsi_goal_rounds SET tick_proc_start = 'reused pid'")
    goals.reconcile(ilk.s, ilk.repo, {}, clock.wall(), rsi.PLAN_CAP_S)
    (r,) = goals.rounds(ilk.s, "rsi-inputs")
    assert (r["result_kind"], r["verdict"]) == ("plan-failed", "retry")
    assert procs.start_time(os.getpid())


def test_a_late_submit_to_a_closed_round_is_recorded_cancelled(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    now = clock.wall()
    r = goals.new_round(ilk.s, INPUTS, {}, now)
    ilk.s.insert("rsi_goal_rounds", {**r, "result_kind": "plan-failed", "verdict": "retry", "closed_at": now})
    bid = batches.submit(ilk.s, "ilk2", plans.from_dict(one_step()), consumer_ref="rsi-auto-9",
                         meta={"rsi_candidate": "goal:rsi-inputs", "rsi_source": "goal",
                               "rsi_goal_round_id": r["id"]})
    assert ilk.s.get("batches", bid)["status"] == "cancelled"
    assert ilk.s.one("SELECT 1 FROM transitions WHERE entity = 'batches' AND entity_id = ? AND "
                     "to_state = 'cancelled' AND reason = 'rsi-round-closed'", bid)


def test_a_held_round_is_abandoned_when_its_goal_changes_and_its_branch_never_lands(land):
    from ilk2 import rsi_land
    ilk = land
    bid = shipped_rsi_batch(ilk, [{"write": "notes.md", "text": "a\n"}])   # shipped, not in main
    now = clock.wall()
    r = goals.new_round(ilk.s, INPUTS, {}, now)
    ilk.s.insert("rsi_goal_rounds", {**r, "batch_id": bid})
    goals.reconcile(ilk.s, ilk.repo, {"rsi-inputs": INPUTS}, now, rsi.PLAN_CAP_S)
    assert goals.state(ilk.s, ilk.repo, INPUTS, now)[0] == "held"            # same goal: still held
    goals.reconcile(ilk.s, ilk.repo, {"rsi-inputs": {**INPUTS, "reset": 1}}, now, rsi.PLAN_CAP_S)
    (done,) = goals.rounds(ilk.s, "rsi-inputs")
    assert (done["result_kind"], done["verdict"]) == ("abandoned", "miss")
    assert rsi_land._row(ilk.s, bid)["status"] == "abandoned" and rsi.pending(ilk.s) == []


def test_a_removed_goal_is_retired(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    write_goals(ilk, INPUTS)
    tick_done(ilk)
    goals.tick(ilk.s, ilk.repo, clock.wall(), rsi.PLAN_CAP_S)
    ilk.s.insert("rsi_goal_rounds", {**goals.new_round(ilk.s, INPUTS, {}, clock.wall()),
                                     "result_kind": "answered", "verdict": "miss", "closed_at": clock.wall()})
    write_goals(ilk)
    assert goals.tick(ilk.s, ilk.repo, clock.wall(), rsi.PLAN_CAP_S) == {"rsi-inputs": "retired"}
    assert ilk.s.get("rsi_candidates", "goal:rsi-inputs")["status"] == "retired"


def test_failed_plans_stop_a_goal_with_a_planning_alert_and_reset_goal_restarts_it(ilk, monkeypatch):
    # Oct 19: 3 planning timeouts stopped gh-resolve-step-minutes with "did not move it" and a
    # clear "command" that was a sentence; each round had also started cold.
    import subprocess

    from ilk2 import cli
    ilk.make_repo()
    ilk2_project(ilk)
    failing_inputs(monkeypatch)
    monkeypatch.setattr(rsi, "pending", lambda s: [])
    monkeypatch.setattr(goals, "RETRY_WAIT_S", 0)
    seen = []

    def timed_out(s, cand, *a, **k):
        seen.append(cand["evidence"])
        return {"shelve": f"the planner wrote no .rsi-spec.json (session timeout); "
                          f"its last notes: lever {len(seen)} is the brief"}
    monkeypatch.setattr(rsi, "plan", timed_out)
    write_goals(ilk, INPUTS)
    g = ["git", "-C", str(ilk.repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(g + ["add", "docs"], check=True)
    subprocess.run(g + ["commit", "-q", "-m", "goals"], check=True)
    tick_done(ilk)
    for _ in range(3):
        assert rsi.tick(ilk.s)["did"] == "shelved"
    assert "earlier_rounds" not in seen[0] and "lever 1 is the brief" in seen[1]  # carried over
    assert rsi.tick(ilk.s)["why"] == "no open candidate"
    (row,) = rows(ilk)
    assert "failed to plan" in row["detail"] and "lever 3" in row["detail"]
    assert "did not move" not in row["detail"]
    assert row["clear_command"] == "ilk2 rsi reset-goal rsi-inputs"

    (ilk.repo / "stray.txt").write_text("x")                        # never swept into its commit
    assert cli.main(["rsi", "reset-goal", "rsi-inputs", "--repo", str(ilk.repo)]) == 2
    (ilk.repo / "stray.txt").unlink()
    assert cli.main(["rsi", "reset-goal", "nope", "--repo", str(ilk.repo)]) == 2
    assert cli.main(["rsi", "reset-goal", "rsi-inputs", "--repo", str(ilk.repo)]) == 0
    assert json.loads((ilk.repo / "docs/rsi/goals.json").read_text())[0]["reset"] == 1
    log = subprocess.run(["git", "-C", str(ilk.repo), "log", "-1", "--format=%s", "--name-only"],
                         capture_output=True, text=True, check=True).stdout.split()
    assert log[:4] == ["rsi:", "reset", "goal", "rsi-inputs"] and log[-1] == "docs/rsi/goals.json"
    assert rsi.tick(ilk.s)["did"] == "shelved"                       # a new hash: eligible again
    assert not rows(ilk)
