import sqlite3
import threading

import pytest

from ilk2 import store as st


@pytest.fixture
def s(tmp_path):
    s = st.Store(tmp_path / "state.db")
    s.insert("projects", {"id": "p", "repo_root": "/r", "default_branch": "main",
                          "config": "{}", "created_at": 0})
    s.insert("batches", {"id": "b", "project_id": "p", "base_commit": "abc", "branch": "ilk2/b",
                         "execution_root": "/r", "consumer_ref": "issue-7", "status": "queued",
                         "snapshot": "{}", "originals": "{}", "created_at": 0})
    yield s
    s.close()


def test_transition_is_cas_and_journalled(s):
    v = s.transition("batches", "b", "active", actor="supervisor", reason="admitted",
                     expect_from="queued")
    assert v == 2
    with pytest.raises(st.Conflict):
        s.transition("batches", "b", "active", actor="x", reason="y", expect_from="queued")
    with pytest.raises(st.Conflict):
        s.transition("batches", "b", "parked", actor="x", reason="y", expect_version=1)
    rows = s.all("SELECT from_state, to_state, actor, prev_version FROM transitions")
    assert [tuple(r) for r in rows] == [("queued", "active", "supervisor", 1)]


def test_unknown_state_rejected(s):
    with pytest.raises(st.StoreError):
        s.transition("batches", "b", "stuck", actor="x", reason="y")


def test_needs_human_one_open_row_per_reason(s):
    a = s.open_needs_human("attempts", "3 attempts red", "ilk2 resume b", batch_id="b")
    b = s.open_needs_human("attempts", "again", "ilk2 resume b", batch_id="b")
    assert a == b
    row = s.get("needs_human", a)
    assert row["consumer_ref"] == "issue-7" and row["project_id"] == "p"
    s.ack(a, "chad")
    with pytest.raises(st.Conflict):
        s.ack(a, "chad")
    c = s.open_needs_human("attempts", "new entry", "ilk2 resume b", batch_id="b")
    assert c != a


def test_needs_human_reason_enum(s):
    with pytest.raises(st.StoreError):
        s.open_needs_human("stuck", "", "", batch_id="b")


def test_events_carry_consumer_ref_and_cursor(s):
    c1 = s.event("phase.start", {"phase": "prepare"}, batch_id="b")
    c2 = s.event("phase.end", {"phase": "prepare"}, batch_id="b")
    evs = s.events_since(c1 - 1)
    assert [e["cursor"] for e in evs] == [c1, c2]
    assert all(e["consumer_ref"] == "issue-7" for e in evs)
    assert evs[0]["payload"] == {"phase": "prepare"}


def test_refusal_dedup_and_clear(s):
    a = s.refuse("b", "host-load", "load 12 > 10 cores", "load falls", "supervisor")
    assert s.refuse("b", "host-load", "still", "load falls", "supervisor") == a
    assert len(s.open_refusals("b")) == 1
    assert s.clear_refusal("b", "host-load")
    assert s.open_refusals("b") == []
    with pytest.raises(st.StoreError):
        s.refuse("b", "x", "", "", "nobody")


def test_store_busy_after_retries(tmp_path):
    path = tmp_path / "state.db"
    st.Store(path).close()
    locker = sqlite3.connect(path, isolation_level=None)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        s = st.Store.__new__(st.Store)
        s.path, s.retries, s._in_tx = str(path), 1, False
        s.conn = sqlite3.connect(path, timeout=0.05, isolation_level=None)
        s.conn.execute("PRAGMA busy_timeout = 50")
        with pytest.raises(st.StoreBusy):
            with s.tx():
                pass
    finally:
        locker.execute("ROLLBACK")


def test_concurrent_cas_exactly_one_wins(tmp_path):
    path = tmp_path / "state.db"
    s0 = st.Store(path)
    s0.insert("projects", {"id": "p", "repo_root": "/r", "default_branch": "main",
                           "config": "{}", "created_at": 0})
    s0.insert("batches", {"id": "b", "project_id": "p", "base_commit": "abc", "branch": "x",
                          "execution_root": "/r", "status": "queued", "snapshot": "{}",
                          "originals": "{}", "created_at": 0})
    wins = []

    def go(name):
        s = st.Store(path)
        try:
            s.transition("batches", "b", "active", actor=name, reason="race", expect_version=1)
            wins.append(name)
        except st.Conflict:
            pass
        finally:
            s.close()

    ts = [threading.Thread(target=go, args=(f"t{i}",)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(wins) == 1
