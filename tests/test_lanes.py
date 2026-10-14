"""Lanes: independent plans of one batch run at once (docs/02 §2.4.2a; docs/06 "Parallel plans",
Chad's go Oct 14). Real supervisor, real drivers as subprocesses, the stub worker scripted per
worktree."""

from __future__ import annotations

import json
import os

import pytest
from conftest import one_step  # noqa: F401  (fixture module import)
from test_stall import sup, until

from ilk2 import batches

SHELL_OK = {"name": "ok", "adapter": "shell", "declares_tests": False, "command": ["true"]}


@pytest.fixture
def lanes(ilk, monkeypatch):
    monkeypatch.setenv("ILK2_TIME_SCALE", "60")
    monkeypatch.setenv("ILK2_NOTIFY", "log")
    monkeypatch.delenv("ILK2_FAULTS", raising=False)
    ilk.make_repo()
    ilk.project(max_lanes=2, concurrency_limit=2)
    return ilk


def plan(slug, writes, depends_on=()):
    return {"slug": slug, "depends_on": list(depends_on),
            "steps": [{"title": f"{slug} work", "instructions": f"write {writes}", "gates": [SHELL_OK]}]}


def by_cwd(ilk, scripts: dict):
    """scripts: worktree-path part -> list of action lists (one per call)."""
    ilk.script_path.write_text(json.dumps(
        {"by_cwd": {k: [{"actions": a} for a in v] for k, v in scripts.items()}}))


def runs_of(ilk, bid):
    return ilk.s.all("SELECT * FROM runs WHERE batch_id = ? ORDER BY created_at", bid)


def dump(ilk, bid) -> str:
    """Why a lanes test did not reach its state: runs, plans, last events, driver errors."""
    from ilk2 import paths
    out = [f"batch {dict(ilk.s.get('batches', bid))['status']}"]
    out += [f"run {r['id']} plan={r['plan_id']} {r['status']} {r['terminal_reason']}" for r in runs_of(ilk, bid)]
    out += [f"plan {p['slug']} {p['status']} {p['blocked_reason']}" for p in
            ilk.s.all("SELECT * FROM plans WHERE batch_id = ?", bid)]
    out += [f"ev {e['kind']} {json.dumps(e['payload'])[:160]}" for e in ilk.s.events_since(0, bid)[-25:]]
    for r in runs_of(ilk, bid):
        for name in ("driver-error.txt", "driver.out"):
            f = paths.run_dir(r["id"]) / name
            if f.exists() and f.read_text().strip():
                out.append(f"{r['id']} {name}: {f.read_text()[-1500:]}")
    out += [f"refusal {x['reason']} {x['detail']}" for x in ilk.s.all(
        "SELECT * FROM refusals WHERE batch_id = ? AND cleared_at IS NULL", bid)]
    return "\n".join(out)


def overlapped(a, b) -> bool:
    end_a, end_b = a["ended_at"] or 1e18, b["ended_at"] or 1e18
    return a["created_at"] < end_b and b["created_at"] < end_a


def test_two_independent_plans_run_in_parallel_lanes_and_both_merge(lanes):
    ilk = lanes
    bid = ilk.submit({"plans": [plan("p1", "one.txt"), plan("p2", "two.txt")]})
    # each worker sleeps first, so both lanes are cut from the same head (the stub is fast)
    by_cwd(ilk, {f"{bid}.p1": [[{"sleep": 4}, {"write": "one.txt", "text": "1"}]],
                 f"{bid}.p2": [[{"sleep": 4}, {"write": "two.txt", "text": "2"}]]})
    sp = sup(ilk, host_budget=4)
    assert until(sp, lambda: ilk.s.get("batches", bid)["status"] == "shipped", timeout=120), dump(ilk, bid)
    b = ilk.s.get("batches", bid)
    assert ilk.git(b["execution_root"], "show", f"{b['branch']}:one.txt") == "1"
    assert ilk.git(b["execution_root"], "show", f"{b['branch']}:two.txt") == "2"
    lane_runs = [r for r in runs_of(ilk, bid) if r["plan_id"]]
    assert {r["plan_id"] for r in lane_runs} == {f"{bid}.p1", f"{bid}.p2"}
    first = [r for r in lane_runs if r["plan_id"] == f"{bid}.p1"][0]
    second = [r for r in lane_runs if r["plan_id"] == f"{bid}.p2"][0]
    assert overlapped(first, second)                       # the two plans ran at the same time
    kinds = [e["kind"] for e in ilk.s.events_since(0, bid)]
    assert kinds.count("lane.merged") == 2 and "repair.added" not in kinds
    assert not os.path.exists(f"{b['execution_root']}.p1")  # lane worktrees go after the merge


