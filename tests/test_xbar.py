"""The ilk2 SwiftBar item (`ilk2 xbar`, contrib/xbar/ilk2.10s.sh): title states and dropdown."""

import json

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


def test_a_pending_rsi_branch_needs_attention():
    st = {"batches": [], "needs_human": [],
          "rsi": {"pending": [{"batch": "b-9", "branch": "ilk2/b-9", "ref": "rsi-auto-1"}],
                  "open": 0, "planning": None}}
    out = xbar.render(st, tick_age=5, stale=180)
    assert title(out).startswith("ilk2 ⚠ 1")
    lines = out.splitlines()
    start = next(i for i, x in enumerate(lines) if x.startswith("⚠ needs you"))
    end = next(i for i, x in enumerate(lines) if x.startswith("▶ running"))
    section = lines[start:end]
    assert "b-9 [rsi-auto-1] ilk2: RSI branch ilk2/b-9 needs review" in section
    assert any(x.startswith("--review: git log main..ilk2/b-9") and "Menlo" in x for x in section)


def test_the_rsi_section_shows_candidates_and_planning():
    st = {"batches": [], "needs_human": [],
          "rsi": {"pending": [], "open": 3, "planning": "x", "today": 1, "cap": 3}}
    lines = xbar.render(st, tick_age=5, stale=180).splitlines()
    assert "3 open candidates · planning: x · today 1/3 batches" in lines
    assert lines.index("RSI") > next(i for i, x in enumerate(lines) if x.startswith("▶ running"))
    bare = xbar.render({"batches": [], "needs_human": []}, tick_age=5, stale=180)
    assert "RSI" not in bare.splitlines() and "open candidates" not in bare


def test_the_rsi_section_shows_the_landing_in_progress_with_its_stage():
    now = 1_729_000_000.0

    def st(landing):
        return {"batches": [], "needs_human": [],
                "rsi": {"pending": [], "open": 0, "planning": None, "now": now,
                        "landing": landing, "recent": [], "today": 0, "cap": 3,
                        "landing_on": True, "repo": "/repo"}}

    for landing, stage in [
        ({"status": "merged", "version": "v2", "batch_id": "b-1", "title": "fix x"}, "merged"),
        # released 5 min ago: 10 of the 15 canary minutes are left
        ({"status": "released", "version": "v2", "batch_id": "b-1", "title": "fix x",
          "released_at": now - 300}, "released, canary 10m left"),
        # verified 1 h ago: SOAK_S - CANARY_S after that is 22h45m out, so 22h
        ({"status": "verified", "version": "v2", "batch_id": "b-1", "title": "fix x",
          "verified_at": now - 3600}, "verified, 22h until the rezmac push"),
    ]:
        lines = xbar.render(st(landing), tick_age=5, stale=180).splitlines()
        assert f"landing now: {stage} · v2  b-1  fix x" in lines
    missing = xbar.render(st({"status": "merged", "batch_id": "b-1"}), tick_age=5, stale=180)
    assert "landing now: merged · ?  b-1  ?" in missing.splitlines()
    bare = xbar.render({"batches": [], "needs_human": [], "rsi": {"pending": [], "open": 0}},
                       tick_age=5, stale=180)
    assert "landing now" not in bare


def test_the_rsi_section_lists_recent_landings_with_a_git_show_copy():
    recent = [{"status": "pushed", "version": f"v{i}", "batch_id": f"b-{i}", "title": f"fix {i}",
               "merge_commit": f"deadbee{i}"} for i in range(6, 0, -1)]  # newest first
    st = {"batches": [], "needs_human": [],
          "rsi": {"pending": [], "open": 0, "planning": None, "now": 0, "landing": None,
                  "recent": recent, "today": 0, "cap": 3, "landing_on": False, "repo": "/repo"}}
    lines = xbar.render(st, tick_age=5, stale=180).splitlines()
    assert [x for x in lines if x.startswith("v")] == \
        [f"v{i}  fix {i}  pushed" for i in range(6, 1, -1)]  # five rows, no sixth
    subs = [x for x in lines if x.startswith("--git -C")]
    assert len(subs) == 5
    for i, sub in zip(range(6, 1, -1), subs):
        assert f"param2=" in sub and f"git -C /repo show --stat deadbee{i}" in sub
        assert "pbcopy" in sub and "terminal=false" in sub
    st["rsi"]["repo"] = None
    assert not [x for x in xbar.render(st, tick_age=5, stale=180).splitlines()
                if x.startswith("--git -C")]


