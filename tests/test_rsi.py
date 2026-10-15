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


def test_the_finder_reports_a_gate_that_keeps_hitting_its_cap(ilk):
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    for _ in range(2):
        ilk.s.event("gate.timeout", {"gate": "convex"}, batch_id=bid)
    ilk.s.event("gate.timeout", {"gate": "typing"}, batch_id=bid)   # one hit: noise, no candidate
    ilk.s.event("base.measured", {"gate": "convex", "timeouts": 1}, batch_id=bid)
    got = [c for c in rsi.find(ilk.s) if c.source == "gate-cap"]
    assert len(got) == 1                                         # exactly one, for convex
    (c,) = got
    assert c.title == "proj: gate convex hits its time cap"
    assert len(c.evidence) == 3                                  # 2 gate.timeout + 1 base.measured
    assert all(e["gate"] == "convex" and e["batch"] == bid for e in c.evidence)
    assert {e["kind"] for e in c.evidence} == {"gate.timeout", "base.measured"}
    assert all(set(e) == {"batch", "gate", "at", "kind"} for e in c.evidence)


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


# -- planner and trigger ---------------------------------------------------------------------

def ilk2_project(ilk, **cfg):
    from conftest import PY_BIN, STUB, gate
    from ilk2 import batches
    c = batches.ProjectConfig(claude_bin=STUB, sandbox_allow=[str(ilk.tmp)],
                              path_prelude=[PY_BIN, "/usr/bin", "/bin"], rsi_auto=True,
                              deny_paths=["AGENTS.md", "docs/rsi/**"],
                              rsi_gate=gate("tests/test_calc.py", name="suite"), **cfg)
    batches.project_add(ilk.s, "ilk2", ilk.repo, c)


PLAN = {"title": "runs that end lost are retried", "scope": ["calc.py", "tests/test_calc.py"],
        "must_pass": ["tests/test_calc.py::test_add"],
        "instructions": "Make add() add. " * 30}


def spec_write(obj):
    return [{"write": ".rsi-spec.json", "text": json.dumps(obj)}]


def lost_run(ilk):
    ilk.project()
    bid = ilk.submit(one_step())
    ended_run(ilk, bid, "lost")