def test_a_merge_conflict_becomes_a_repair_in_the_batch_root(lanes):
    ilk = lanes
    bid = ilk.submit({"plans": [plan("p1", "calc.py"), plan("p2", "calc.py")]})
    b = ilk.s.get("batches", bid)
    by_cwd(ilk, {f"{bid}.p1": [[{"sleep": 4}, {"write": "shared.txt", "text": "from p1\n"}]],
                 f"{bid}.p2": [[{"sleep": 4}, {"write": "shared.txt", "text": "from p2\n"}]],
                 b["execution_root"]: [[{"write": "shared.txt", "text": "from p1\nfrom p2\n"}]]})
    sp = sup(ilk, host_budget=4)
    assert until(sp, lambda: ilk.s.get("batches", bid)["status"] in ("shipped", "blocked"), timeout=150), dump(ilk, bid)
    assert ilk.s.get("batches", bid)["status"] == "shipped"
    rp = ilk.s.all("SELECT * FROM plans WHERE batch_id = ? AND slug LIKE 'merge-%'", bid)
    assert len(rp) == 1 and rp[0]["status"] == "shipped"
    ev = [e for e in ilk.s.events_since(0, bid) if e["kind"] == "repair.added"]
    assert ev and ev[0]["payload"]["repair_kind"] == "merge-conflict"
    assert ilk.git(b["execution_root"], "show", f"{b['branch']}:shared.txt") == "from p1\nfrom p2"


def test_a_dependent_plan_starts_from_the_merged_head(lanes):
    ilk = lanes
    bid = ilk.submit({"plans": [plan("p1", "one.txt"), plan("p2", "two.txt", depends_on=["p1"])]})
    by_cwd(ilk, {f"{bid}.p1": [[{"write": "one.txt", "text": "1"}]],
                 f"{bid}.p2": [[{"write": "two.txt", "text": "2"}]]})
    sp = sup(ilk, host_budget=4)
    assert until(sp, lambda: ilk.s.get("batches", bid)["status"] == "shipped", timeout=120), dump(ilk, bid)
    lane_runs = {r["plan_id"]: r for r in runs_of(ilk, bid) if r["plan_id"]}
    p1, p2 = lane_runs[f"{bid}.p1"], lane_runs[f"{bid}.p2"]
    assert p2["created_at"] >= p1["ended_at"]              # waited for p1 to merge
    starts = [e for e in ilk.s.events_since(0, bid) if e["kind"] == "lane.started"]
    assert [e["payload"]["plan"] for e in starts] == ["p1", "p2"]


def test_a_blocked_lane_does_not_stop_the_other_and_the_batch_ends_blocked(lanes):
    ilk = lanes
    bid = ilk.submit({"plans": [plan("p1", "nothing"), plan("p2", "two.txt")]})
    by_cwd(ilk, {f"{bid}.p1": [[]],                          # never changes anything: red x5
                 f"{bid}.p2": [[{"write": "two.txt", "text": "2"}]]})
    sp = sup(ilk, host_budget=4)
    assert until(sp, lambda: ilk.s.get("batches", bid)["status"] == "blocked", timeout=180), dump(ilk, bid)
    plans = {p["slug"]: p["status"] for p in ilk.s.all("SELECT * FROM plans WHERE batch_id = ?", bid)}
    assert plans == {"p1": "blocked", "p2": "shipped"}
    assert ilk.s.get("batches", bid)["blocked_reason"]
    assert ilk.s.one("SELECT 1 FROM needs_human WHERE batch_id = ? AND state = 'open'", bid)


def test_one_lane_keeps_the_serial_path(ilk, monkeypatch):
    monkeypatch.setenv("ILK2_TIME_SCALE", "60")
    ilk.make_repo()
    ilk.project()                                           # max_lanes = 1
    assert batches.project_config(ilk.s, "proj").max_lanes == 1
    bid = ilk.submit({"plans": [plan("p1", "one.txt"), plan("p2", "two.txt")]})
    ilk.script([{"write": "one.txt", "text": "1"}], [{"write": "two.txt", "text": "2"}])
    assert ilk.drive_inprocess(bid) == "shipped"
    assert all(r["plan_id"] is None for r in runs_of(ilk, bid))
    assert not [e for e in ilk.s.events_since(0, bid) if e["kind"].startswith("lane.")]


