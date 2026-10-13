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


def until(sp: supervisor.Supervisor, pred, timeout: float = 90, keep: bool = False) -> bool:
    """Tick until pred holds. Drivers still running are then killed, unless keep=True (the
    test goes on with them)."""
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            sp.tick()
            if pred():
                return True
            time.sleep(sp.b.tick)
        return False
    finally:
        for rid, p in ([] if keep else list(sp.children.items())):
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
    # Self-resolution: 3 restarts back off, 6 back off longer, 9 block for a human.
    monkeypatch.setenv("ILK2_FAULTS", "phase-recover")
    monkeypatch.setenv("ILK2_BOUNDS", json.dumps({"crash_backoff_first": 1, "crash_backoff_second": 2}))
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked", timeout=90)
    assert reasons(stall, bid) == ["interrupted"] * 9
    backoffs = stall.s.all("SELECT * FROM refusals WHERE batch_id = ? AND reason = 'crash-backoff'", bid)
    assert len(backoffs) == 2 and all(r["cleared_at"] for r in backoffs)
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

def opt_in(ilk, policy):
    from dataclasses import asdict
    cfg = batches.project_config(ilk.s, "proj")
    cfg.notify_policy = policy
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")


def test_s12_one_row_renotified_no_retries(stall):
    opt_in(stall, "owner")  # pages are opt-in (Q12, Chad Oct 13)
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
    kicked_after = None
    try:
        while time.monotonic() < deadline and status(stall, bid) != "shipped":
            r = health.check(stall.s, b)
            if r["kickstarted"] and kicked_after is None:
                started = stall.s.one("SELECT started_wall FROM supervisor WHERE id = 1")["started_wall"]
                kicked_after = clock.wall() - started
            time.sleep(1)
        assert kicked_after is not None and status(stall, bid) == "shipped"
        # the hung supervisor is within its start-up bound, so the bound is supervisor_startup;
        # the health loop here polls every 1 s
        assert kicked_after <= b.supervisor_startup + 2.5, kicked_after
    finally:
        row = stall.s.one("SELECT pid FROM supervisor WHERE id = 1")
        for pid in {hung.pid, row["pid"]}:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


# -- S16: host sleep ---------------------------------------------------------------------------

