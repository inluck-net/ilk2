"""The P5 consumer contract, ilk2's half (docs/06 "P5 addendum" R1, R2, R4, R5)."""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from conftest import one_step

from ilk2 import batches, lease, supervisor
from ilk2.store import Conflict

GOOD = {"write": "calc.py", "text": "def add(a, b):\n    return a + b\n"}


def sup(ilk, **kw):
    s = supervisor.Supervisor(ilk.s, load_check=False, **kw)
    s.start()
    return s


def until(sp, pred, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        sp.tick()
        if pred():
            return True
        time.sleep(sp.b.tick)
    return False


@pytest.fixture
def p5(ilk, monkeypatch):
    monkeypatch.setenv("ILK2_TIME_SCALE", "60")
    ilk.make_repo()
    ilk.project()
    return ilk


def test_r1_recover_refused_while_a_process_is_in_the_root(p5):
    bid = p5.submit(one_step())
    root = p5.s.get("batches", bid)["execution_root"]
    head = p5.git(root, "rev-parse", "HEAD")
    g = lease.driver_release(p5.s, root, head)
    g = lease.consumer_acquire(p5.s, root, g, head, "gh", head, -1)
    lease.expire_due(p5.s)
    g = lease.get(p5.s, root)["generation"]
    stray = subprocess.Popen(["sleep", "30"], cwd=root)
    try:
        with pytest.raises(Conflict, match="inside"):
            lease.recover(p5.s, root, g, head)
    finally:
        stray.kill()
        stray.wait()
    assert lease.recover(p5.s, root, g, head) == g + 1


def test_r2_lease_length_capped_by_the_project(p5):
    from dataclasses import asdict
    cfg = batches.project_config(p5.s, "proj")
    cfg.max_lease_s = 3600
    p5.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    bid = p5.submit(one_step())
    root = p5.s.get("batches", bid)["execution_root"]
    head = p5.git(root, "rev-parse", "HEAD")
    g = lease.driver_release(p5.s, root, head)
    with pytest.raises(Conflict, match="max_lease_s"):
        lease.consumer_acquire(p5.s, root, g, head, "gh", head, 3601)
    g = lease.consumer_acquire(p5.s, root, g, head, "gh", head, 3600)
    with pytest.raises(Conflict, match="max_lease_s"):
        lease.consumer_renew(p5.s, root, g, 7200)


def test_r4_a_batch_waits_while_a_consumer_holds_its_root(p5):
    # gh-resolve reuses resolver-<issue> roots: the second batch on a root must wait for the
    # consumer's release, behind a refusal row, without counting as a crash.
    first = p5.submit(one_step())
    root = p5.s.get("batches", first)["execution_root"]
    p5.s.transition("batches", first, "cancelled", actor="test", reason="x")
    head = p5.git(root, "rev-parse", "HEAD")
    g = lease.driver_release(p5.s, root, head)
    g = lease.consumer_acquire(p5.s, root, g, head, "gh-resolve", head, 600)
    p5.script([GOOD])
    second = p5.submit(one_step(), root=__import__("pathlib").Path(root))
    sp = sup(p5)
    assert until(sp, lambda: [r["reason"] for r in p5.s.open_refusals(second)] == ["root-lease"], 30)
    n_runs = p5.s.one("SELECT COUNT(*) n FROM runs WHERE batch_id = ?", second)["n"]
    for _ in range(4):
        sp.tick()
    assert p5.s.one("SELECT COUNT(*) n FROM runs WHERE batch_id = ?", second)["n"] == n_runs
    assert p5.s.get("batches", second)["status"] in ("queued", "active")
    lease.consumer_release(p5.s, root, g, head)
    assert until(sp, lambda: p5.s.get("batches", second)["status"] == "shipped", 60)
    st = p5.s.one("SELECT restart_count FROM steps WHERE batch_id = ?", second)
    assert st["restart_count"] == 0  # waiting was never a crash


def test_r4_two_batches_on_one_root_never_run_together(p5):
    p5.script([{"sleep": 3}, GOOD], [GOOD])
    first = p5.submit(one_step())
    root = p5.s.get("batches", first)["execution_root"]
    second = p5.submit(one_step(), root=__import__("pathlib").Path(root))
    sp = sup(p5)
    seen_both = False

    def done():
        nonlocal seen_both
        live = {r["batch_id"] for r in p5.s.all("SELECT batch_id FROM runs WHERE status IN "
                                                 "('starting', 'running')")}
        seen_both = seen_both or {first, second} <= live
        return all(p5.s.get("batches", b)["status"] == "shipped" for b in (first, second))
    assert until(sp, done, 90)
    assert not seen_both


def test_r5_reserved_runs_are_left_for_their_project(p5, tmp_path):
    from dataclasses import asdict
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(p5.repo), str(other)], check=True)
    cfg = batches.project_config(p5.s, "proj")
    batches.project_add(p5.s, "resolver", other, batches.ProjectConfig(**{**asdict(cfg), "reserve": 1}))
    p5.script([{"sleep": 5}, GOOD])
    a = p5.submit(one_step())
    b = p5.submit(one_step())
    sp = sup(p5, host_budget=2)
    sp.tick()
    live = {r["batch_id"] for r in p5.s.all("SELECT batch_id FROM runs WHERE status IN ('starting', 'running')")}
    # proj may use only one of the two runs: the other is the resolver's, idle or not
    assert len(live & {a, b}) == 1
    refused = [r["reason"] for bid in (a, b) for r in p5.s.open_refusals(bid)]
    assert "host-budget" in refused or "project-limit" in refused


