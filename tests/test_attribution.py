"""Attribution suite A1-A10 (docs/02 §2.5.7, §2.11.4), with the stub worker and in-process drivers.

The consumer has two independent tests: test_calc (always green) and test_b, which reads b.py.
Steps that only declare test_calc can break test_b unseen; a later step that declares test_b
then finds an inherited failure, and attribution must find the step that broke it.
"""

from __future__ import annotations

import json

import pytest
from conftest import gate

from ilk2 import bounds, gates

@pytest.fixture(autouse=True)
def real_wall_budget(monkeypatch):
    # Time is compressed 600x in tests; real pytest probes are not. Keep the attribution
    # wall budget at its unscaled 20 minutes so the probe-count budget is what A6 tests.
    monkeypatch.setenv("ILK2_BOUNDS", json.dumps({"attribution_wall": 20 * 60}))


CONFTEST = "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
FILES = {"calc.py": "def add(a, b):\n    return a + b\n",
         "b.py": "B = 1\n",
         "tests/conftest.py": CONFTEST,
         "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
         "tests/test_b.py": "from b import B\ndef test_b():\n    assert B == 1\n"}
CALC = gate("tests/test_calc.py")
BOTH = gate("tests/test_calc.py", "tests/test_b.py", name="both")
BREAK = {"write": "b.py", "text": "B = 2\n"}
FIX = {"write": "b.py", "text": "B = 1\n"}


def note(n):
    return {"write": f"notes{n}.txt", "text": f"{n}\n"}


def step(title, g=CALC, **kw):
    return {"title": title, "instructions": title, "gates": [g], "must_pass": [], **kw}


def setup(ilk, files=None, **cfg):
    ilk.make_repo(files or FILES)
    ilk.project(**cfg)


def attributions(ilk):
    out = []
    for gr in ilk.s.all("SELECT details FROM gate_results ORDER BY id"):
        out += list(json.loads(gr["details"]).get("attribution", {}).values())
    return out


def repair_plans(ilk, bid):
    return ilk.s.all("SELECT * FROM plans WHERE batch_id = ? AND repair_of_step_id IS NOT NULL", bid)


def order_of(ilk, kind, **match):
    """Event cursors of `kind` whose payload matches."""
    return [e["cursor"] for e in ilk.s.events_since(0)
            if e["kind"] == kind and all(e["payload"].get(k) == v for k, v in match.items())]


# -- A7: base clean, step 1 red, step 2 red -> step 1 ----------------------------------------

def test_a7_walk_backwards_finds_the_first_failing_child(ilk):
    setup(ilk)
    ilk.script([BREAK], [note(1)], [note(2)], [FIX])
    bid = ilk.submit({"plans": [{"slug": "p", "steps": [
        step("break b"), step("unrelated"), step("declares test_b", BOTH)]}]})
    assert ilk.drive_inprocess(bid) == "shipped"
    att = [a for a in attributions(ilk) if a["result"] != "current"]
    assert [a["result"] for a in att] == ["attributed"]
    assert att[0]["step"] == f"{bid}.p.0"  # the child of the last clean commit, not p.1
    rp = repair_plans(ilk, bid)
    assert len(rp) == 1 and rp[0]["repair_of_step_id"] == f"{bid}.p.0" and rp[0]["status"] == "shipped"
    # The repair's gate runs only the repaired test, not the whole gate (Oct 14)
    rg = json.loads(ilk.s.one("SELECT gates FROM steps WHERE plan_id = ?", rp[0]["id"])["gates"])
    ids = [c for c in rg[0]["command"] if "::" in c]
    assert len(ids) == 1 and ids[0].startswith("tests/test_b.py::")
    assert "tests/test_calc.py" not in rg[0]["command"]
    s2 = ilk.s.one("SELECT * FROM steps WHERE id = ?", f"{bid}.p.2")
    assert s2["status"] == "accepted" and s2["attempt_count"] == 1  # pass-with-attributed
    gr = ilk.s.one("SELECT outcome FROM gate_results WHERE gate = 'both' ORDER BY id LIMIT 1")
    assert gr["outcome"] == "pass-with-attributed"
    # A3: the probes ran elsewhere; the step's own uncommitted work reached its commit
    b = ilk.s.get("batches", bid)
    assert ilk.git(b["execution_root"], "show", f"{s2['accepted_commit']}:notes2.txt") == "2"
    assert ilk.git(b["execution_root"], "show", f"{b['branch']}:b.py") == "B = 1"  # repaired
    assert ilk.stub_calls() == 4


