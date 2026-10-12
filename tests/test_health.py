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
