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