# -- A1: the breaking plan has shipped; the batch pauses behind the repair --------------------

def test_a1_repair_plan_for_a_shipped_plan_pauses_the_batch(ilk):
    setup(ilk)
    ilk.script([BREAK], [note(1)], [note(2)], [FIX], [note(3)])
    bid = ilk.submit({"plans": [
        {"slug": "p", "steps": [step("break b")]},
        {"slug": "q", "steps": [step("q0"), step("q1 declares test_b", BOTH), step("q2")]}]})
    assert ilk.drive_inprocess(bid) == "shipped"
    rp = repair_plans(ilk, bid)
    assert len(rp) == 1 and rp[0]["repair_of_step_id"] == f"{bid}.p.0"
    p = ilk.s.one("SELECT * FROM plans WHERE id = ?", f"{bid}.p")
    assert p["status"] == "shipped"
    reopened = ilk.s.all("SELECT * FROM transitions WHERE entity = 'plans' AND entity_id = ? "
                         "AND from_state = 'shipped'", f"{bid}.p")
    assert reopened == []  # P was never reopened
    repair_step = f"{rp[0]['id']}.0"
    accepted_repair = order_of(ilk, "step.accepted", step=repair_step)
    q2_start = order_of(ilk, "phase.start", phase="prepare", step=f"{bid}.q.2")
    assert accepted_repair and q2_start and accepted_repair[0] < q2_start[0]


# -- A9: clean, failing (1), fixed (2), failing again (4) -> step 4 ---------------------------

def test_a9_most_recent_edge(ilk):
    setup(ilk)
    ilk.script([BREAK], [FIX], [note(1)], [BREAK], [note(2)], [FIX])
    bid = ilk.submit({"plans": [{"slug": "p", "steps": [
        step("s1 break"), step("s2 fix"), step("s3"), step("s4 break"), step("s5 declares", BOTH)]}]})
    assert ilk.drive_inprocess(bid) == "shipped"
    att = [a for a in attributions(ilk) if a["result"] == "attributed"]
    assert len(att) == 1 and att[0]["step"] == f"{bid}.p.3", att


# -- A2: the specification changed between the edge commits -----------------------------------

def test_a2_spec_changed_is_unattributable(ilk):
    setup(ilk)
    ilk.script([BREAK, {"append": "tests/conftest.py", "text": "# changed\n"}],
               [{"write": "tests/conftest.py", "text": CONFTEST}],
               [note(1)], [FIX])
    bid = ilk.submit({"plans": [{"slug": "p", "steps": [
        step("break b, change conftest"), step("restore conftest"), step("declares", BOTH)]}]})
    assert ilk.drive_inprocess(bid) == "shipped"
    results = [a["result"] for a in attributions(ilk)]
    assert results == ["spec-changed"], results
    assert repair_plans(ilk, bid) == []
    outs = [a["outcome"] for a in ilk.s.all("SELECT outcome FROM attempts WHERE step_id = ? "
                                            "ORDER BY n", f"{bid}.p.2")]
    assert outs == ["red", "accepted"]  # judged on all new failures, repaired by the step itself


# -- A4: flaky at the boundary ----------------------------------------------------------------

