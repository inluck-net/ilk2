"""The ilk2 SwiftBar item (`ilk2 xbar`, contrib/xbar/ilk2.10s.sh): title states and dropdown."""

from ilk2 import xbar


def batch(id_, status, steps=(), blocked_reason=None, ref=None, project="gh-triage"):
    return {"id": id_, "status": status, "project": project, "consumer_ref": ref,
            "blocked_reason": blocked_reason, "branch": f"ilk2/{id_}",
            "plans": [{"slug": "p", "status": status, "steps": [
                {"idx": i, "title": t, "status": s, "attempts": n} for i, (t, s, n) in enumerate(steps)]}]}


def row(id_, bid, reason="attempts", detail="step p.0: 3 attempts red", clear="ilk2 resume b-1"):
    return {"id": id_, "batch_id": bid, "reason": reason, "detail": detail,
            "clear_command": clear, "consumer_ref": None}


def title(text):
    return text.splitlines()[0]


def test_ok_when_nothing_runs_or_waits():
    out = xbar.render({"batches": [batch("b-1", "shipped")], "needs_human": []}, tick_age=5, stale=180)
    assert title(out).startswith("ilk2 ok")


def test_running_shows_count_and_current_step():
    st = {"batches": [batch("b-2", "active", [("move PRs", "accepted", 1), ("fold issues", "pending", 1)],
                            ref="p3-batch2")], "needs_human": []}
    out = xbar.render(st, tick_age=5, stale=180)
    assert title(out).startswith("ilk2 ▶ 1")
    assert "fold issues" in out and "p3-batch2" in out and "attempt 1" in out


def test_needs_you_wins_and_offers_the_clear_command():
    st = {"batches": [batch("b-1", "blocked", blocked_reason="attempts: 3 red"),
                      batch("b-2", "active", [("x", "pending", 1)])],
          "needs_human": [row("nh-1", "b-1")]}
    out = xbar.render(st, tick_age=5, stale=180)
    assert title(out).startswith("ilk2 ⚠ 1")  # one batch needs you; the running one does not count
    assert "attempts" in out and "ilk2 resume b-1" in out
    assert any(line.startswith("--") and "copy" in line for line in out.splitlines())


def test_blocked_without_a_row_still_counts():
    st = {"batches": [batch("b-1", "blocked", blocked_reason="dependency: p")], "needs_human": []}
    assert title(xbar.render(st, tick_age=5, stale=180)).startswith("ilk2 ⚠ 1")


def test_stale_supervisor_is_an_error_whatever_else_holds():
    st = {"batches": [], "needs_human": []}
    assert title(xbar.render(st, tick_age=None, stale=180)).startswith("ilk2 ✗")
    assert title(xbar.render(st, tick_age=900, stale=180)).startswith("ilk2 ✗")


def test_cli_prints_a_menu(ilk, capsys):
    from ilk2 import cli
    assert cli.main(["xbar"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("ilk2 ") and "---" in out


def test_running_shows_progress_phase_and_steps():
    # Chad, Oct 13: "I don't see any progress". Step k of n, the attempt, the live phase and
    # its age; every step in a submenu with its state and commit.
    b = batch("b-3", "active", [("D-523 pin flip", "accepted", 1), ("D-469 drain pins", "pending", 1)],
              ref="p4-batch1", project="gh-resolve")
    b["plans"][0]["steps"][0]["commit"] = "efef1fdacd0000"
    b["live"] = {"phase": "dispatch", "phase_age_s": 725, "attempt_age_s": 740}
    out = xbar.render({"batches": [b], "needs_human": []}, tick_age=5, stale=180)
    line = next(x for x in out.splitlines() if x.startswith("b-3"))
    assert "1/2 done" in line and "attempt 1" in line and "dispatch 12m" in line
    subs = [x for x in out.splitlines() if x.startswith("--")]
    assert any("✓" in x and "D-523" in x and "efef1fd" in x for x in subs)
    assert any("▶" in x and "D-469" in x for x in subs)


def test_live_phase_is_read_from_the_run(ilk):
    from conftest import one_step
    from ilk2 import clock, xbar as xb
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    rid = ilk.new_run(bid)
    ilk.s.update("runs", rid, {"status": "running", "phase": "gates",
                               "phase_started_mono": clock.mono() - 125}, actor="test", reason="x")
    live = xb.live_runs(ilk.s)
    assert live[bid]["phase"] == "gates" and 120 <= live[bid]["phase_age_s"] < 200


def test_progress_counts_done_steps_when_a_repair_plan_sorts_first():
    # Chad, Oct 14: the P5 batch read "step 2/15" with 12 of 15 accepted (repairs sort first).
    b = batch("b-4", "active", [("work 1", "accepted", 1), ("work 2", "accepted", 1)], project="gh-resolve")
    b["plans"].insert(0, {"slug": "repair-1", "status": "active", "repair_of": "b-4.p.0",
                          "steps": [{"title": "Repair t", "status": "pending", "attempts": 3}]})
    out = xbar.render({"batches": [b], "needs_human": []}, tick_age=5, stale=180)
    line = next(x for x in out.splitlines() if x.startswith("b-4"))
    assert "2/3 done (+1 repair)" in line and "repair-1" in line and "attempt 3" in line


def test_a_batch_with_lanes_shows_them_and_marks_each_lanes_step():
    b = batch("b-5", "active", [("p1 work", "pending", 1)], project="ilk-skills")
    b["plans"][0]["slug"] = "p1"
    b["plans"].append({"slug": "p2", "status": "active", "steps": [{"title": "p2 work", "status": "pending", "attempts": 1}]})
    b["live"] = {"phase": "dispatch", "phase_age_s": 60, "attempt_age_s": 60, "lanes": ["p1", "p2"]}
    out = xbar.render({"batches": [b], "needs_human": []}, tick_age=5, stale=180)
    line = next(x for x in out.splitlines() if x.startswith("b-5"))
    assert "2 lanes" in line
    assert sum(1 for x in out.splitlines() if x.startswith("--▶")) == 2
