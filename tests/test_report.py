"""`ilk2 report`: the docs/04 §4.5 acceptance rows, per project (the P4/P5/P7 gate evidence)."""

from __future__ import annotations

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