def test_a4_flaky_boundary_is_not_blamed(ilk, monkeypatch):
    setup(ilk)
    ilk.script([BREAK], [note(1)], [FIX])
    bid = ilk.submit({"plans": [{"slug": "p", "steps": [step("break b"), step("declares", BOTH)]}]})
    base = ilk.s.get("batches", bid)["base_commit"]
    real = gates.measure
    seen = {"base_probes": 0, "failing": None}

    def flaky(g, where, env, workdir, spec=None):
        r = real(g, where, env, workdir, spec)
        if g.name.endswith("-probe") and r.failures:
            seen["failing"] = r.failures
        if g.name.endswith("-probe") and where.name == base[:12]:
            seen["base_probes"] += 1
            if seen["base_probes"] == 2 and seen["failing"]:  # the first confirmation disagrees
                r.failures, r.outcome = dict(seen["failing"]), "fail"
        return r
    monkeypatch.setattr(gates, "measure", flaky)
    assert ilk.drive_inprocess(bid) == "shipped"
    assert [a["result"] for a in attributions(ilk)] == ["flaky"]
    assert repair_plans(ilk, bid) == []


# -- A5: dependencies come from each commit's own lockfile ------------------------------------

def test_a5_each_diagnostic_worktree_installs_its_own_lockfile(ilk):
    files = dict(FILES)
    files["uv.lock"] = "v1"
    files[".gitignore"] = "installed.txt\n"
    files["tests/test_b.py"] = "def test_b():\n    assert open('installed.txt').read() == 'v1'\n"
    setup(ilk, files, setup={"command": ["python", "-c",
                                         "import shutil; shutil.copy('uv.lock', 'installed.txt')"]})
    ilk.script([{"write": "uv.lock", "text": "v2"}], [note(1)], [{"write": "uv.lock", "text": "v1"}])
    bid = ilk.submit({"plans": [{"slug": "p", "steps": [
        step("bump the lockfile"), step("declares", BOTH)]}]})
    assert ilk.drive_inprocess(bid) == "shipped"
    att = [a for a in attributions(ilk) if a["result"] == "attributed"]
    assert len(att) == 1 and att[0]["step"] == f"{bid}.p.0", attributions(ilk)


# -- A6: probe budget, then reuse of cached probes ---------------------------------------------

def test_a6_budget_then_cached_probes_reused(ilk, monkeypatch):
    monkeypatch.setattr(bounds, "PROBES_PER_FAILURE", 3)
    setup(ilk)
    ilk.script([BREAK], [note(1)], [note(2)], [note(3)], [note(4)], [note(5)], [FIX])
    bid = ilk.submit({"plans": [{"slug": "p", "steps": [
        step("break"), step("s2"), step("s3"), step("s4"), step("declares", BOTH)]}]})
    assert ilk.drive_inprocess(bid) == "shipped"
    att = attributions(ilk)
    assert [a["result"] for a in att] == ["budget", "budget"], att
    assert all(a["probes"] <= 3 for a in att)
    probes = [e["payload"] for e in ilk.s.events_since(0) if e["kind"] == "attribution.probe"]
    first, second = probes[:3], probes[3:]
    assert not any(p["cached"] for p in first)
    assert [p["cached"] for p in second[:3]] == [True, True, True]  # reused, not repeated
    walk = []
    for p in probes:
        walk.append(p)
        if not p["present"]:
            break  # what follows confirms the edge, deliberately uncached
    walked = [p["commit"] for p in walk if not p["cached"]]
    assert len(walked) == len(set(walked))  # across both attempts, no walk probe repeated
    assert repair_plans(ilk, bid) == []


# -- A8: different assertions at one line have different fingerprints -------------------------

def test_a8_different_assertions_same_line_are_different_failures(tmp_path):
    from test_gates import gate as g_
    from test_gates import m, project
    root = project(tmp_path, {"tests/test_a.py": "def test_x():\n    assert 1 == 2, 'expected 1, got 2'\n"})
    base = m(g_("tests/test_a.py"), root, tmp_path).failure_map()
    (root / "tests/test_a.py").write_text("def test_x():\n    assert 3 == 4, 'expected 3, got 4'\n")
    r = m(g_("tests/test_a.py"), root, tmp_path)
    assert r.failure_map()["tests/test_a.py::test_x"] != base["tests/test_a.py::test_x"]
    assert gates.judge(r, base).new_failures == ["tests/test_a.py::test_x"]