def test_a_lane_whose_driver_dies_resumes_in_its_worktree_with_its_work(lanes):
    # Stall extension for lanes: a lost lane run is requeued like any run; the new run reuses
    # the lane worktree, keeps the step's work, and the plan still merges.
    import signal
    import time
    ilk = lanes
    bid = ilk.submit({"plans": [plan("p1", "one.txt"), plan("p2", "two.txt")]})
    by_cwd(ilk, {f"{bid}.p1": [[{"write": "one.txt", "text": "1"}, {"hang": True}], []],
                 f"{bid}.p2": [[{"sleep": 2}, {"write": "two.txt", "text": "2"}]]})
    sp = sup(ilk, host_budget=4)
    lane1 = os.path.join(os.path.dirname(ilk.s.get("batches", bid)["execution_root"]), f"{bid}.p1")
    assert until(sp, lambda: os.path.exists(os.path.join(lane1, "one.txt")), timeout=60, keep=True), dump(ilk, bid)
    run = ilk.s.one("SELECT * FROM runs WHERE plan_id = ? AND status = 'running'", f"{bid}.p1")
    os.killpg(run["pgid"], signal.SIGKILL)                # the driver and its worker die
    time.sleep(0.5)
    assert until(sp, lambda: ilk.s.get("batches", bid)["status"] == "shipped", timeout=150), dump(ilk, bid)
    b = ilk.s.get("batches", bid)
    assert ilk.git(b["execution_root"], "show", f"{b['branch']}:one.txt") == "1"
    assert any(e["kind"] == "worktree.kept" for e in ilk.s.events_since(0, bid))
    assert len([r for r in runs_of(ilk, bid) if r["plan_id"] == f"{bid}.p1"]) >= 2


def test_two_repairs_never_run_at_once(lanes):
    # gh-resolve P6, Oct 14: verification added three repairs; two were admitted together, and
    # the second waited on the first's merge lock until the supervisor called its spawn stalled,
    # three times, putting the batch in crash backoff. Root plans share the batch root: one at a time.
    from ilk2 import batches as b_
    from ilk2.runs import ready_plans
    ilk = lanes
    bid = ilk.submit({"plans": [plan("p1", "one.txt")]})
    v = ilk.s.get("batches", bid)["version"]
    b_.amend(ilk.s, bid, [{"op": "add_plan", "slug": f"repair-{i}", "repair_of_step_id": f"{bid}.p1.0",
                           "steps": [{"title": "r", "instructions": "r", "gates": [SHELL_OK]}]}
                          for i in (1, 2)], expect_version=v, actor="driver", reason="test")
    ready = ready_plans(ilk.s, bid)
    assert {p["slug"] for p in ready} == {"repair-1", "repair-2"}
    rid = ilk.new_run(bid)
    ilk.s.update("runs", rid, {"plan_id": ready[0]["id"]}, actor="test", reason="bind")
    assert ready_plans(ilk.s, bid) == []                   # the other waits for that run


def test_a_union_merge_path_needs_no_repair(ilk, monkeypatch):
    # gh-resolve P6, Oct 14: 2 of 3 merge repairs were one design doc every plan appends to.
    monkeypatch.setenv("ILK2_TIME_SCALE", "60")
    monkeypatch.setenv("ILK2_NOTIFY", "log")
    monkeypatch.delenv("ILK2_FAULTS", raising=False)
    ilk.make_repo()
    ilk.project(max_lanes=2, concurrency_limit=2, union_merge=["notes/*.md"])
    bid = ilk.submit({"plans": [plan("p1", "notes/log.md"), plan("p2", "notes/log.md")]})
    by_cwd(ilk, {f"{bid}.p1": [[{"sleep": 4}, {"write": "notes/log.md", "text": "from p1\n"}]],
                 f"{bid}.p2": [[{"sleep": 4}, {"write": "notes/log.md", "text": "from p2\n"}]]})
    sp = sup(ilk, host_budget=4)
    assert until(sp, lambda: ilk.s.get("batches", bid)["status"] in ("shipped", "blocked"), timeout=120), dump(ilk, bid)
    assert ilk.s.get("batches", bid)["status"] == "shipped", dump(ilk, bid)
    b = ilk.s.get("batches", bid)
    text = ilk.git(b["execution_root"], "show", f"{b['branch']}:notes/log.md")
    assert set(text.splitlines()) == {"from p1", "from p2"}
    assert not ilk.s.one("SELECT 1 FROM plans WHERE batch_id = ? AND slug LIKE 'merge-%'", bid)