def test_s16_host_sleep_is_recorded_not_a_stall(stall, monkeypatch):
    stall.script([{"sleep": 4}, GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: stall.s.one("SELECT 1 FROM attempts WHERE status = 'running'"), timeout=30,
                 keep=True)
    att = stall.s.one("SELECT * FROM attempts WHERE status = 'running'")
    real_wall = clock.wall
    # The host sleeps mid-attempt: wall time jumps past the attempt cap, monotonic does not.
    monkeypatch.setattr(clock, "wall", lambda: real_wall() + sp.b.attempt_cap + 7200)
    sp.tick()
    assert "host-slept" in [e["kind"] for e in stall.s.events_since(0)]
    assert stall.s.get("attempts", att["id"])["status"] == "running"  # the same attempt goes on
    assert until(sp, lambda: status(stall, bid) == "shipped")
    assert reasons(stall, bid) == ["shipped"]  # no stall, no requeue
    assert [a["outcome"] for a in stall.s.all("SELECT outcome FROM attempts")] == ["accepted"]


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
    s2.conn.close()
    # The driver could not even end its run. A supervisor finds it with no live driver,
    # ends it as lost and requeues the batch, which then ships.
    sp = sup(stall)
    assert stall.s.get("runs", rid)["status"] == "ended"
    assert stall.s.get("runs", rid)["terminal_reason"] == "lost"
    assert until(sp, lambda: status(stall, bid) == "shipped")
    assert reasons(stall, bid) == ["lost", "shipped"]


# -- S22: lease races --------------------------------------------------------------------------

def test_s22_lease_races(stall):
    stall.script([GOOD])
    bid = stall.submit(one_step())
    root = stall.s.get("batches", bid)["execution_root"]
    head = stall.git(root, "rev-parse", "HEAD")
    batches.park(stall.s, bid)
    g = lease.driver_release(stall.s, root, head)
    # (a) a consumer acquire and `ilk2 resume` race at the same generation, on separate
    # connections released together: exactly one wins, and the loser fails for that reason.
    import threading
    from ilk2 import paths
    from ilk2.store import Conflict
    barrier = threading.Barrier(2)
    results = {}

    def race(name, fn):
        s = Store(paths.db())
        try:
            barrier.wait()
            fn(s)
            results[name] = "won"
        except (Conflict, batches.SubmitRejected) as e:
            results[name] = f"lost: {type(e).__name__}"
        finally:
            s.close()
    threads = [threading.Thread(target=race, args=("consumer", lambda s: lease.consumer_acquire(
                   s, root, g, head, "gh", head, 60))),
               threading.Thread(target=race, args=("resume", lambda s: batches.resume(s, bid)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(v == "won" for v in results.values()) == [False, True], results
    row = lease.get(stall.s, root)
    if results["consumer"] == "won":
        assert row["holder"] == "consumer" and status(stall, bid) == "parked"
        g = lease.consumer_release(stall.s, root, row["generation"], head)
    else:
        assert row["holder"] == "driver" and status(stall, bid) == "queued"
        batches.park(stall.s, bid)
        g = lease.driver_release(stall.s, root, head)
    # (b) stale expected head refused
    with pytest.raises(Exception):
        lease.consumer_acquire(stall.s, root, g, "0" * 40, "gh", head, 60)
    # (c) expiry fences: consumer-expired, resume fails, the late release succeeds
    g = lease.consumer_acquire(stall.s, root, g, head, "gh", head, -1)
    assert lease.expire_due(stall.s) == [root]
    assert lease.get(stall.s, root)["holder"] == "consumer-expired"
    with pytest.raises(batches.SubmitRejected):
        batches.resume(stall.s, bid)
    # the expired consumer pushes late, then releases: its commit is the head, no driver
    # commit came in between
    (Path(root) / "late.txt").write_text("late\n")
    stall.git(root, "add", "late.txt")
    stall.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "late push")
    late = stall.git(root, "rev-parse", "HEAD")
    assert stall.git(root, "rev-parse", "HEAD~1") == head
    g = lease.consumer_release(stall.s, root, lease.get(stall.s, root)["generation"], late)
    assert lease.get(stall.s, root)["holder"] == "released"
    assert lease.get(stall.s, root)["expected_head"] == late
    head = late
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
                 timeout=30, keep=True)
    att = stall.s.one("SELECT * FROM attempts WHERE status = 'running'")
    root = stall.s.get("batches", bid)["execution_root"]
    end = time.monotonic() + 10
    while not procs.processes_in(root) and time.monotonic() < end:
        time.sleep(0.1)
    assert procs.processes_in(root), "the worker should be running in the root"
    assert all(p.poll() is None for p in sp.children.values())  # the driver is alive
    batches.cancel(stall.s, bid, "issue-closed")
    assert until(sp, lambda: not stall.s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status != 'ended'", bid),
                 timeout=30)
    assert status(stall, bid) == "cancelled"
    assert lease.get(stall.s, root)["holder"] == "released"
    assert_cancelled_hierarchy(stall, bid)
    a = stall.s.get("attempts", att["id"])
    assert a["status"] == "done" and a["outcome"] in ("interrupted", "cancelled")
    assert procs.processes_in(root) == []  # the worker is gone
    assert stall.stub_calls() == 1  # cancelled, not retried


def assert_cancelled_hierarchy(ilk, bid):
    # Codex M1 review #1: the whole hierarchy ends, not only the batch and run.
    assert [p["status"] for p in ilk.s.all("SELECT status FROM plans WHERE batch_id = ?", bid)] == ["cancelled"]
    assert {s["status"] for s in ilk.s.all("SELECT status FROM steps WHERE batch_id = ?", bid)} == {"cancelled"}
    assert ilk.s.all("SELECT a.id FROM attempts a JOIN steps s ON s.id = a.step_id "
                     "WHERE s.batch_id = ? AND a.status = 'running'", bid) == []


def test_s23_cancel_while_accepting(stall, monkeypatch):
    monkeypatch.setenv("ILK2_FAULTS", "after-intent=hang-once")
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: stall.s.one("SELECT 1 FROM steps WHERE batch_id = ? AND status = 'accepting'", bid),
                 timeout=30, keep=True)
    assert all(p.poll() is None for p in sp.children.values())  # the driver hangs in accepting
    batches.cancel(stall.s, bid, "stand-down")
    assert until(sp, lambda: not stall.s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status != 'ended'", bid),
                 timeout=30)
    root = stall.s.get("batches", bid)["execution_root"]
    assert status(stall, bid) == "cancelled"
    assert lease.get(stall.s, root)["holder"] == "released"
    assert_cancelled_hierarchy(stall, bid)  # recovered by undo (no commit), then cancelled
    # Codex M1 review #4: undo resets the worktree before the root is released
    assert stall.git(root, "status", "--porcelain") == ""


def test_cancel_of_a_queued_batch_ends_its_hierarchy(stall):
    bid = stall.submit(one_step())
    batches.cancel(stall.s, bid, "human")
    assert_cancelled_hierarchy(stall, bid)


def test_cancel_keeps_the_root_while_a_process_is_in_it(stall):
    # Codex M1 review #5: a process left in the root fences it; the lease is released only
    # after a later check finds the root empty.
    bid = stall.submit(one_step())
    root = stall.s.get("batches", bid)["execution_root"]
    stray = subprocess.Popen(["sleep", "60"], cwd=root, start_new_session=True)
    try:
        sp = sup(stall)
        batches.cancel(stall.s, bid, "human")
        sp.tick()
        assert lease.get(stall.s, root)["holder"] == "driver"
        assert [r["reason"] for r in stall.s.open_human_rows(bid)] == ["unkillable"]
    finally:
        stray.kill()
        stray.wait()
    sp.tick()
    assert lease.get(stall.s, root)["holder"] == "released"
    assert_cancelled_hierarchy(stall, bid)


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
    base = stall.s.all("SELECT * FROM base_measurements WHERE batch_id = ?", bid)
    assert len(base) == 1 and "tests/test_nm.py::test_nm" not in json.loads(base[0]["failures"])


def test_s24_failing_setup_blocks_setup_not_base(stall):
    _set_setup(stall, ["python", "-c", "import sys; sys.exit(3)"])
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked")
    assert [r["reason"] for r in stall.s.open_human_rows(bid)] == ["setup"]
    assert stall.s.one("SELECT COUNT(*) n FROM base_measurements WHERE batch_id = ?", bid)["n"] == 0
    assert stall.s.one("SELECT COUNT(*) n FROM gate_results")["n"] == 0
    assert stall.stub_calls() == 0


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
    from ilk2 import paths

    def sent():
        log = paths.logs() / "notify.log"
        return [json.loads(x)["text"] for x in log.open()] if log.exists() else []
    assert notify.due(stall.s, b) == 0
    assert sent() == []  # no macOS, no off-host page
    with stall.s.tx() as c:
        c.execute("UPDATE needs_human SET created_at = created_at - ?", (b.consumer_offhost + 1,))
    assert notify.due(stall.s, b) == 1
    assert notify.due(stall.s, b) == 0  # one notice, never re-sent
    out = sent()
    assert len(out) == 1 and out[0].startswith("[offhost]"), out


# -- S27: host load admission ------------------------------------------------------------------

def assert_nothing_ran(ilk, bid):
    assert ilk.s.one("SELECT COUNT(*) n FROM runs WHERE batch_id = ?", bid)["n"] == 0
    assert ilk.s.one("SELECT COUNT(*) n FROM gate_results")["n"] == 0
    assert ilk.s.one("SELECT COUNT(*) n FROM base_measurements WHERE batch_id = ?", bid)["n"] == 0
    assert ilk.stub_calls() == 0


def test_s27_host_load_refuses_then_clears(stall, monkeypatch):
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = supervisor.Supervisor(stall.s, load_check=True)
    sp.start()
    monkeypatch.setattr(os, "getloadavg", lambda: (99.0, 99.0, 99.0))
    sp.tick()
    assert status(stall, bid) == "queued"
    assert [r["reason"] for r in stall.s.open_refusals(bid)] == ["host-load"]
    assert_nothing_ran(stall, bid)
    monkeypatch.setattr(os, "getloadavg", lambda: (0.1, 0.1, 0.1))
    monkeypatch.setattr(supervisor, "free_memory", lambda: 8 * 1024 ** 3)
    for _ in range(3):
        stall.s.event("gate.timeout", {"gate": "x"})
    sp.tick()
    assert status(stall, bid) == "queued"
    assert "gate timeouts" in stall.s.open_refusals(bid)[0]["detail"]
    assert_nothing_ran(stall, bid)
    with stall.s.tx() as c:  # the timeouts age out of the 30-minute window (time compressed)
        c.execute("UPDATE events SET mono = mono - ? WHERE kind = 'gate.timeout'",
                  (sp.b.load_timeout_window + 1,))
    assert until(sp, lambda: status(stall, bid) == "shipped")
    assert stall.s.open_refusals(bid) == []


def test_lost_run_closes_its_running_attempt(stall):
    stall.script([{"sleep": 30}, GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: stall.s.one("SELECT 1 FROM attempts WHERE status = 'running'"), timeout=30)
    run = stall.s.one("SELECT * FROM runs WHERE batch_id = ? AND status != 'ended'", bid)
    sp.kill_run(run)
    sp.end_and_requeue(run, "lost")
    a = stall.s.one("SELECT * FROM attempts WHERE run_id = ?", run["id"])
    assert a["status"] == "done" and a["outcome"] == "interrupted"


def test_driver_error_counts_toward_the_crash_loop(stall, monkeypatch):
    # Codex M1 review #3: a driver that records its own `error` must still consume a restart,
    # and the attempt it left running is closed.
    monkeypatch.setenv("ILK2_FAULTS", "phase-dispatch=raise")
    monkeypatch.setenv("ILK2_BOUNDS", json.dumps({"crash_backoff_first": 1, "crash_backoff_second": 1}))
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked", timeout=90)
    assert reasons(stall, bid) == ["error"] * 9
    assert [r["reason"] for r in stall.s.open_human_rows(bid)] == ["crash-loop"]
    assert stall.s.all("SELECT id FROM attempts WHERE status = 'running'") == []


def test_supervised_kill_with_survivors_fences_instead_of_requeueing(stall, monkeypatch):
    # Codex M1 review #5, supervisor side.
    stall.script([{"sleep": 30}, GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: stall.s.one("SELECT 1 FROM attempts WHERE status = 'running'"), timeout=30)
    run = stall.s.one("SELECT * FROM runs WHERE batch_id = ? AND status != 'ended'", bid)
    real = procs.terminate_group
    monkeypatch.setattr(procs, "terminate_group",
                        lambda *a, **kw: {**real(*a, **kw), "survivors": [(999999, "stuck")]})
    sp.kill_run(run)
    sp.end_and_requeue(run, "stalled (dispatch)")
    assert status(stall, bid) == "blocked"
    root = stall.s.get("batches", bid)["execution_root"]
    assert lease.get(stall.s, root)["holder"] == "driver"
    sp.tick()
    assert stall.s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status != 'ended'", bid) is None


def test_attempt_left_running_by_an_ended_run_is_closed(stall):
    # The M1 store kept one such row from before lost runs closed their attempts.
    bid = stall.submit(one_step())
    rid = stall.new_run(bid)
    step = stall.s.one("SELECT id FROM steps WHERE batch_id = ?", bid)
    stall.s.insert("attempts", {"id": "a-orphan", "run_id": rid, "step_id": step["id"], "n": 1,
                                "status": "running", "created_at": clock.wall()})
    died = clock.wall() - 3600  # the run died an hour before anyone closed its attempt
    stall.s.update("runs", rid, {"status": "ended", "terminal_reason": "lost", "ended_at": died},
                   actor="test", reason="x")
    sp = sup(stall)
    sp.tick()
    a = stall.s.get("attempts", "a-orphan")
    assert a["status"] == "done" and a["outcome"] == "interrupted"
    # ended when its run ended, not when it was found (ilk2-f0: `ilk2 report` counted 291 min)
    assert a["ended_at"] == died


def test_notifications_are_opt_in(stall):
    # Q12 (Chad, Oct 13): a project that configured no channel gets the durable row and its
    # events, and no page of any kind.
    stall.script([BAD], [BAD], [BAD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: status(stall, bid) == "blocked")
    assert len(stall.s.open_human_rows(bid)) == 1
    assert [e["kind"] for e in stall.s.events_since(0, bid)].count("batch.blocked") == 1
    b = bounds.load()
    with stall.s.tx() as c:
        c.execute("UPDATE needs_human SET created_at = created_at - ?", (b.consumer_offhost + 1,))
    assert notify.due(stall.s, b) == 0
    from ilk2 import paths
    assert not (paths.logs() / "notify.log").exists()


def test_crash_backoff_holds_the_batch_then_clears_itself(stall, monkeypatch):
    monkeypatch.setenv("ILK2_FAULTS", "phase-recover")
    monkeypatch.setenv("ILK2_BOUNDS", json.dumps({"crash_backoff_first": 600, "crash_backoff_second": 600}))
    stall.script([GOOD])
    bid = stall.submit(one_step())
    sp = sup(stall)
    assert until(sp, lambda: [r["reason"] for r in stall.s.open_refusals(bid)] == ["crash-backoff"], timeout=30)
    n = len(reasons(stall, bid))
    for _ in range(4):
        sp.tick()
        time.sleep(sp.b.tick)
    assert len(reasons(stall, bid)) == n and status(stall, bid) == "active"  # held, not respawned
    assert stall.s.open_human_rows(bid) == []  # nobody paged for a backoff
    with stall.s.tx() as c:  # the backoff period passes
        c.execute("UPDATE refusals SET opened_at = opened_at - 601 WHERE reason = 'crash-backoff'")
    monkeypatch.setenv("ILK2_FAULTS", "")
    sp2 = supervisor.Supervisor(stall.s, load_check=False)
    assert until(sp2, lambda: status(stall, bid) == "shipped", timeout=60)