def test_the_rsi_section_reports_the_daily_cap_and_the_landing_switch():
    st = {"batches": [], "needs_human": [],
          "rsi": {"pending": [], "open": 3, "planning": "x", "now": 0, "landing": None,
                  "recent": [], "today": 1, "cap": 3, "landing_on": True, "repo": None}}
    lines = xbar.render(st, tick_age=5, stale=180).splitlines()
    assert "3 open candidates · planning: x · today 1/3 batches" in lines
    assert "landing: on" in lines
    st["rsi"]["landing_on"] = False
    lines = xbar.render(st, tick_age=5, stale=180).splitlines()
    assert "landing: off (rsi_land)" in lines and "landing: on" not in lines


def test_the_title_carries_the_canary_sign_while_a_release_is_in_its_canary():
    def st(landing, batches=None, needs=None):
        return {"batches": batches or [], "needs_human": needs or [],
                "rsi": {"pending": [], "open": 0, "planning": None, "now": 0,
                        "landing": landing, "recent": [], "today": 0, "cap": 3,
                        "landing_on": True, "repo": None}}

    canary = {"status": "released", "released_at": 0, "version": "v2", "batch_id": "b-1",
              "title": "fix x"}
    assert title(xbar.render(st(canary), tick_age=5, stale=180)) == "ilk2 ok ↻"
    run = [batch("b-1", "active", [("x", "pending", 1)])]
    assert title(xbar.render(st(canary, run), tick_age=5, stale=180)) == "ilk2 ▶ 1 ↻"
    need = [batch("b-1", "blocked", blocked_reason="x"), batch("b-2", "blocked", blocked_reason="y")]
    assert title(xbar.render(st(canary, need), tick_age=5, stale=180)) == "ilk2 ⚠ 2 ↻ | color=orange"
    # the red title never signs, whatever the landing holds
    assert title(xbar.render(st(canary), tick_age=None, stale=180)) == "ilk2 ✗ | color=red"
    # and only a release in its canary signs
    for landing in (None, {"status": "merged"}, {"status": "verified"}, {"status": "pushed"}):
        assert title(xbar.render(st(landing), tick_age=5, stale=180)) == "ilk2 ok"


def test_main_carries_the_rsi_state_and_keeps_rendering_when_rsi_fails(ilk, capsys, monkeypatch):
    from ilk2 import rsi
    monkeypatch.setattr(rsi, "pending", lambda s: [{"id": "b-9", "branch": "ilk2/b-9",
                                                   "consumer_ref": "rsi-auto-1"}])
    monkeypatch.setattr(rsi, "candidates", lambda s: [])
    ilk.s.insert("rsi_candidates", {"id": "c-1", "source": "driver-error", "title": "planning one",
                                    "evidence": "[]", "hits": 1, "score": 50, "status": "planning",
                                    "first_seen": 1.0, "last_seen": 1.0})
    assert xbar.main() == 0
    out = capsys.readouterr().out
    assert "b-9 [rsi-auto-1] ilk2: RSI branch ilk2/b-9 needs review" in out
    assert "0 open candidates · planning: planning one" in out

    def boom(s):
        raise RuntimeError("no rsi_candidates")  # a store without the table, or any rsi error
    monkeypatch.setattr(rsi, "pending", boom)
    assert xbar.main() == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("ilk2 ") and "open candidates" not in out
    assert "landing now" not in out and "landing:" not in out


def test_a_branch_awaiting_landing_is_not_a_needs_you_item():
    # Oct 15: "awaits review" read as a hold while the branch only waited for the next pass.
    st = {"batches": [], "needs_human": [],
          "rsi": {"pending": [{"batch": "b-1", "branch": "ilk2/b-1", "ref": "rsi-auto-3", "review": False}],
                  "open": 1, "landing_on": True,
                  "last_tick": {"did": "nothing", "why": "daily cap 3 reached"}}}
    out = xbar.render(st, tick_age=5, stale=180)
    assert out.splitlines()[0] == "ilk2 ok"
    assert "b-1 [rsi-auto-3] awaits landing" in out and "needs review" not in out
    assert "trigger: nothing (daily cap 3 reached)" in out


def _running(ilk, model=None, init=True):
    """A running batch whose live attempt's worker event log records `model` in its init event."""
    from conftest import one_step
    from ilk2 import clock
    bid = ilk.submit(one_step())
    rid = ilk.new_run(bid)
    step = ilk.s.one("SELECT id FROM steps WHERE batch_id = ?", bid)
    aid = f"a-{rid}"
    ilk.s.insert("attempts", {"id": aid, "run_id": rid, "step_id": step["id"], "n": 1,
                              "status": "running", "created_at": clock.wall()})
    logs = ilk.home / "data" / "runs" / rid / aid / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    ev = {"type": "system", "subtype": "init", "model": model, "session_id": "s-1"} if init \
        else {"type": "assistant", "message": {}}
    (logs / "start.jsonl").write_text(json.dumps({"t": 0.1, "ev": ev}) + "\n")
    return bid, rid, aid


