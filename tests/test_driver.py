import json

from conftest import gate, one_step

from ilk2 import lease

GOOD = {"write": "calc.py", "text": "def add(a, b):\n    return a + b\n"}
BAD = {"write": "calc.py", "text": "def add(a, b):\n    return a - b\n"}


def commits_with_trailer(ilk, root, branch):
    log = ilk.git(root, "log", "--format=%B%x1e", branch)
    return [r for r in log.split("\x1e") if "ilk2-attempt:" in r]


def test_happy_path_ships_with_trailer_commit(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    assert b["status"] == "shipped"
    step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    assert step["status"] == "accepted" and step["attempt_count"] == 1
    root = b["execution_root"]
    assert ilk.git(root, "rev-parse", b["branch"]) == step["accepted_commit"]
    assert len(commits_with_trailer(ilk, root, b["branch"])) == 1
    assert "return a + b" in ilk.git(root, "show", f"{b['branch']}:calc.py")  # not vacuous
    assert ilk.stub_calls() == 1
    assert ilk.git(root, "status", "--porcelain") == ""
    assert lease.get(ilk.s, root)["holder"] == "released"
    assert ilk.s.one("SELECT COUNT(*) n FROM base_measurements WHERE batch_id = ?", bid)["n"] == 1


def test_red_then_resume_fixes(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([BAD], [GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT * FROM attempts ORDER BY n")
    assert [a["outcome"] for a in atts] == ["red", "accepted"]
    assert atts[0]["session_id"] == atts[1]["session_id"]  # Q6: the 2nd attempt resumes
    assert "test_add" in atts[0]["evidence"]


def test_three_reds_block_step_with_one_human_row(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([BAD], [BAD], [BAD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "blocked"
    assert ilk.s.get("batches", bid)["status"] == "blocked"
    rows = ilk.s.open_human_rows(bid)
    assert [r["reason"] for r in rows] == ["attempts"]
    assert ilk.stub_calls() == 3


def test_s7_dependency_blocked_independent_ships(ilk):
    ilk.make_repo({"a.py": "", "tests/test_a.py": "def test_a():\n    assert open('a.py').read() == 'A'\n",
                   "tests/test_c.py": "def test_c():\n    assert open('c.py').read() == 'C'\n"})
    ilk.project()
    ilk.script([{"write": "a.py", "text": "wrong"}], [{"write": "a.py", "text": "wrong"}],
               [{"write": "a.py", "text": "wrong"}], [{"write": "c.py", "text": "C"}])
    spec = {"plans": [
        {"slug": "a", "steps": [{"instructions": "A", "gates": [gate("tests/test_a.py")]}]},
        {"slug": "b", "depends_on": ["a"], "steps": [{"instructions": "B", "gates": [gate("tests/test_a.py")]}]},
        {"slug": "c", "steps": [{"instructions": "C", "gates": [gate("tests/test_c.py")]}]}]}
    bid = ilk.submit(spec)
    assert ilk.drive_inprocess(bid) == "blocked"
    st = {p["slug"]: (p["status"], p["blocked_reason"]) for p in
          ilk.s.all("SELECT * FROM plans WHERE batch_id = ?", bid)}
    assert st["a"][0] == "blocked" and st["c"][0] == "shipped"
    assert st["b"] == ("blocked", "dependency: a")
    assert len(ilk.s.open_human_rows(bid)) == 1


def test_s8_expected_red_then_green(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([{"write": "tests/test_new.py", "text": "from calc import add\n"
                 "def test_new():\n    assert add(1, 1) == 2\n"}], [GOOD])
    spec = {"plans": [{"slug": "p", "steps": [
        {"title": "pin", "instructions": "red-first", "gates": [gate("tests/test_new.py", name="new")],
         "expect_red": ["tests/test_new.py::test_new"]},
        {"title": "fix", "instructions": "make it pass", "gates": [gate("tests/test_new.py", name="new")]}]}]}
    bid = ilk.submit(spec)
    assert ilk.drive_inprocess(bid) == "shipped"


def test_s10_base_unmeasured_blocks_before_dispatch(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bad_gate = gate("tests/test_calc.py", "--no-such-option-xyz")
    bid = ilk.submit(one_step(gates_=[bad_gate]))
    reason = ilk.drive_inprocess(bid)
    assert reason == "blocked (base-unmeasured)"
    assert ilk.stub_calls() == 0
    assert [r["reason"] for r in ilk.s.open_human_rows(bid)] == ["base-unmeasured"]


def test_scope_violation_is_red(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD | {}, ], )
    ilk.script([{"write": "other.py", "text": "x"}, GOOD], [GOOD], [GOOD])
    bid = ilk.submit(one_step(scope=["calc.py"]))
    assert ilk.drive_inprocess(bid) == "blocked"
    att = ilk.s.all("SELECT * FROM attempts ORDER BY n")[0]
    assert "outside the step's declared scope" in att["evidence"]


def test_s5_api_errors_not_counted(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([{"api_error": 429}], [{"api_error": 503}], [GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    assert step["attempt_count"] == 1
    outs = [a["outcome"] for a in ilk.s.all("SELECT outcome FROM attempts ORDER BY created_at")]
    assert outs == ["api", "api", "accepted"]


def test_s19_possibly_inherited_marked(ilk):
    ilk.make_repo({"calc.py": "def add(a, b):\n    return a + b\n",
                   "b.py": "B = 1\n",
                   "tests/conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n",
                   "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
                   "tests/test_b.py": "from b import B\ndef test_b():\n    assert B == 1\n"})
    ilk.project()
    ilk.script([{"write": "b.py", "text": "B = 2\n"}],             # step 0 breaks test_b
               [{"write": "notes.txt", "text": "x"}], [{"write": "notes.txt", "text": "y"}],
               [{"write": "notes.txt", "text": "z"}])
    spec = {"plans": [{"slug": "p", "steps": [
        {"title": "s0", "instructions": "x", "gates": [gate("tests/test_calc.py")]},
        {"title": "s1", "instructions": "y", "gates": [gate("tests/test_calc.py", "tests/test_b.py", name="both")]}]}]}
    bid = ilk.submit(spec)
    assert ilk.drive_inprocess(bid) == "blocked"
    gr = ilk.s.one("SELECT * FROM gate_results WHERE gate = 'both' ORDER BY id LIMIT 1")
    details = json.loads(gr["details"])
    assert details["possibly_inherited"] == ["tests/test_b.py::test_b"]
    ev = ilk.s.all("SELECT evidence FROM attempts WHERE evidence IS NOT NULL")[0]["evidence"]
    assert "possibly-inherited" in ev


def test_s14_crash_points_recover_without_duplicates(ilk):
    ilk.make_repo()
    ilk.project()
    for point in ("after-intent", "after-commit", "before-finalize"):
        ilk.script([GOOD], [GOOD])
        bid = ilk.submit(one_step())
        rc = ilk.drive_subprocess(bid, faults=point)
        assert rc != 0
        step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
        assert step["status"] == "accepting", point
        ilk.s.update("runs", ilk.s.one("SELECT id FROM runs WHERE batch_id = ? AND status != 'ended'",
                                       bid)["id"], {"status": "ended", "terminal_reason": "lost"},
                     actor="test", reason="crash")
        assert ilk.drive_inprocess(bid) == "shipped", point
        b = ilk.s.get("batches", bid)
        assert len(commits_with_trailer(ilk, b["execution_root"], b["branch"])) == 1, point
        assert ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)["accepted_commit"]


def test_amend_pending_only_version_checked_and_journalled(ilk):
    import pytest
    from ilk2 import batches
    from ilk2.store import Conflict
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    v = ilk.s.get("batches", bid)["version"]
    sid = ilk.s.one("SELECT id FROM steps WHERE batch_id = ?", bid)["id"]
    v2 = batches.amend(ilk.s, bid, [{"op": "replace", "step": sid, "instructions": "new words"},
                                    {"op": "add", "plan": "p", "title": "more", "instructions": "x",
                                     "gates": [gate("tests/test_calc.py")]}], expect_version=v)
    assert ilk.s.get("steps", sid)["instructions"] == "new words"
    assert ilk.s.one("SELECT COUNT(*) n FROM steps WHERE batch_id = ?", bid)["n"] == 2
    with pytest.raises(Conflict):  # stale version
        batches.amend(ilk.s, bid, [{"op": "replace", "step": sid, "title": "t"}], expect_version=v)
    ilk.s.update("steps", sid, {"attempt_count": 1}, actor="test", reason="started")
    with pytest.raises(batches.SubmitRejected):  # started steps cannot change
        batches.amend(ilk.s, bid, [{"op": "replace", "step": sid, "title": "t"}], expect_version=v2)
    assert ilk.s.one("SELECT COUNT(*) n FROM amendments WHERE batch_id = ?", bid)["n"] == 1
    assert "plans" in ilk.s.get("batches", bid)["snapshot"]  # the submitted snapshot is untouched


def test_changed_paths_keeps_first_entry_intact(ilk):
    from ilk2 import gitops
    root = ilk.make_repo()
    (root / "calc.py").write_text("x = 1\n")          # tracked, modified: " M calc.py"
    (root / "new.py").write_text("y = 2\n")           # untracked: "?? new.py"
    assert gitops.changed_paths(root) == ["calc.py", "new.py"]