# -- gh-resolve's adapter review (gh-resolve-e1, Oct 13, against ceb788c) ------------------------

def cli(*args):
    import io, contextlib
    from ilk2 import cli as c
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = c.main(list(args))
    out = buf.getvalue().strip()
    return code, (json.loads(out) if out.startswith("{") else out)


def spec_file(tmp_path):
    f = tmp_path / "b.json"
    f.write_text(json.dumps(one_step()))
    return str(f)


def test_b1_submit_onto_a_named_branch_and_rework_on_it(p5, tmp_path):
    p5.script([GOOD], [{"write": "notes.txt", "text": "rework"}])
    code, out = cli("submit", spec_file(tmp_path), "--project", "proj", "--branch", "resolver-42", "--json")
    assert code == 0
    b = p5.s.get("batches", out["batch"])
    assert b["branch"] == "resolver-42"
    sp = sup(p5)
    assert until(sp, lambda: p5.s.get("batches", b["id"])["status"] == "shipped")
    first_head = p5.git(str(p5.repo), "rev-parse", "resolver-42")
    # rework: a second batch on the same branch builds on its head
    f2 = tmp_path / "b2.json"
    f2.write_text(json.dumps({"plans": [{"slug": "rw", "steps": [{"title": "rework", "instructions": "x",
                  "gates": [{"name": "unit", "adapter": "pytest", "command": ["python", "-m", "pytest", "-q",
                  "-p", "no:cacheprovider", "tests/test_calc.py"]}], "must_pass": []}]}]}))
    code, out = cli("submit", str(f2), "--project", "proj", "--branch", "resolver-42", "--json")
    assert code == 0
    b2 = p5.s.get("batches", out["batch"])
    assert b2["base_commit"] == first_head
    assert until(sp, lambda: p5.s.get("batches", b2["id"])["status"] == "shipped")
    assert p5.git(str(p5.repo), "rev-parse", "resolver-42^") == first_head


def test_b2_api_version(p5):
    assert cli("--api-version", "1", "status", "--json")[0] == 0
    code, out = cli("--api-version", "2", "status", "--json")
    assert code == 2 and out["error"] == "unsupported-api-version"


def test_b3_one_rejection_shape(p5, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"plans": [{"slug": "p", "steps": [{"title": "t", "instructions": "x", "gates": []}]}]}))
    code, out = cli("submit", str(bad), "--project", "proj", "--json")
    assert code == 2 and out["error"] == "rejected" and out["problems"]


def test_b4_status_says_draining(p5):
    with p5.s.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES ('draining', '1') ON CONFLICT(key) DO UPDATE SET value = '1'")
    assert cli("status", "--json")[1]["draining"] is True


def test_b5_recover_from_a_consumer_whose_process_is_gone(p5):
    bid = p5.submit(one_step())
    root = p5.s.get("batches", bid)["execution_root"]
    head = p5.git(root, "rev-parse", "HEAD")
    g = lease.driver_release(p5.s, root, head)
    g = lease.consumer_acquire(p5.s, root, g, head, "gh-resolve", head, 600)
    assert lease.recover(p5.s, root, g, head) == g + 1  # reap's flock is free: its process is gone
    assert lease.get(p5.s, root)["holder"] == "released"


def test_b6_shadow_fetches_a_base_it_does_not_have(p5, tmp_path):
    import subprocess as sp_
    shadow = tmp_path / "sh"
    sp_.run(["git", "clone", "-q", str(p5.repo), str(shadow)], check=True)
    sp_.run(["git", "-C", str(shadow), "remote", "set-url", "--push", "origin", "DISABLED"], check=True)
    (p5.repo / "later.txt").write_text("x")
    p5.git(str(p5.repo), "add", "later.txt")
    p5.git(str(p5.repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "later")
    new_base = p5.git(str(p5.repo), "rev-parse", "HEAD")
    from dataclasses import asdict
    cfg = batches.project_config(p5.s, "proj")
    batches.project_add(p5.s, "sh", shadow, batches.ProjectConfig(**{**asdict(cfg), "shadow_of": str(p5.repo)}))
    bid = batches.submit(p5.s, "sh", __import__("ilk2").plans.from_dict(one_step()), base=new_base)
    assert p5.s.get("batches", bid)["base_commit"] == new_base


def test_b8_cancel_is_idempotent(p5, tmp_path):
    bid = p5.submit(one_step())
    assert cli("cancel", bid, "--reason", "stand-down")[0] == 0
    assert cli("cancel", bid, "--reason", "stand-down")[0] == 0  # already cancelled: done