def _end(ilk, rid, aid):
    from ilk2 import clock
    ilk.s.update("attempts", aid, {"status": "done", "outcome": "accepted"},
                 actor="test", reason="x")
    ilk.s.update("runs", rid, {"status": "ended", "terminal_reason": "done",
                               "ended_at": clock.wall()}, actor="test", reason="x")


def test_the_menu_shows_the_configured_worker_model(ilk, capsys):
    tmpl = ilk.home / "worker-template" / "settings.json"
    tmpl.write_text(json.dumps({"model": "cfg-model"}))
    assert xbar.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert "worker model: cfg-model" in lines
    assert lines.index("---") < lines.index("worker model: cfg-model") < \
        next(i for i, x in enumerate(lines) if x.startswith("⚠ needs you"))

    # `env.ANTHROPIC_MODEL` is the fallback when the template sets no `model` key
    tmpl.write_text(json.dumps({"env": {"ANTHROPIC_MODEL": "env-model"}}))
    assert xbar.main() == 0
    assert "worker model: env-model" in capsys.readouterr().out.splitlines()

    # neither key set, and no template file at all: the whole line is absent
    tmpl.write_text(json.dumps({"env": {}}))
    assert xbar.main() == 0
    assert "worker model" not in capsys.readouterr().out
    tmpl.unlink()
    assert xbar.main() == 0
    assert "worker model" not in capsys.readouterr().out


def test_the_worker_model_line_shows_the_live_models_only_when_they_differ(ilk, capsys):
    # the template the `ilk` pin writes configures stub-model
    ilk.make_repo()
    ilk.project()
    bid, rid, aid = _running(ilk, model="stub-model")
    assert xbar.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert [x for x in lines if x.startswith("worker model:")] == ["worker model: stub-model"]
    _end(ilk, rid, aid)

    bid, rid, aid = _running(ilk, model="other-model")
    assert xbar.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert "worker model: stub-model  (live: other-model)" in lines
    _end(ilk, rid, aid)

    # several distinct live models: every one of them, sorted and comma-separated
    _running(ilk, model="b-model")
    _running(ilk, model="a-model")
    assert xbar.main() == 0
    lines = capsys.readouterr().out.splitlines()
    assert "worker model: stub-model  (live: a-model, b-model)" in lines


def test_each_running_batch_shows_the_model_its_live_attempt_runs(ilk, capsys):
    ilk.make_repo()
    ilk.project()
    b1, _, _ = _running(ilk, model="m-live")
    b2, _, _ = _running(ilk, init=False)  # the worker has not emitted its init event yet
    assert xbar.main() == 0
    lines = capsys.readouterr().out.splitlines()

    def subs(bid):  # the batch block's own submenu lines (status() lists newest first)
        head = next(i for i, x in enumerate(lines) if x.startswith(f"{bid}  "))
        out = []
        for x in lines[head + 1:]:
            if not x.startswith("--"):
                break
            out.append(x)
        return out

    s1 = subs(b1)
    assert s1[0] == "--model: m-live"  # first submenu line, before the step lines
    assert any(x.startswith(("--▶", "--·")) for x in s1[1:])
    assert not any(x.startswith("--model:") for x in subs(b2))


def test_the_worker_template_never_leaks_other_keys_into_the_menu(ilk, capsys):
    (ilk.home / "worker-template" / "settings.json").write_text(
        json.dumps({"model": "cfg-model", "env": {"ANTHROPIC_API_TOKEN": "sekret-token"},
                    "apiKeyHelper": "sekret-helper"}))
    assert xbar.main() == 0
    out = capsys.readouterr().out
    assert "worker model: cfg-model" in out.splitlines()
    assert "sekret-token" not in out and "sekret-helper" not in out


def test_queued_batches_are_not_counted_as_running():
    # Chad, Oct 16: the title said "ilk2 ▶ 5" while 3 ran and 2 waited for a host slot.
    st = {"batches": [batch("b-q1", "queued", [("x", "pending", 0)]),
                      batch("b-a1", "active", [("y", "pending", 1)]),
                      batch("b-q2", "queued", [("z", "pending", 0)]),
                      batch("b-a2", "active", [("w", "pending", 1)])], "needs_human": []}
    out = xbar.render(st, tick_age=5, stale=180)
    assert title(out) == "ilk2 ▶ 2 (+2 queued)"
    assert "▶ running (2) · queued (2)" in out
    ids = [ln.split()[0] for ln in out.splitlines() if ln.startswith("b-")]
    assert ids == ["b-a1", "b-a2", "b-q1", "b-q2"]               # running first
