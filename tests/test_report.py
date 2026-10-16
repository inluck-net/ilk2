"""`ilk2 report`: the docs/04 §4.5 acceptance rows, per project (the P4/P5/P7 gate evidence)."""

from __future__ import annotations

import json
import subprocess

from conftest import one_step

from ilk2 import batches, report

GOOD = {"write": "calc.py", "text": "def add(a, b):\n    return a + b\n"}
BAD = {"write": "calc.py", "text": "def add(a, b):\n    return a - b\n"}


def test_report_is_per_project_and_covers_the_acceptance_rows(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(ilk.repo), str(other)], check=True)
    batches.project_add(ilk.s, "other", other, batches.project_config(ilk.s, "proj"))
    ilk.script([BAD], [GOOD], [GOOD])
    a = ilk.submit(one_step())
    assert ilk.drive_inprocess(a) == "shipped"            # proj: 1 step, 2 attempts
    b = batches.submit(ilk.s, "other", __import__("ilk2").plans.from_dict(one_step()))
    assert ilk.drive_inprocess(b) == "shipped"            # other: 1 step, 1 attempt
    c = ilk.submit(one_step())
    batches.park(ilk.s, c)  # a human transition in proj, with its reason
    r_all = report.build(ilk.s)
    r_proj = report.build(ilk.s, project="proj")
    r_other = report.build(ilk.s, project="other")
    assert r_all["accepted_steps"] == 2 and r_proj["accepted_steps"] == 1 and r_other["accepted_steps"] == 1
    assert r_proj["attempts"] == 2 and r_other["attempts"] == 1
    assert r_all["silent_stuck"] == 0                      # I9: every terminal state has a reason
    assert r_all["runs_past_bound_now"] == 0               # I11 spot check
    assert {"human_transitions", "human_waits", "refused_hours",
            "minutes_to_first_edit_median", "share_attempts_producing_nothing",
            "minutes_per_accepted_step"} <= set(r_all)
    ht = r_proj["human_transitions"]
    assert ht["total"] >= 1 and ht["max_per_batch"] >= 1
    assert all(x["actor"] == "human" and x["reason"] for x in ht["items"])


def test_silent_stuck_counts_a_terminal_state_without_a_reason(ilk):
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    ilk.s.transition("batches", bid, "cancelled", actor="test", reason="x")  # no terminal_reason
    assert report.build(ilk.s)["silent_stuck"] == 1


def test_v1_baseline_from_close_records(tmp_path, monkeypatch):
    # §4.5 compares a project with its own V1 baseline, measured the same way: minutes of
    # iteration time per step advanced, from V1's close records (logs/.ilk-loop.log).
    import json
    from ilk2 import v1
    monkeypatch.setenv("ILK2_V1_DATA", str(tmp_path))
    log = tmp_path / "projects" / "k" / "logs" / ".ilk-loop.log"
    log.parent.mkdir(parents=True)

    def rec(ts, mins, step, status="pending"):
        return "[x] " + json.dumps({"record_phase": "close", "timestamp": ts, "duration_sec": mins * 60,
                                    "execution": {"step_to": {"p.md": {"step": str(step), "status": status,
                                                                       "estimated_steps": "3"}}}})
    log.write_text("\n".join([rec("2026-10-12T10:00", 60, 0), rec("2026-10-12T11:00", 30, 1),
                              rec("2026-10-12T12:00", 30, 1), rec("2026-10-12T13:00", 60, 3, "shipped"),
                              "noise line"]) + "\n")
    b = v1.baseline("k")
    assert b["iterations"] == 4 and b["steps_advanced"] == 3 and b["iterations_without_a_step"] == 2
    assert b["minutes_per_accepted_step"] == 60.0  # 180 min / 3 steps


def test_steps_report_per_batch(ilk):
    # Oct 14: the P5 batch's per-step baseline was assembled by hand; it is one command now.
    ilk.make_repo()
    ilk.project()
    ilk.script([BAD, {"context": 120_000}], [GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    r = report.steps(ilk.s, bid)
    assert r["steps"][0]["peak_context_k"] == 120          # W4: largest prompt of any attempt
    assert r["steps"][0]["turns"] >= 2 and r["steps"][0]["s_per_turn"] is not None   # pace
    proj = report.build(ilk.s, "proj")
    assert proj["turns_per_accepted_step_median"] >= 2 and proj["seconds_per_turn_median"] is not None
    assert r["accepted"] == 1 and r["of"] == 1 and r["first_attempt_share"] == 0.0
    row = r["steps"][0]
    assert row["kind"] == "work" and row["attempts"] == 2 and row["red"] == 1
    assert row["minutes"] >= 0 and "| 1 |" in report.steps_table(r)


def test_batch_report_sums_phase_minutes(ilk):
    # The per-step table only shows worker time, so measuring the base is invisible (Oct 14: a
    # live kira batch spent 37 min in `base` before its first attempt).
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    r1, r2 = ilk.new_run(bid), ilk.new_run(bid)
    t0 = 1_728_000_000.0

    def ev(rid, kind, phase, at):
        ilk.s.insert("events", {"batch_id": bid, "run_id": rid, "kind": kind,
                                "payload": json.dumps({"phase": phase}),
                                "at": t0 + at, "mono": at})

    for rid, end in ((r1, 3000.0), (r2, 3804.0)):  # r2's unmatched recover stops at ended_at
        ilk.s.update("runs", rid, {"ended_at": t0 + end}, actor="test", reason="ended")

    # run 1: every phase paired with its own end
    for kind, phase, at in [("phase.start", "base", 0), ("phase.end", "base", 1800),
                            ("phase.start", "dispatch", 1800), ("phase.end", "dispatch", 2400),
                            ("phase.start", "gates", 2400), ("phase.end", "gates", 2460),
                            ("phase.start", "scope", 2460), ("phase.end", "scope", 2462)]:
        ev(r1, kind, phase, at)
    # run 2: dispatch is interrupted (no end), so it lasts until the next phase.start; recover
    # is never closed either, and has no next start, so it lasts until the run's ended_at
    for kind, phase, at in [("phase.start", "base", 3000), ("phase.end", "base", 3420),
                            ("phase.start", "dispatch", 3420), ("phase.start", "gates", 3558),
                            ("phase.end", "gates", 3564), ("phase.start", "recover", 3564)]:
        ev(r2, kind, phase, at)

    r = report.steps(ilk.s, bid)
    assert r["phase_minutes"] == {"base": 37.0, "dispatch": 12.3, "recover": 4.0,
                                  "gates": 1.1, "scope": 0.0}
    lines = [ln for ln in report.steps_table(r).splitlines() if ln.startswith("Phases (min):")]
    assert lines == ["Phases (min): base 37.0, dispatch 12.3, recover 4.0, gates 1.1"]