def test_tick_plans_the_top_candidate_and_submits_it_with_ilk2s_gate(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    lost_run(ilk)
    ilk.script(spec_write(PLAN))
    out = rsi.tick(ilk.s)
    assert out["did"] == "submitted", out
    b = ilk.s.get("batches", out["batch"])
    assert b["project_id"] == "ilk2" and b["consumer_ref"] == "rsi-auto-1"
    st = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", b["id"])
    assert [g["name"] for g in json.loads(st["gates"])] == ["suite"]   # the planner chose none
    assert json.loads(st["must_pass"]) == PLAN["must_pass"]
    (c,) = rsi.candidates(ilk.s, None)
    assert (c["status"], c["batch_id"]) == ("submitted", b["id"])
    assert "stall" in ilk.stub_prompts()[0] or "lost" in ilk.stub_prompts()[0]


def test_a_plan_that_touches_the_kernel_or_edits_files_is_shelved(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    lost_run(ilk)
    ilk.script(spec_write({**PLAN, "scope": ["calc.py", "AGENTS.md"]}))
    out = rsi.tick(ilk.s)
    assert out["did"] == "shelved" and "protected kernel" in out["note"]
    ilk.s.update("rsi_candidates", out["candidate"], {"status": "open"}, actor="t", reason="t")
    ilk.script(spec_write(PLAN) + [{"write": "calc.py", "text": "x = 1\n"}])
    out = rsi.tick(ilk.s)
    assert out["did"] == "shelved" and "changed files" in out["note"]
    assert not ilk.s.one("SELECT 1 FROM batches WHERE project_id = 'ilk2'")


def test_an_already_fixed_candidate_is_closed_without_a_batch(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    lost_run(ilk)
    ilk.script(spec_write({"already_fixed": "b7ed8b5 runs.py:ready_plans"}))
    out = rsi.tick(ilk.s)
    assert out["did"] == "done" and "b7ed8b5" in out["note"]
    assert rsi.candidates(ilk.s) == []                       # done is not reopened by the finder
    rsi.record(ilk.s, rsi.find(ilk.s))
    assert rsi.candidates(ilk.s) == []


def test_the_trigger_waits_for_a_batch_in_flight_reviews_and_the_daily_cap(ilk, monkeypatch):
    ilk.make_repo()
    ilk2_project(ilk)
    lost_run(ilk)
    ilk.script(spec_write(PLAN))
    first = rsi.tick(ilk.s)
    assert first["did"] == "submitted"
    assert rsi.tick(ilk.s)["why"] == "an ilk2 batch is in flight"
    # shipped (not cancelled: a cancelled batch uses no slot of the daily cap)
    ilk.s.update("batches", first["batch"], {"status": "shipped"}, actor="t", reason="t")
    monkeypatch.setattr(rsi, "pending", lambda s: [1, 2])
    assert rsi.tick(ilk.s)["why"] == "2 RSI branches wait for review"
    monkeypatch.setattr(rsi, "pending", lambda s: [])
    monkeypatch.setattr(rsi, "DAILY_CAP", 1)
    (c,) = rsi.candidates(ilk.s, None)                   # another open RSI-found candidate
    ilk.s.update("rsi_candidates", c["id"], {"status": "open"}, actor="t", reason="t")
    assert rsi.tick(ilk.s)["why"] == "daily cap 1 reached"


def test_the_trigger_is_off_unless_the_project_says_so(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    cfg = json.loads(ilk.s.get("projects", "ilk2")["config"])
    cfg["rsi_auto"] = False
    ilk.s.update("projects", "ilk2", {"config": json.dumps(cfg)}, actor="t", reason="t")
    assert rsi.tick(ilk.s)["why"] == "rsi_auto is off for the ilk2 project"


def test_the_supervisor_runs_rsi_tick_only_when_opted_in_and_never_twice_at_once(ilk, tmp_path):
    from ilk2.supervisor import Supervisor
    ilk.make_repo()
    sp = Supervisor(ilk.s, load_check=False)
    marker = tmp_path / "ticks"
    sp.rsi_cmd = ["/bin/sh", "-c", f"echo t >> {marker}; sleep 2"]
    sp.rsi_tick(1_000_000.0)
    assert not marker.exists()                                # no ilk2 project: off
    ilk2_project(ilk)
    sp.rsi_tick(1_000_000.0)
    sp.rsi_tick(1_000_000.0 + sp.RSI_EVERY_S + 1)             # the first is still running
    sp._rsi.wait()
    sp.rsi_tick(1_000_000.0 + sp.RSI_EVERY_S + 2)             # done, and the interval passed
    sp._rsi.wait()
    sp.rsi_tick(1_000_000.0 + sp.RSI_EVERY_S + 3)             # too soon after the last
    assert marker.read_text().splitlines() == ["t", "t"]


def test_one_plan_at_a_time(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    lost_run(ilk)
    rsi.record(ilk.s, rsi.find(ilk.s))
    (c,) = rsi.candidates(ilk.s)
    ilk.s.update("rsi_candidates", c["id"], {"status": "planning", "planned_at": clock.wall()},
                 actor="t", reason="another tick is planning")
    assert rsi.tick(ilk.s)["why"] == "a plan is in progress"
    ilk.s.update("rsi_candidates", c["id"], {"planned_at": clock.wall() - 2 * rsi.PLAN_CAP_S - 1},
                 actor="t", reason="that tick died")
    ilk.script(spec_write({"already_fixed": "x"}))
    assert rsi.tick(ilk.s)["did"] == "done"                  # the stale plan was reopened


def test_another_hosts_candidates_are_kept_here_tagged_with_the_host(ilk, monkeypatch, tmp_path):
    # Oct 14: kira-live runs on rezmac, RSI plans on chad-mbp; rezmac's records must reach it.
    from ilk2 import paths as p_
    ilk.make_repo()
    ilk2_project(ilk)
    (p_.home() / "host.json").write_text(json.dumps({"rsi_hosts": ["rezmac"]}))
    remote_rows = {"candidates": [
        {"id": "gate-cap:abc", "source": "needs-human", "title": "needs_human: base-unmeasured",
         "hits": 7, "score": 45.0, "status": "open", "batch_id": None,
         "evidence": [{"batch": "b-1", "detail": "kira gate"}]},
        {"id": "x:1", "source": "unknown-source", "title": "from a newer release", "hits": 1,
         "score": 1, "status": "open", "batch_id": None, "evidence": []}]}
    ssh = tmp_path / "ssh"
    ssh.write_text(f"#!/bin/sh\ncat <<'J'\n{json.dumps(remote_rows)}\nJ\n")
    ssh.chmod(0o755)
    monkeypatch.setenv("ILK2_SSH", str(ssh))
    monkeypatch.setattr(rsi, "plan", lambda *a, **k: {"done": "test: not planned"})
    rsi.tick(ilk.s)
    rows = {r["id"]: r for r in rsi.candidates(ilk.s, None)}
    r = rows["rezmac/gate-cap:abc"]
    assert r["title"] == "[rezmac] needs_human: base-unmeasured" and r["hits"] == 7
    assert json.loads(r["evidence"])[0]["host"] == "rezmac"
    assert "rezmac/x:1" not in rows                          # an unknown source is not kept


def test_an_unreachable_host_is_reported_not_fatal(ilk, monkeypatch, tmp_path):
    from ilk2 import paths as p_
    ilk.make_repo()
    ilk2_project(ilk)
    (p_.home() / "host.json").write_text(json.dumps({"rsi_hosts": ["rezmac"]}))
    ssh = tmp_path / "ssh"
    ssh.write_text("#!/bin/sh\necho 'connection refused' >&2\nexit 255\n")
    ssh.chmod(0o755)
    monkeypatch.setenv("ILK2_SSH", str(ssh))
    out = rsi.tick(ilk.s)
    assert out["why"] == "no open candidate"
    assert any(e["kind"] == "rsi.remote-failed" for e in ilk.s.events_since(0))


def test_must_pass_entries_are_reduced_to_test_ids(ilk):
    # Oct 15: the planner copied the brief's description into must_pass, so no id ever matched
    ilk.make_repo()
    ilk2_project(ilk)
    lost_run(ilk)
    ilk.script(spec_write({**PLAN, "must_pass": ["tests/test_calc.py::test_add, a new test that fails"]}))
    out = rsi.tick(ilk.s)
    assert out["did"] == "submitted", out
    st = ilk.s.one("SELECT must_pass FROM steps WHERE batch_id = ?", out["batch"])
    assert json.loads(st["must_pass"]) == ["tests/test_calc.py::test_add"]


def test_a_must_pass_that_is_not_a_test_id_shelves_the_plan(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    lost_run(ilk)
    ilk.script(spec_write({**PLAN, "must_pass": ["the panel shows the model"]}))
    out = rsi.tick(ilk.s)
    assert out["did"] == "shelved" and "not test ids" in out["note"]


def test_requests_skip_the_daily_cap_and_cancelled_batches_use_no_slot(ilk, monkeypatch):
    # Oct 15: the first request's batch was cancelled for a planner defect, and the cap then
    # held the second request for half a day.
    ilk.make_repo()
    ilk2_project(ilk)
    lost_run(ilk)
    ilk.script(spec_write(PLAN))
    first = rsi.tick(ilk.s)
    assert first["did"] == "submitted"
    ilk.s.transition("batches", first["batch"], "cancelled", actor="t", reason="t")
    assert rsi.today_count(ilk.s, clock.wall()) == 0               # cancelled: no slot
    ilk.s.update("batches", first["batch"], {"status": "shipped"}, actor="t", reason="t")
    assert rsi.today_count(ilk.s, clock.wall()) == 1
    monkeypatch.setattr(rsi, "DAILY_CAP", 1)
    monkeypatch.setattr(rsi, "pending", lambda s: [])
    (ilk.repo / "docs" / "rsi").mkdir(parents=True)
    (ilk.repo / "docs" / "rsi" / "requests.json").write_text(json.dumps(
        [{"id": "r1", "title": "a request", "acceptance": "a test pins it"}]))
    ilk.script(spec_write(PLAN))
    out = rsi.tick(ilk.s)
    assert out["did"] == "submitted" and out["candidate"] == "request:r1"   # skipped the cap
    assert rsi.today_count(ilk.s, clock.wall()) == 1                        # not counted


def test_the_tick_records_its_outcome_for_the_panel(ilk):
    ilk.make_repo()
    ilk2_project(ilk)
    rsi.tick(ilk.s)
    last = json.loads(ilk.s.one("SELECT value FROM meta WHERE key = 'rsi_last_tick'")["value"])
    assert last["did"] == "nothing" and last["why"] == "no open candidate"


def test_rsi_review_holds_are_not_defects(ilk):
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    ilk.s.open_needs_human("rsi-review", "RSI batch needs a review", "look", batch_id=bid)
    assert not [c for c in rsi.find(ilk.s) if c.source == "needs-human"]
