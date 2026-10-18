import json

from ilk2 import bounds, health


def kicks(tmp_path, monkeypatch):
    log = tmp_path / "kicks"
    monkeypatch.setenv("ILK2_KICKSTART_CMD", json.dumps(["/bin/sh", "-c", f"echo k >> {log}"]))
    return lambda: len(log.read_text().splitlines()) if log.exists() else 0


def test_never_ticked_waits_for_the_startup_bound(ilk, tmp_path, monkeypatch):
    n = kicks(tmp_path, monkeypatch)
    b = bounds.Bounds(supervisor_startup=300)
    assert health.check(ilk.s, b)["kickstarted"] is False and n() == 0   # first sight: grace
    with ilk.s.tx() as c:
        c.execute("UPDATE meta SET value = value - 301 WHERE key = 'health_first_unticked'")
    assert health.check(ilk.s, b)["kickstarted"] is True and n() == 1    # never ticked in 5 min


def test_dead_supervisor_is_kickstarted(ilk, tmp_path, monkeypatch):
    n = kicks(tmp_path, monkeypatch)
    with ilk.s.tx() as c:
        c.execute("UPDATE supervisor SET pid = 999999, proc_start = 'x', tick_wall = 1, tick_mono = 1, "
                  "started_wall = 1, boot_time = 0")
    assert health.check(ilk.s, bounds.Bounds())["kickstarted"] is True and n() == 1


# -- RSI watched from outside (docs/08 §8.6) ---------------------------------------------------

def rsi_project(ilk, on=True):
    from test_rsi import ilk2_project
    ilk.make_repo()
    ilk2_project(ilk)
    if not on:
        cfg = json.loads(ilk.s.get("projects", "ilk2")["config"])
        cfg["rsi_auto"] = False
        ilk.s.update("projects", "ilk2", {"config": json.dumps(cfg)}, actor="t", reason="t")


def rsi_rows(ilk, states=("open", "acked")):
    return ilk.s.all(f"SELECT * FROM needs_human WHERE reason = 'rsi-health' AND state IN "
                     f"({','.join('?' * len(states))})", *states)


def tick_events(ilk, *outcomes):
    for i, o in enumerate(outcomes):
        tid = f"t{len(ilk.s.events_since(0))}-{i}"
        ilk.s.event("rsi.tick-started", {"tick_id": tid, "pid": 1})
        if o:
            ilk.s.event(f"rsi.tick-{o}", {"tick_id": tid})


def test_the_stale_bound_is_three_rsi_intervals():
    from ilk2.supervisor import Supervisor
    assert health.RSI_STALE_S == 3 * Supervisor.RSI_EVERY_S


def test_rsi_health_grace_then_one_row_while_no_tick_completes_and_clears_on_recovery(ilk):
    from ilk2 import clock, rsi
    rsi_project(ilk)
    t = clock.wall()
    assert health.rsi_health(ilk.s, t)["stale"] is False and not rsi_rows(ilk)   # rollout: grace
    late = t + health.RSI_STALE_S + 1
    health.rsi_health(ilk.s, late)
    health.rsi_health(ilk.s, late + 60)
    assert len(rsi_rows(ilk)) == 1                                 # one row, not one per minute
    rsi.tick(ilk.s)                                                # completes (does nothing)
    assert health.rsi_health(ilk.s, clock.wall())["stale"] is False
    assert not rsi_rows(ilk)                                       # cleared on recovery


def test_three_failed_ticks_in_a_row_alarm_and_alternating_ones_do_not(ilk):
    from ilk2 import clock
    rsi_project(ilk)
    tick_events(ilk, "failed", "completed", "failed", "completed", "failed")
    assert health.rsi_health(ilk.s, clock.wall())["failing"] is False and not rsi_rows(ilk)
    tick_events(ilk, "failed", "failed")
    assert health.rsi_health(ilk.s, clock.wall())["failing"] is True and len(rsi_rows(ilk)) == 1
    tick_events(ilk, "completed")
    assert health.rsi_health(ilk.s, clock.wall())["failing"] is False and not rsi_rows(ilk)


def test_a_tick_with_no_ending_is_running_until_twice_the_plan_cap(ilk):
    from ilk2 import clock, rsi
    rsi_project(ilk)
    tick_events(ilk, "failed", "failed", None)
    now = clock.wall()
    assert health.rsi_health(ilk.s, now)["failing"] is False       # the third may still finish
    assert health.rsi_health(ilk.s, now + 2 * rsi.PLAN_CAP_S + 1)["failing"] is True


def test_an_acked_rsi_health_row_is_not_duplicated_and_recovery_clears_it(ilk):
    from ilk2 import clock
    rsi_project(ilk)
    tick_events(ilk, "failed", "failed", "failed")
    health.rsi_health(ilk.s, clock.wall())
    (row,) = rsi_rows(ilk)
    ilk.s.ack(row["id"], "chad")
    health.rsi_health(ilk.s, clock.wall())                         # still failing
    assert [r["id"] for r in rsi_rows(ilk)] == [row["id"]]         # no second row
    tick_events(ilk, "completed")
    health.rsi_health(ilk.s, clock.wall())
    assert not rsi_rows(ilk) and ilk.s.get("needs_human", row["id"])["state"] == "cleared"


def test_rsi_off_never_alarms_and_turning_it_on_gives_a_new_grace(ilk):
    import json as j
    from ilk2 import clock
    rsi_project(ilk, on=False)
    t = clock.wall()
    assert health.rsi_health(ilk.s, t + 10 * health.RSI_STALE_S) == {"on": False}
    cfg = j.loads(ilk.s.get("projects", "ilk2")["config"])
    cfg["rsi_auto"] = True
    ilk.s.update("projects", "ilk2", {"config": j.dumps(cfg)}, actor="t", reason="t")
    later = t + 10 * health.RSI_STALE_S
    assert health.rsi_health(ilk.s, later)["stale"] is False       # the grace starts now
    assert health.rsi_health(ilk.s, later + health.RSI_STALE_S + 1)["stale"] is True


def test_clearing_by_reason_can_target_one_goal(ilk):
    ilk.s.open_needs_human("rsi-goal", "a stopped", "reset it", project_id="ilk2", consumer_ref="goal:a")
    ilk.s.open_needs_human("rsi-goal", "b stopped", "reset it", project_id="ilk2", consumer_ref="goal:b")
    assert ilk.s.clear_needs_human_reason("rsi-goal", "ilk2", consumer_ref="goal:a") == 1
    left = ilk.s.all("SELECT consumer_ref FROM needs_human WHERE reason = 'rsi-goal' AND state = 'open'")
    assert [r["consumer_ref"] for r in left] == ["goal:b"]