# -- A10: one repair for the same failure and edge; an independent plan waits ------------------

def test_a10_deduplicated_repair_and_barrier(ilk):
    setup(ilk)
    both2 = gate("tests/test_b.py", name="b-only")
    ilk.script([BREAK], [note(1)], [FIX], [note(2)])
    bid = ilk.submit({"plans": [
        {"slug": "p", "steps": [step("break b"),
                                {"title": "two gates", "instructions": "x", "gates": [BOTH, both2],
                                 "must_pass": []}]},
        {"slug": "r", "steps": [step("independent")]}]})
    assert ilk.drive_inprocess(bid) == "shipped"
    rp = repair_plans(ilk, bid)
    assert len(rp) == 1, [dict(x) for x in rp]
    assert len(order_of(ilk, "repair.joined")) == 1
    repaired = order_of(ilk, "step.accepted", step=f"{rp[0]['id']}.0")
    r_start = order_of(ilk, "phase.start", phase="prepare", step=f"{bid}.r.0")
    assert repaired and r_start and repaired[0] < r_start[0]


def test_a10_blocked_repair_blocks_the_batch(ilk):
    # The barrier, not plan order: while the repair cannot ship, the independent plan never
    # starts, and the batch stops with a named reason.
    setup(ilk)
    ilk.script([BREAK], [note(1)], [note(2)], [note(3)], [note(4)], [note(9)])
    bid = ilk.submit({"plans": [
        {"slug": "p", "steps": [step("break b"), step("declares", BOTH)]},
        {"slug": "r", "steps": [step("independent")]}]})
    assert ilk.drive_inprocess(bid) == "blocked"
    rp = repair_plans(ilk, bid)
    assert len(rp) == 1 and rp[0]["status"] == "blocked"
    r0 = ilk.s.one("SELECT * FROM steps WHERE id = ?", f"{bid}.r.0")
    assert r0["status"] == "pending" and r0["attempt_count"] == 0
    assert ilk.stub_calls() == 7  # break, declares, 5 repair attempts; never the independent step
    assert [r["reason"] for r in ilk.s.open_human_rows(bid)] == ["attempts"]


# -- A10 by edge: several tests one step broke get one repair ----------------------------------

def test_a10_failures_attributed_to_one_edge_share_one_repair(ilk):
    # gh-resolve 18a, Oct 18: one step broke six tests in one pins file, and the gate run that
    # found them added six repair plans, each briefed on one test. One repair names all six.
    files = {**FILES, "tests/test_b2.py": "from b import B\ndef test_b2():\n    assert B == 1\n"}
    setup(ilk, files)
    both_b = gate("tests/test_calc.py", "tests/test_b.py", "tests/test_b2.py", name="both")
    ilk.script([BREAK], [note(1)], [note(2)], [FIX], [note(3)])
    bid = ilk.submit({"plans": [
        {"slug": "p", "steps": [step("break b")]},
        {"slug": "q", "steps": [step("q0"), step("q1 declares the b tests", both_b), step("q2")]}]})
    assert ilk.drive_inprocess(bid) == "shipped"
    rp = repair_plans(ilk, bid)
    assert len(rp) == 1, [dict(x) for x in rp]
    st = ilk.s.one("SELECT * FROM steps WHERE plan_id = ?", rp[0]["id"])
    assert sorted(json.loads(st["must_pass"])) == ["tests/test_b.py::test_b", "tests/test_b2.py::test_b2"]
    assert ilk.s.one("SELECT COUNT(*) n FROM repairs WHERE batch_id = ?", bid)["n"] == 2
