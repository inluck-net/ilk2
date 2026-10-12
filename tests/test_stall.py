"""The stall suite (docs/02 §2.11.4): one injected fault per case, real supervisor and drivers.

Drivers run as subprocesses (`python -m ilk2 drive`) against the stub worker. Time is
compressed with ILK2_TIME_SCALE=60 (attempt cap 60 s, phase bound 10 s, tick 0.5 s) and
single bounds overridden with ILK2_BOUNDS where a case needs it.
Cases implemented elsewhere: S3, S15, S17, S21 (pytest), S25 in test_gates / test_worker;
S5, S7, S8, S10, S14, S19 in test_driver; S9 in test_plans. S20 and S21 (vitest) wait for
the vitest adapter.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import gate, one_step

from ilk2 import batches, bounds, clock, health, lease, notify, procs, supervisor
from ilk2.store import Store

GOOD = {"write": "calc.py", "text": "def add(a, b):\n    return a + b\n"}
BAD = {"write": "calc.py", "text": "def add(a, b):\n    return a - b\n"}


@pytest.fixture
def stall(ilk, monkeypatch):
    monkeypatch.setenv("ILK2_TIME_SCALE", "60")
    monkeypatch.setenv("ILK2_NOTIFY", "log")
    monkeypatch.delenv("ILK2_FAULTS", raising=False)
    ilk.make_repo()
    ilk.project()
    return ilk


def sup(ilk, **kw) -> supervisor.Supervisor:
    s = supervisor.Supervisor(ilk.s, load_check=False, **kw)
    s.start()
    return s


def until(sp: supervisor.Supervisor, pred, timeout: float = 90) -> bool:
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            sp.tick()
            if pred():
                return True
            time.sleep(sp.b.tick)
        return False
    finally:
        for rid, p in list(sp.children.items()):
            if p.poll() is None:
                procs.terminate_group(p.pid, p.poll, 0.5, 1)


def status(ilk, bid):
    return ilk.s.get("batches", bid)["status"]


def reasons(ilk, bid):
    return [r["terminal_reason"] for r in ilk.s.all(
        "SELECT terminal_reason FROM runs WHERE batch_id = ? ORDER BY created_at", bid)]


# -- S1: driver hangs in prepare -------------------------------------------------------------

def test_s1_driver_hangs_in_prepare(stall, monkeypatch):
    monkeypatch.setenv("ILK2_FAULTS", "phase-prepare=hang-once")
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "shipped")
    assert reasons(stall, bid)[0] == "stalled (prepare)"


# -- S2: worker hangs ------------------------------------------------------------------------

def test_s2_worker_hang_is_a_timeout(stall, monkeypatch):
    monkeypatch.setenv("ILK2_BOUNDS", json.dumps({"attempt_cap": 3}))
    stall.script([{"hang": True}], [GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "shipped")
    outs = [a["outcome"] for a in stall.s.all("SELECT * FROM attempts ORDER BY created_at")]
    assert outs == ["red", "accepted"]
    first = json.loads(stall.s.all("SELECT timings FROM attempts ORDER BY created_at")[0]["timings"])
    assert first["outcome"] == "timeout"
    assert reasons(stall, bid) == ["shipped"]


# -- S4: gate runner missing ------------------------------------------------------------------

def test_s4_runner_missing_blocks_at_once(stall, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    runner = bindir / "myrunner"
    runner.write_text("#!/bin/sh\nexec python -m pytest \"$@\"\n")
    runner.chmod(0o755)
    cfg = batches.project_config(stall.s, "proj")
    cfg.path_prelude = [str(bindir), *cfg.path_prelude]
    from dataclasses import asdict
    stall.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    stall.script([GOOD])
    g = {"name": "unit", "adapter": "pytest", "command": ["myrunner", "-q", "tests/test_calc.py"]}
    bid = stall.submit(one_step(gates_=[g]))
    runner.unlink()  # the runner disappears after submit
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked")
    assert stall.stub_calls() == 0
    assert [r["reason"] for r in stall.s.open_human_rows(bid)] == ["base-unmeasured"]


# -- S6: driver killed at every start ----------------------------------------------------------

def test_s6_crash_loop(stall, monkeypatch):
    monkeypatch.setenv("ILK2_FAULTS", "phase-recover")
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked", timeout=30)
    assert reasons(stall, bid) == ["interrupted"] * 3
    rows = stall.s.open_human_rows(bid)
    assert [r["reason"] for r in rows] == ["crash-loop"]


# -- S11: supervisor killed mid-batch ----------------------------------------------------------

def test_s11_supervisor_killed_mid_batch(stall):
    stall.script([{"sleep": 3}, GOOD])
    bid = stall.submit(one_step())
    env = {**os.environ}
    p = subprocess.Popen([sys.executable, "-m", "ilk2", "supervise"], env=env,
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.monotonic() + 30
    run = None
    while time.monotonic() < end:
        run = stall.s.one("SELECT * FROM runs WHERE batch_id = ? AND phase = 'dispatch'", bid)
        if run:
            break
        time.sleep(0.2)
    assert run, "driver never reached dispatch"
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    driver_pid, driver_start = run["pid"], run["proc_start"]
    assert procs.is_same_process(driver_pid, driver_start)  # the driver survived its supervisor
    sp = sup(stall)  # reconcile: terminate, never adopt
    assert not procs.is_same_process(driver_pid, driver_start)
    assert until(sp, lambda: status(stall, bid) == "shipped")
    assert reasons(stall, bid)[0] == "lost"


# -- S12: unacknowledged human row ------------------------------------------------------------

def test_s12_one_row_renotified_no_retries(stall):
    stall.script([BAD], [BAD], [BAD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked")
    rows = stall.s.open_human_rows(bid)
    assert len(rows) == 1
    b = bounds.load()
    assert notify.due(stall.s, b) == 1
    assert notify.due(stall.s, b) == 0
    with stall.s.tx() as c:
        c.execute("UPDATE needs_human SET last_notified_at = last_notified_at - ?", (b.renotify_first + 1,))
    assert notify.due(stall.s, b) == 1
    runs_before = len(reasons(stall, bid))
    for _ in range(5):
        sp.tick()
    assert len(reasons(stall, bid)) == runs_before  # 0 automatic retries


# -- S13: supervisor alive but its loop blocked ------------------------------------------------

def test_s13_hung_supervisor_is_kickstarted(stall, monkeypatch):
    monkeypatch.setenv("ILK2_FAULTS", "supervisor-tick=hang-once")
    stall.script([GOOD])
    bid = stall.submit(one_step())
    env = {**os.environ}
    hung = subprocess.Popen([sys.executable, "-m", "ilk2", "supervise"], env=env,
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    restart = [sys.executable, "-c",
               "import os, signal, subprocess, sys\n"
               f"os.kill({hung.pid}, signal.SIGKILL)\n"
               "subprocess.Popen([sys.executable, '-m', 'ilk2', 'supervise'], start_new_session=True,"
               " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"]
    monkeypatch.setenv("ILK2_KICKSTART_CMD", json.dumps(restart))
    b = bounds.load()
    deadline = time.monotonic() + 60
    kicked = False
    try:
        while time.monotonic() < deadline and status(stall, bid) != "shipped":
            r = health.check(stall.s, b)
            kicked = kicked or r["kickstarted"]
            time.sleep(1)
        assert kicked and status(stall, bid) == "shipped"
    finally:
        row = stall.s.one("SELECT pid FROM supervisor WHERE id = 1")
        for pid in {hung.pid, row["pid"]}:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


# -- S16: host sleep ---------------------------------------------------------------------------

def test_s16_host_sleep_is_recorded_not_a_stall(stall, monkeypatch):
    sp = sup(stall)
    real_wall = clock.wall
    monkeypatch.setattr(clock, "wall", lambda: real_wall() + 7200)  # wall jumps, monotonic does not
    sp.tick()
    kinds = [e["kind"] for e in stall.s.events_since(0)]
    assert "host-slept" in kinds


# -- S18: store locked ------------------------------------------------------------------------

def test_s18_store_busy_ends_run_not_hung(stall):
    stall.script([GOOD])
    bid = stall.submit(one_step())
    rid = stall.new_run(bid)
    from ilk2.driver import Driver
    from ilk2 import paths
    locker = sqlite3.connect(paths.db(), isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    s2 = Store.__new__(Store)
    s2.path, s2.retries, s2._in_tx = str(paths.db()), 1, False
    s2.conn = sqlite3.connect(paths.db(), timeout=0.05, isolation_level=None, check_same_thread=False)
    s2.conn.row_factory = sqlite3.Row
    t0 = time.monotonic()
    try:
        reason = Driver(s2, rid).run()  # cannot write: ends with store-busy, does not hang
    finally:
        locker.execute("ROLLBACK")
    assert reason == "store-busy" and time.monotonic() - t0 < 5
    # the run row could not be ended; the supervisor reaps the exited driver and requeues
    assert stall.s.get("runs", rid)["status"] == "starting"


# -- S22: lease races --------------------------------------------------------------------------

def test_s22_lease_races(stall):
    stall.script([GOOD])
    bid = stall.submit(one_step())
    root = stall.s.get("batches", bid)["execution_root"]
    head = stall.git(root, "rev-parse", "HEAD")
    g = lease.driver_release(stall.s, root, head)
    # (a) consumer acquire and a resume at the same generation: exactly one wins
    wins = 0
    for fn in (lambda: lease.consumer_acquire(stall.s, root, g, head, "gh", head, 60),
               lambda: lease.driver_acquire(stall.s, root, g)):
        try:
            fn()
            wins += 1
        except Exception:
            pass
    assert wins == 1
    # reset to released for the next cases
    row = lease.get(stall.s, root)
    if row["holder"] == "consumer":
        g = lease.consumer_release(stall.s, root, row["generation"], head)
    else:
        g = lease.driver_release(stall.s, root, head)
    # (b) stale expected head refused
    with pytest.raises(Exception):
        lease.consumer_acquire(stall.s, root, g, "0" * 40, "gh", head, 60)
    # (c) expiry fences: consumer-expired, resume fails, the late release succeeds
    g = lease.consumer_acquire(stall.s, root, g, head, "gh", head, -1)
    assert lease.expire_due(stall.s) == [root]
    assert lease.get(stall.s, root)["holder"] == "consumer-expired"
    batches.park(stall.s, bid)
    with pytest.raises(batches.SubmitRejected):
        batches.resume(stall.s, bid)
    g = lease.consumer_release(stall.s, root, lease.get(stall.s, root)["generation"], head)
    assert lease.get(stall.s, root)["holder"] == "released"
    # (d) a renewing consumer does not expire
    g = lease.consumer_acquire(stall.s, root, g, head, "gh", head, 60)
    g = lease.consumer_renew(stall.s, root, g, 60)
    assert lease.expire_due(stall.s) == []


# -- S23: cancel mid-attempt and while accepting ------------------------------------------------

def test_s23_cancel_mid_attempt(stall):
    stall.script([{"sleep": 30}, GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: stall.s.one("SELECT 1 FROM runs WHERE batch_id = ? AND phase = 'dispatch'", bid),
                 timeout=30)
    batches.cancel(stall.s, bid, "issue-closed")
    assert until(sp, lambda: not stall.s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status != 'ended'", bid),
                 timeout=30)
    assert status(stall, bid) == "cancelled"
    root = stall.s.get("batches", bid)["execution_root"]
    assert lease.get(stall.s, root)["holder"] == "released"


def test_s23_cancel_while_accepting(stall, monkeypatch):
    monkeypatch.setenv("ILK2_FAULTS", "after-intent=hang-once")
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: stall.s.one("SELECT 1 FROM steps WHERE batch_id = ? AND status = 'accepting'", bid),
                 timeout=30)
    batches.cancel(stall.s, bid, "stand-down")
    assert until(sp, lambda: not stall.s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status != 'ended'", bid),
                 timeout=30)
    step = stall.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    assert step["status"] == "pending"  # recovered: undo (no commit had been made)
    root = stall.s.get("batches", bid)["execution_root"]
    assert lease.get(stall.s, root)["holder"] == "released"


# -- S24: setup hook ---------------------------------------------------------------------------

def _set_setup(ilk, cmd):
    from dataclasses import asdict
    cfg = batches.project_config(ilk.s, "proj")
    cfg.setup = {"command": cmd, "cap_s": 60}
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")


def test_s24_setup_installs_then_base_measures(stall):
    _set_setup(stall, ["python", "-c", "import os; os.makedirs('node_modules', exist_ok=True)"])
    (stall.repo / "tests" / "test_nm.py").write_text(
        "import os\ndef test_nm():\n    assert os.path.isdir('node_modules')\n")
    stall.git(stall.repo, "add", ".")
    stall.git(stall.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "nm")
    stall.script([GOOD])
    bid = stall.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_nm.py")]))
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "shipped")


def test_s24_failing_setup_blocks_setup_not_base(stall):
    _set_setup(stall, ["python", "-c", "import sys; sys.exit(3)"])
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked")
    assert [r["reason"] for r in stall.s.open_human_rows(bid)] == ["setup"]


# -- S26: resolver project, consumer policy ----------------------------------------------------

def test_s26_consumer_policy_no_page(stall, monkeypatch):
    from dataclasses import asdict
    cfg = batches.project_config(stall.s, "proj")
    cfg.notify_policy = "consumer"
    stall.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    stall.script([BAD], [BAD], [BAD])
    bid = stall.submit(one_step(), consumer_ref="issue-42")
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked")
    evs = [e for e in stall.s.events_since(0) if e["kind"] == "needs_human"]
    assert evs and evs[0]["consumer_ref"] == "issue-42"
    b = bounds.load()
    assert notify.due(stall.s, b) == 0  # no macOS, no off-host page
    with stall.s.tx() as c:
        c.execute("UPDATE needs_human SET created_at = created_at - ?", (b.consumer_offhost + 1,))
    assert notify.due(stall.s, b) == 1
    assert notify.due(stall.s, b) == 0  # one notice, never re-sent


# -- S27: host load admission ------------------------------------------------------------------

def test_s27_host_load_refuses_then_clears(stall, monkeypatch):
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = supervisor.Supervisor(stall.s, load_check=True)
    sp.start()
    monkeypatch.setattr(os, "getloadavg", lambda: (99.0, 99.0, 99.0))
    sp.tick()
    assert status(stall, bid) == "queued"
    assert [r["reason"] for r in stall.s.open_refusals(bid)] == ["host-load"]
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(supervisor, "free_memory", lambda: 8 * 1024 ** 3)
    for _ in range(3):
        stall.s.event("gate.timeout", {"gate": "x"})
    sp.tick()
    assert status(stall, bid) == "queued"
    assert "gate timeouts" in stall.s.open_refusals(bid)[0]["detail"]
    with stall.s.tx() as c:  # the timeouts age out of the 30-minute window (time compressed)
        c.execute("UPDATE events SET mono = mono - ? WHERE kind = 'gate.timeout'",
                  (sp.b.load_timeout_window + 1,))
    assert until(sp, lambda: status(stall, bid) == "shipped")
    assert stall.s.open_refusals(bid) == []
