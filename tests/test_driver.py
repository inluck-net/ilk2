import json

import pytest
from pathlib import Path

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
    assert ilk.stub_calls() == 5  # 3 attempts, then the escalation round of 2 (self-resolution)
    assert "5 attempts red" in rows[0]["detail"]


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


def test_s10_base_unmeasured_rejected_at_submit(ilk):
    # Self-resolution: without a setup hook the submit preflight catches it synchronously.
    import pytest
    from ilk2 import batches
    ilk.make_repo()
    ilk.project()
    with pytest.raises(batches.SubmitRejected, match="usage-error"):
        ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "--no-such-option-xyz")]))
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0


def test_s10_base_unmeasured_blocks_before_dispatch(ilk):
    # With a setup hook the preflight is skipped, so activation still catches it (§2.11.4 S10).
    ilk.make_repo()
    ilk.project(setup={"command": ["python", "-c", "pass"]})
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
    # §2.11.4: 429/5xx for 10 minutes. Backoff 1, 2, 4, 8 min (time-scaled) spans 15.
    ilk.script([{"api_error": 429}], [{"api_error": 503}], [{"api_error": 429}],
               [{"api_error": 502}], [GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    assert step["attempt_count"] == 1
    outs = [a["outcome"] for a in ilk.s.all("SELECT outcome FROM attempts ORDER BY created_at")]
    assert outs == ["api"] * 4 + ["accepted"]
    from ilk2 import bounds
    scale = bounds.load().api_backoff_first / 60
    waited = sum(e["payload"]["seconds"] for e in ilk.s.events_since(0) if e["kind"] == "api.backoff")
    assert waited / scale >= 10 * 60  # in unscaled terms, more than 10 minutes of errors
    assert ilk.s.open_refusals(bid) == [] and ilk.s.open_human_rows(bid) == []


def test_s19_possibly_inherited_marked(ilk, monkeypatch):
    # The marking is what an unattributed inherited failure gets. Attribution would otherwise
    # trace this one to step 0 within its budget (2 s at the test time scale) on an idle host,
    # and pass the step; under load it ran out, which is why this test passed only on a busy
    # machine (Oct 14). Budget 0: attribution ends at `budget`, deterministically.
    monkeypatch.setenv("ILK2_BOUNDS", json.dumps({"attribution_wall": 0}))
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
    # the brief of step 1's second attempt (the 3rd worker call) carries the probe's verdict
    prompt = ilk.stub_prompts()[2]
    assert "tests/test_b.py::test_b [possibly-inherited" in prompt


def test_s14_crash_points_recover_without_duplicates(ilk):
    ilk.make_repo()
    ilk.project()
    # §2.4.1: after-intent recovers by undo (a new attempt); after-commit and before-finalize
    # recover by finalizing the commit already made (no new worker call, same SHA).
    for point, commits_before, undo in (("after-intent", 0, True), ("after-commit", 1, False),
                                        ("before-finalize", 1, False)):
        ilk.script([GOOD], [GOOD])
        bid = ilk.submit(one_step())
        rc = ilk.drive_subprocess(bid, faults=point)
        assert rc != 0
        step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
        assert step["status"] == "accepting", point
        b = ilk.s.get("batches", bid)
        root, branch = b["execution_root"], b["branch"]
        assert len(commits_with_trailer(ilk, root, branch)) == commits_before, point
        crashed_head = ilk.git(root, "rev-parse", branch)
        assert ilk.stub_calls() == 1, point
        ilk.s.update("runs", ilk.s.one("SELECT id FROM runs WHERE batch_id = ? AND status != 'ended'",
                                       bid)["id"], {"status": "ended", "terminal_reason": "lost"},
                     actor="test", reason="crash")
        assert ilk.drive_inprocess(bid) == "shipped", point
        step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
        assert len(commits_with_trailer(ilk, root, branch)) == 1, point
        assert step["accepted_commit"] == ilk.git(root, "rev-parse", branch), point
        assert ilk.stub_calls() == (2 if undo else 1), point
        if not undo:
            assert step["accepted_commit"] == crashed_head, point  # the commit was kept
        recovery = [e["payload"]["action"] for e in ilk.s.events_since(0)
                    if e["kind"] == "recovery" and e["batch_id"] == bid]
        assert recovery == (["undo"] if undo else ["finalize"]), point


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


def test_export_v1_round_trips_through_the_v1_importer(ilk, tmp_path):
    from ilk2 import batches, plans
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    files = batches.export_v1(ilk.s, bid, tmp_path / "out")
    p = plans.parse_v1_subplan(files[0].read_text())
    assert p.slug == "p" and len(p.steps) == 1
    assert p.steps[0].gates[0].command[:3] == ["python", "-m", "pytest"]
    assert "implement add" in p.steps[0].instructions


def test_declared_test_path_still_absent_after_step_is_red(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD], [GOOD], [GOOD])  # never creates tests/test_new.py
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_new.py")]))
    assert ilk.drive_inprocess(bid) == "blocked"
    ev = ilk.s.all("SELECT evidence FROM attempts WHERE evidence IS NOT NULL")[0]["evidence"]
    assert "tests/test_new.py" in ev


def test_worker_that_did_not_finish_is_never_accepted(ilk):
    # Codex M1 review #2: the worker writes the right code, then exits without a result.
    # The gates are green, but a crashed attempt is red evidence, not an acceptance.
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD, {"exit": 3}], [GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT * FROM attempts ORDER BY n")
    assert [a["outcome"] for a in atts] == ["red", "accepted"]
    assert "crashed" in atts[0]["evidence"]


def test_attempt_that_changes_nothing_is_red(ilk):
    # Codex M1 review #2: gates already green and no edit must not become an empty commit.
    ilk.make_repo({"calc.py": "def add(a, b):\n    return a + b\n",
                   "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
                   "tests/conftest.py": "import sys, os\n"
                   "sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"})
    ilk.project()
    ilk.script([], [], [])
    bid = ilk.submit(one_step(must_pass=[]))
    assert ilk.drive_inprocess(bid) == "blocked"
    atts = ilk.s.all("SELECT * FROM attempts ORDER BY n")
    assert [a["outcome"] for a in atts] == ["red"] * 5  # 3, then the escalation round
    assert "no change" in atts[0]["evidence"]
    b = ilk.s.get("batches", bid)
    assert commits_with_trailer(ilk, b["execution_root"], b["branch"]) == []


def test_unkillable_worker_fences_the_batch_and_keeps_the_lease(ilk, monkeypatch):
    # Codex M1 review #5: survivors of SIGKILL must stop the batch with the root still held.
    import json as _json
    from ilk2 import procs
    monkeypatch.setenv("ILK2_BOUNDS", _json.dumps({"attempt_cap": 2}))
    real = procs.terminate_group

    def fake(*a, **kw):
        res = real(*a, **kw)
        res["survivors"] = [(999999, "stuck")]
        return res
    monkeypatch.setattr(procs, "terminate_group", fake)
    ilk.make_repo()
    ilk.project()
    ilk.script([{"hang": True}])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "blocked (unkillable)"
    b = ilk.s.get("batches", bid)
    assert b["status"] == "blocked"
    assert [r["reason"] for r in ilk.s.open_human_rows(bid)] == ["unkillable"]
    assert lease.get(ilk.s, b["execution_root"])["holder"] == "driver"


def test_shell_gate_runs_green_at_base(ilk):
    # Codex M1 review #7 / docs/02 §2.5.1: a shell check (exit code only) is a real gate.
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    lint = {"name": "lint", "adapter": "shell", "declares_tests": False,
            "command": ["python", "-c", "import ast; ast.parse(open('calc.py').read())"]}
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py"), lint]))
    assert ilk.drive_inprocess(bid) == "shipped"


def test_shell_gate_is_judged_absolutely_red_at_base_is_allowed(ilk):
    # V1 local checks are acceptance checks, often red at the base by design ("no xfail left").
    # A shell gate must exit 0 after its step; an unchanged red is never a pass (B5).
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD], [GOOD, {"write": "done.txt", "text": "y"}])
    check = {"name": "accept", "adapter": "shell", "declares_tests": False,
             "command": ["python", "-c", "import os; raise SystemExit(0 if os.path.exists('done.txt') else 1)"]}
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py"), check]))
    assert ilk.drive_inprocess(bid) == "shipped"
    outs = [a["outcome"] for a in ilk.s.all("SELECT outcome FROM attempts ORDER BY n")]
    assert outs == ["red", "accepted"]  # attempt 1 left the check red: not accepted


def test_commits_carry_the_v1_plan_trailer(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    assert "[plan:p#step-0]" in ilk.git(b["execution_root"], "log", "-1", "--format=%B", b["branch"])


def test_s21_timeout_class_rerun_costs_no_attempt(ilk):
    marker = ilk.tmp / "timed-out-once"
    flaky = (f"import os, pytest\nfrom calc import add\nM = {str(marker)!r}\n"
             "def test_flaky():\n"
             "    try:\n        add(1, 1)\n    except NotImplementedError:\n        return\n"
             "    if not os.path.exists(M):\n"
             "        open(M, 'w').close()\n"
             "        pytest.fail('Timeout >15.0s')\n")
    ilk.make_repo({"calc.py": "def add(a, b):\n    raise NotImplementedError\n",
                   "tests/conftest.py": "import sys, os\n"
                   "sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n",
                   "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n",
                   "tests/test_flaky.py": flaky})
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_flaky.py")]))
    assert ilk.drive_inprocess(bid) == "shipped"
    assert marker.exists()  # the timeout-class failure happened after the step, and was re-run
    assert ilk.stub_calls() == 1
    step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    assert step["attempt_count"] == 1


def test_deny_paths_make_an_attempt_red_whatever_the_scope(ilk):
    # docs/06 R8: a project's denied paths (gh-resolve's write-target denylist, e.g.
    # convex/migrations/) hold for every batch, whoever submits it and whatever its scope.
    ilk.make_repo()
    ilk.project(deny_paths=["migrations/"])
    ilk.script([GOOD, {"write": "migrations/0001.py", "text": "x = 1\n"}],
               [{"delete": "migrations/0001.py"}])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT * FROM attempts ORDER BY n")
    assert [a["outcome"] for a in atts] == ["red", "accepted"]
    assert "migrations/0001.py" in atts[0]["evidence"] and "denied" in atts[0]["evidence"]
    b = ilk.s.get("batches", bid)
    assert "migrations" not in ilk.git(b["execution_root"], "ls-tree", "-r", "--name-only", b["branch"])


def test_deny_paths_reject_a_scope_that_names_them(ilk):
    import pytest
    from ilk2 import batches
    ilk.make_repo()
    ilk.project(deny_paths=["migrations/"])
    with pytest.raises(batches.SubmitRejected, match="denied"):
        ilk.submit(one_step(scope=["migrations/0002.py"]))


def test_escalation_round_gets_all_the_evidence_in_a_fresh_session(ilk):
    # docs/06 "Self-resolution": after 3 reds, attempt 4 starts a fresh session briefed with
    # every earlier attempt's evidence, before any human is asked.
    ilk.make_repo()
    ilk.project()
    ilk.script([BAD], [BAD], [BAD], [GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT * FROM attempts ORDER BY n")
    assert [a["outcome"] for a in atts] == ["red", "red", "red", "accepted"]
    assert atts[3]["session_id"] not in {a["session_id"] for a in atts[:3]}  # fresh
    prompt = ilk.stub_prompts()[3]
    assert all(f"Attempt {k}" in prompt for k in (1, 2, 3)) and "test_add" in prompt
    assert ilk.s.open_human_rows(bid) == []


def test_deny_paths_allow_a_broader_scope_that_contains_them(ilk):
    # kira-shadow, Oct 13: scope ['convex/'] with deny ['convex/migrations/'] was rejected.
    # Edits under the denied path are still red at attempt time (the test above).
    ilk.make_repo()
    ilk.project(deny_paths=["tests/fixtures/"])
    ilk.submit(one_step(scope=["tests/", "calc.py"]))


def test_consumer_git_hooks_do_not_break_ilk2_worktrees(ilk):
    # kira-cloudflare, Oct 13: lefthook's post-checkout hook made `git worktree add` exit
    # non-zero, the run errored, and the next run measured a worktree whose setup never ran.
    ilk.make_repo()
    hook = ilk.repo / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\necho 'consumer hook ran' >&2\nexit 1\n")
    hook.chmod(0o755)
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"


def test_interrupted_setup_is_rerun_in_a_reused_diag_worktree(ilk):
    ilk.make_repo()
    ilk.project(setup={"command": ["python", "-c", "open('setup-ran', 'a').write('x')"]})
    bid = ilk.submit(one_step())
    from ilk2.driver import Driver
    d = Driver(ilk.s, ilk.new_run(bid))
    base = ilk.s.get("batches", bid)["base_commit"]
    p = d.diag_worktree(base)
    assert (p / "setup-ran").read_text() == "x"
    (p / ".ilk2-setup-done").unlink()  # as if the run died during setup
    d.diag_worktree(base)
    assert (p / "setup-ran").read_text() == "xx"  # setup ran again
    d.diag_worktree(base)
    assert (p / "setup-ran").read_text() == "xx"  # and not a third time once it completed


def test_deny_patterns_follow_gitignore_style_double_star():
    # gh-resolve's path_denylist ('**/schema.ts', '**/migrations/**', '**/wrangler.toml'):
    # fnmatch missed the top-level file, since '**/' needed at least one directory.
    from ilk2.batches import path_matches
    yes = [("schema.ts", "**/schema.ts"), ("convex/schema.ts", "**/schema.ts"),
           ("migrations/a.ts", "**/migrations/**"), ("convex/migrations/a.ts", "**/migrations/**"),
           ("wrangler.toml", "**/wrangler.toml"), ("a/b", "a/**/b"), ("a/x/y/b", "a/**/b")]
    no = [("convex/schemas.ts", "**/schema.ts"), ("convex/schema.tsx", "**/schema.ts"),
          ("migrationsx/a.ts", "**/migrations/**")]
    assert all(path_matches(p, [pat]) for p, pat in yes), [x for x in yes if not path_matches(*x[:1], [x[1]])]
    assert not any(path_matches(p, [pat]) for p, pat in no)


def test_submit_deny_paths_add_to_the_projects(ilk):
    # gh-resolve keeps its denylist as the one source: `submit --deny-path` per batch, additive.
    ilk.make_repo()
    ilk.project(deny_paths=["nothing/"])
    ilk.script([GOOD, {"write": "schema.ts", "text": "x = 1\n"}], [{"delete": "schema.ts"}])
    from ilk2 import batches, plans
    bid = batches.submit(ilk.s, "proj", plans.from_dict(one_step()), deny_paths=["**/schema.ts"])
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT * FROM attempts ORDER BY n")
    assert [a["outcome"] for a in atts] == ["red", "accepted"]
    assert "schema.ts" in atts[0]["evidence"] and "denied" in atts[0]["evidence"]


def test_deny_glob_star_stays_in_one_segment_like_gh_resolve():
    # gh-resolve admission._matches_any: '**/' spans directories, a single '*' does not.
    from ilk2.batches import path_matches
    assert path_matches(".env.local", [".env*"]) and not path_matches("app/.env.local", [".env*"])
    assert path_matches("app/.env.local", ["**/.env*"])
    assert not path_matches("src/key.pem", ["*.pem"]) and path_matches("src/key.pem", ["**/*.pem"])
    assert path_matches("packages/ui/components/x/_generated/api.ts", ["**/components/*/_generated/**"])
    assert not path_matches("components/x/y/_generated/api.ts", ["**/components/*/_generated/**"])
    assert path_matches("infra/aws/main.tf", ["infra/**"]) and path_matches("infra", ["infra/**"]) is False
    assert path_matches("convex/migrations/a.ts", ["convex/migrations/"])  # a bare dir prefix


def test_plain_scope_entries_cover_everything_below_them():
    from ilk2.batches import path_matches
    assert path_matches("tools/gh_resolve/x.py", ["tools/gh_resolve"])
    assert path_matches("tools/gh_resolve/x.py", ["tools/gh_resolve/"])
    assert path_matches("tools/gh_resolve", ["tools/gh_resolve"])
    assert not path_matches("tools/gh_resolver/x.py", ["tools/gh_resolve"])


def _verify_spec():
    v1_tool = {"name": "local-check-1", "adapter": "shell", "declares_tests": False,
               "command": ["python3", "/Users/x/.ilk/current/skills/ilk-loop/scripts/verification_record.py",
                           "--project", "."]}
    return {"plans": [
        {"slug": "work", "steps": [{"title": "add", "instructions": "implement add",
                                    "gates": [gate("tests/test_calc.py")],
                                    "must_pass": ["tests/test_calc.py::test_add"]}]},
        {"slug": "x-batch-verify", "verification": True, "depends_on": ["work"],
         "steps": [{"title": "Run the full suite, record the result", "instructions": "verify",
                    "gates": [v1_tool]}]}]}


def test_v1_verification_tooling_becomes_the_projects_verify_command(ilk):
    # gh-resolve's P5 master (Oct 13): its batch-verify plan gates only on V1 scripts that
    # write V1 records. Under ilk2 it runs the project's full suite at the batch head instead.
    import pytest
    from ilk2 import batches, plans
    ilk.make_repo()
    ilk.project()
    with pytest.raises(batches.SubmitRejected, match="verify_command"):
        ilk.submit(_verify_spec())
    from dataclasses import asdict
    cfg = batches.project_config(ilk.s, "proj")
    cfg.verify_command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    bid = ilk.submit(_verify_spec())
    vstep = ilk.s.one("SELECT * FROM steps WHERE batch_id = ? AND plan_id LIKE '%verify'", bid)
    cmds = [g["command"] for g in json.loads(vstep["gates"])]
    assert cmds == [cfg.verify_command] and not any(".ilk/" in " ".join(c) for c in cmds)


def test_a_verification_step_that_changes_nothing_is_accepted_without_a_commit(ilk):
    from dataclasses import asdict
    from ilk2 import batches
    ilk.make_repo()
    ilk.project()
    cfg = batches.project_config(ilk.s, "proj")
    cfg.verify_command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    ilk.script([GOOD], [])  # the work step edits; the verify step's worker changes nothing
    bid = ilk.submit(_verify_spec())
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    work = ilk.s.one("SELECT accepted_commit FROM steps WHERE plan_id = ?", f"{bid}.work")["accepted_commit"]
    ver = ilk.s.one("SELECT accepted_commit FROM steps WHERE plan_id LIKE '%verify'")["accepted_commit"]
    assert ver == work == ilk.git(b["execution_root"], "rev-parse", b["branch"])  # no extra commit


def test_v1_tooling_is_recognised_by_script_name_too():
    # ilk-skills is V1's own repo: its verify plans call the same scripts by a repo-relative
    # path (skills/ilk-loop/scripts/verification_record.py), not through ~/.ilk/.
    from ilk2 import batches, plans
    for cmd in (["python3", "skills/ilk-loop/scripts/verification_record.py", "--project", "."],
                ["python3", "skills/ilk-loop/scripts/verify_attribution.py", "--batch", "x"]):
        assert batches._v1_tooling(plans.Gate(name="g", adapter="shell", command=cmd))
    assert not batches._v1_tooling(plans.Gate(name="g", adapter="pytest",
                                              command=["python3", "-m", "pytest", "tests/test_a.py"]))


def test_next_step_of_a_plan_resumes_the_previous_steps_session(ilk):
    # Oct 13, gh-resolve P5 batch: 19 of 27.5 min went to exploring before the first edit, and
    # every step of a plan started cold. Step k now continues step k-1's session.
    ilk.make_repo()
    ilk.project()
    ilk.script([{"write": "a.txt", "text": "a"}], [{"write": "b.txt", "text": "b"}],
               [{"write": "c.txt", "text": "c"}])
    spec = {"plans": [
        {"slug": "p", "steps": [{"title": f"s{i}", "instructions": f"step {i}", "must_pass": [],
                                 "gates": [gate("tests/test_calc.py")]} for i in range(2)]},
        {"slug": "q", "steps": [{"title": "q0", "instructions": "other plan", "must_pass": [],
                                 "gates": [gate("tests/test_calc.py")]}]}]}
    bid = ilk.submit(spec)
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT a.session_id, st.plan_id FROM attempts a JOIN steps st ON st.id = a.step_id "
                     "ORDER BY a.created_at")
    assert atts[0]["session_id"] == atts[1]["session_id"]   # p.0 -> p.1 carried
    assert atts[2]["session_id"] != atts[1]["session_id"]   # a new plan starts fresh
    prompts = ilk.stub_prompts()
    assert "step 1" in prompts[1] and "previous step" in prompts[1].lower()
    assert "Preloaded context" not in prompts[1]           # the session already has it


def test_red_first_a_must_pass_test_that_passes_without_the_change_is_red(ilk):
    # gh-resolve-e1, Oct 13: folding V1's "Red-first pins" step must keep its guarantee, that a
    # new pin fails on the old code ("loop-written tests copy the code's assumptions").
    ilk.make_repo()
    ilk.project()
    weak = {"write": "tests/test_new.py", "text": "def test_new():\n    assert True\n"}
    strong = {"write": "tests/test_new.py", "text":
              "from calc import add\n\ndef test_new():\n    assert add(1, 1) == 2\n"}
    ilk.script([GOOD, weak], [strong])
    spec = one_step(gates_=[gate("tests/test_calc.py", "tests/test_new.py")],
                    must_pass=["tests/test_new.py::test_new"])
    bid = ilk.submit(spec)
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT * FROM attempts ORDER BY n")
    assert [a["outcome"] for a in atts] == ["red", "accepted"]
    assert "without your change" in atts[0]["evidence"] and "test_new" in atts[0]["evidence"]


def test_red_first_whole_file_needs_one_failing_test_not_all(ilk):
    # a sensitivity control may pass on the old code too (D-523's control did)
    ilk.make_repo()
    ilk.project()
    pins = {"write": "tests/test_pins.py", "text":
            "from calc import add\n\ndef test_pin():\n    assert add(2, 2) == 4\n\n"
            "def test_control():\n    assert callable(add)\n"}
    ilk.script([GOOD, pins])
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_pins.py")],
                              must_pass=["tests/test_pins.py"]))
    assert ilk.drive_inprocess(bid) == "shipped"
    assert [a["outcome"] for a in ilk.s.all("SELECT outcome FROM attempts")] == ["accepted"]


def test_advisory_scope_records_but_never_reds(ilk):
    # gh-resolve P5 batch, Oct 13: a step's V1 scope_paths left out tests/test_ledger.py, whose
    # schema test the gate needed changed; 3 attempts died on it. V1 never enforced scope_paths
    # (plan_lint and the ship report only), so V1-planned projects treat scope as advisory.
    ilk.make_repo()
    ilk.project(enforce_scope=False, deny_paths=["secrets/"])
    ilk.script([GOOD, {"write": "notes/extra.txt", "text": "x"}])
    bid = ilk.submit(one_step(scope=["calc.py"]))
    assert ilk.drive_inprocess(bid) == "shipped"
    assert [a["outcome"] for a in ilk.s.all("SELECT outcome FROM attempts")] == ["accepted"]
    ev = [e for e in ilk.s.events_since(0) if e["kind"] == "scope.advisory"]
    assert ev and "notes/extra.txt" in ev[0]["payload"]["outside"]


@pytest.mark.parametrize("last_outcome", ["interrupted", "red", "timeout", "crashed"])
def test_a_new_run_keeps_the_steps_own_leftover_work(ilk, last_outcome):
    # gh-resolve P5 batch, Oct 13: an upgrade drain started a new run, whose prepare reset the
    # worktree and discarded a red attempt's 554-line diff. The step's own work is kept.
    from ilk2 import clock
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    root = Path(ilk.s.get("batches", bid)["execution_root"])
    step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    rid = ilk.new_run(bid)
    ilk.s.insert("attempts", {"id": "a-old", "run_id": rid, "step_id": step["id"], "n": 1,
                              "status": "done", "outcome": last_outcome, "parent_commit": "x",
                              "created_at": clock.wall()})
    ilk.s.update("runs", rid, {"status": "ended", "terminal_reason": "drained"}, actor="test",
                 reason="drained")
    (root / "calc.py").write_text(GOOD["text"])           # the earlier attempt's work
    ilk.script([])                                          # this attempt changes nothing more
    assert ilk.drive_inprocess(bid) == "shipped"
    assert "still holds the previous attempt's changes" in ilk.stub_prompts()[0]
    assert any(e["kind"] == "worktree.kept" for e in ilk.s.events_since(0))


def test_a_new_run_resets_leftovers_that_are_not_the_steps_work(ilk):
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    root = Path(ilk.s.get("batches", bid)["execution_root"])
    (root / "stray.txt").write_text("x")                    # no attempt of this step left it
    ilk.script([GOOD])
    assert ilk.drive_inprocess(bid) == "shipped"
    assert not (root / "stray.txt").exists()


def test_amend_merges_the_ceremony_of_a_plan_not_started(ilk):
    # gh-resolve P5 batch, Oct 13: imported before the merge existed, 18 steps.
    import pytest
    from ilk2 import batches
    ilk.make_repo()
    ilk.project()
    spec = one_step()
    spec["plans"][0]["steps"][0]["title"] = "Red-first pins"
    bid = ilk.submit(spec)
    v = ilk.s.get("batches", bid)["version"]
    v = batches.amend(ilk.s, bid, [{"op": "add", "plan": "p", "title": "Implement", "instructions":
                                    "make it pass", "gates": [gate("tests/test_calc.py")]}],
                      expect_version=v)
    assert ilk.s.one("SELECT COUNT(*) n FROM steps WHERE batch_id = ?", bid)["n"] == 2
    v = batches.amend(ilk.s, bid, [{"op": "merge_ceremony", "plan": "p"}], expect_version=v)
    rows = ilk.s.all("SELECT * FROM steps WHERE batch_id = ? ORDER BY idx", bid)
    assert [r["title"] for r in rows] == ["Implement"] and "make it pass" in rows[0]["instructions"]
    sid = rows[0]["id"]
    ilk.s.update("steps", sid, {"attempt_count": 1}, actor="test", reason="started")
    with pytest.raises(batches.SubmitRejected):  # a started plan is never rewritten
        batches.amend(ilk.s, bid, [{"op": "merge_ceremony", "plan": "p"}], expect_version=v)
    ilk.s.update("steps", sid, {"attempt_count": 0}, actor="test", reason="reset")
    ilk.script([GOOD])
    assert ilk.drive_inprocess(bid) == "shipped"


def test_red_first_checks_pins_from_an_earlier_step_against_the_plan_start(ilk):
    # V1 merged plans: pins land in the plan's first step and "remove every xfail" (must_pass)
    # is a later step, so the pin file is not changed in the step that names it. Here the pin
    # passes on the plan's starting code, so it cannot detect anything: the later step is red.
    ilk.make_repo()
    ilk.project()
    weak = {"write": "tests/test_pins.py", "text": "def test_pin():\n    assert True\n"}
    note = {"write": "notes.txt", "text": "x"}
    ilk.script([GOOD, weak], [note], [note], [note], [note], [note])
    spec = {"plans": [{"slug": "p", "steps": [
        {"title": "implement and pin", "instructions": "x",
         "gates": [gate("tests/test_calc.py", "tests/test_pins.py")]},
        {"title": "remove every xfail", "instructions": "y",
         "gates": [gate("tests/test_calc.py", "tests/test_pins.py")],
         "must_pass": ["tests/test_pins.py::test_pin"]}]}]}
    bid = ilk.submit(spec)
    ilk.drive_inprocess(bid)
    second = ilk.s.all("SELECT a.outcome, a.evidence FROM attempts a JOIN steps st ON st.id = "
                       "a.step_id WHERE st.idx = 1 ORDER BY a.n")
    assert second and second[0]["outcome"] == "red"
    assert "without your change" in second[0]["evidence"]


def test_an_attempt_killed_at_its_bound_mid_edit_does_not_strand_the_step(ilk, monkeypatch):
    # gh-resolve-e1, Oct 13 (V1 incident, kira #8604/#8585): an attempt killed at the time bound
    # left uncommitted edits, and every relaunch was refused at entry, forever. In ilk2 the edits
    # stay in the worktree, the next attempt runs on them, and nothing checks the entry state.
    monkeypatch.setenv("ILK2_BOUNDS", json.dumps({"attempt_cap": 3, "term_grace": 1, "kill_grace": 1}))
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD, {"hang": True}], [])         # edit, then hang past the cap; then nothing new
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT outcome, evidence FROM attempts ORDER BY n")
    assert [a["outcome"] for a in atts] == ["red", "accepted"]
    assert "did not finish" in atts[0]["evidence"] and "kept in the worktree" in atts[0]["evidence"]


def test_a_green_verification_step_needs_no_worker(ilk):
    # gh-resolve P5 batch, Oct 14: the verify step's worker spent 18 min trying to run and record
    # the full suite that is the step's own gate. The gate now runs first.
    from dataclasses import asdict
    from ilk2 import batches
    ilk.make_repo()
    ilk.project()
    cfg = batches.project_config(ilk.s, "proj")
    cfg.verify_command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    ilk.script([GOOD])  # only the work step has a worker
    bid = ilk.submit(_verify_spec())
    assert ilk.drive_inprocess(bid) == "shipped"
    assert ilk.stub_calls() == 1
    v = ilk.s.one("SELECT a.outcome, a.reason FROM attempts a JOIN steps st ON st.id = a.step_id "
                  "WHERE st.plan_id LIKE '%verify'")
    assert v["outcome"] == "accepted" and "no worker" in v["reason"]


def test_a_red_verification_step_briefs_the_worker_on_the_gate_it_already_ran(ilk):
    # gh-resolve P5 batch, Oct 14: the worker followed V1's batch-verify instructions and went
    # looking for V1's verification record, which does not exist under ilk2.
    from dataclasses import asdict
    from ilk2 import batches
    ilk.make_repo()
    ilk.project()
    cfg = batches.project_config(ilk.s, "proj")
    cfg.verify_command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    spec = _verify_spec()
    spec["plans"] = spec["plans"][1:]                 # the verify plan alone, at a red head
    spec["plans"][0]["depends_on"] = []
    bad = {"write": "tests/test_extra.py", "text": "def test_x():\n    assert False\n"}
    ilk.script([{"write": "tests/test_extra.py", "text": "def test_x():\n    assert True\n"}])
    bid = ilk.submit(spec)
    b = ilk.s.get("batches", bid)
    from pathlib import Path as _P
    (_P(b["execution_root"]) / "tests" / "test_extra.py").write_text(bad["text"])
    ilk.git(b["execution_root"], "add", "-A")
    ilk.git(b["execution_root"], "commit", "-qm", "red head")
    ilk.drive_inprocess(bid)
    prompts = ilk.stub_prompts()
    assert prompts and prompts[0].startswith("This is a verification step.")


def test_status_by_consumer_ref_returns_the_batch_meta(ilk, capsys):
    # option (c), gh-resolve-e1's ask (Oct 14): look batches up by the consumer's own ref, and
    # keep the consumer's per-batch record (issue, run id, rework context) with the batch.
    from ilk2 import batches, cli, plans
    ilk.make_repo()
    ilk.project()
    a = batches.submit(ilk.s, "proj", plans.from_dict(one_step()), consumer_ref="issue-1",
                       meta={"issue": 1, "run_id": "r1"})
    b = batches.submit(ilk.s, "proj", plans.from_dict(one_step()), consumer_ref="issue-2")
    assert cli.main(["status", "--consumer-ref", "issue-1", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [x["id"] for x in out["batches"]] == [a]
    assert out["batches"][0]["meta"] == {"issue": 1, "run_id": "r1"}
    assert cli.main(["status", "--batch", b, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["batches"][0]["meta"] is None
    with pytest.raises(batches.SubmitRejected):
        batches.submit(ilk.s, "proj", plans.from_dict(one_step()), meta=["not", "an", "object"])


def test_project_set_and_host_budget_take_effect_without_a_restart(ilk, capsys):
    # Chad, Oct 14: at least 3 kira batches at once on both hosts. A supervisor restart would
    # interrupt every live attempt, so both settings apply live.
    from ilk2 import batches, cli, paths, supervisor
    ilk.make_repo()
    ilk.project()
    assert cli.main(["project", "set", "proj", "concurrency_limit=3", "enforce_scope=false"]) == 0
    cfg = batches.project_config(ilk.s, "proj")
    assert cfg.concurrency_limit == 3 and cfg.enforce_scope is False
    assert cli.main(["project", "set", "proj", "no_such=1"]) == 2
    sp = supervisor.Supervisor(ilk.s, host_budget=2, load_check=False)
    (paths.home() / "host.json").write_text(json.dumps({"host_budget": 4, "load_check": False}))
    sp.reload_host_config()
    assert sp.host_budget == 4


def test_three_batches_of_one_project_run_at_once(ilk):
    from ilk2 import batches, cli, plans, supervisor
    ilk.make_repo()
    ilk.project()
    assert cli.main(["project", "set", "proj", "concurrency_limit=3"]) == 0
    bids = [batches.submit(ilk.s, "proj", plans.from_dict(one_step())) for _ in range(4)]
    sp = supervisor.Supervisor(ilk.s, host_budget=4, load_check=False)
    sp.drive_cmd = ["/bin/sh", "-c", "sleep 5", "stub"]
    try:
        sp.admit()
        live = {r["batch_id"] for r in ilk.s.all("SELECT batch_id FROM runs WHERE status IN "
                                                  "('starting', 'running')")}
        assert live == set(bids[:3])                                   # 3 at once, each its own root
        assert ilk.s.one("SELECT reason FROM refusals WHERE batch_id = ? AND cleared_at IS NULL",
                         bids[3])["reason"] == "project-limit"
    finally:
        for p in sp.children.values():
            p.kill()
            p.wait()


def test_a_finished_batchs_worktree_goes_after_the_keep_window_its_branch_stays(ilk):
    from ilk2 import supervisor
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    root = Path(b["execution_root"])
    sp = supervisor.Supervisor(ilk.s, load_check=False)
    assert sp.gc_worktrees() == [] and root.exists()            # inside the keep window
    with ilk.s.tx() as c:
        c.execute("UPDATE transitions SET at = at - ? WHERE entity = 'batches' AND entity_id = ?",
                  (sp.b.worktrees_keep + 60, bid))
    assert sp.gc_worktrees() == [bid]
    assert not root.exists()
    assert ilk.git(ilk.repo, "rev-parse", "--verify", b["branch"])  # the branch stays


def test_a_repair_step_is_not_red_first_checked(ilk):
    # gh-resolve P5 batch, Oct 14: a repair edited the file of the test it repairs and was judged
    # "passes without your change". A repair's must_pass is a test that failed; it is no new pin.
    from ilk2 import batches
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    v = ilk.s.get("batches", bid)["version"]
    v = batches.amend(ilk.s, bid, [{"op": "add_plan", "slug": "repair-1", "repair_of_step_id": f"{bid}.p.0",
                                    "steps": [{"title": "Repair", "instructions": "fix the test file",
                                               "gates": [gate("tests/test_new.py")],
                                               "must_pass": ["tests/test_new.py::test_new"]}]}],
                      expect_version=v, actor="driver", reason="regression-attributed")
    passes_anyway = {"write": "tests/test_new.py", "text": "def test_new():\n    assert True\n"}
    ilk.script([passes_anyway], [GOOD])
    assert ilk.drive_inprocess(bid) == "shipped"
    rep = ilk.s.one("SELECT a.outcome FROM attempts a JOIN steps st ON st.id = a.step_id "
                    "WHERE st.plan_id = ? ORDER BY a.created_at DESC", f"{bid}.repair-1")
    assert rep["outcome"] == "accepted"  # (after its uncounted gates-first run, red here)


def test_doctor_shows_host_budget_and_each_projects_limit(ilk, capsys):
    from ilk2 import cli, paths
    ilk.make_repo()
    ilk.project()
    assert cli.main(["project", "set", "proj", "concurrency_limit=3"]) == 0
    (paths.home() / "host.json").write_text(json.dumps({"host_budget": 4, "load_check": False}))
    capsys.readouterr()
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "runs: 0 of host_budget 4" in out and "proj: 0 of 3 running, 0 queued" in out


def test_a_verification_step_reruns_its_gates_on_every_entry(ilk):
    # gh-resolve P5 batch, Oct 14: after two repairs landed, the verification step's worker was
    # briefed with the failures of its first gates-first run, which the repairs had fixed.
    from dataclasses import asdict
    from ilk2 import batches, clock
    ilk.make_repo()
    ilk.project()
    cfg = batches.project_config(ilk.s, "proj")
    cfg.verify_command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    ilk.script([GOOD])
    bid = ilk.submit(_verify_spec())
    vstep = ilk.s.one("SELECT * FROM steps WHERE batch_id = ? AND plan_id LIKE '%verify'", bid)
    rid = ilk.new_run(bid)
    ilk.s.insert("attempts", {"id": "a-stale", "run_id": rid, "step_id": vstep["id"], "n": 1,
                              "status": "done", "outcome": "red", "evidence": "stale: test_x failed",
                              "parent_commit": "x", "created_at": clock.wall()})
    ilk.s.update("steps", vstep["id"], {"attempt_count": 1}, actor="test", reason="earlier red")
    ilk.s.update("runs", rid, {"status": "ended", "terminal_reason": "drained"}, actor="test", reason="x")
    assert ilk.drive_inprocess(bid) == "shipped"
    assert ilk.stub_calls() == 1  # the work step only; the verify step needed no worker


def test_a_base_failure_is_not_charged_to_a_step_that_changes_the_gates_config(ilk):
    # gh-resolve P5 batch, Oct 14: the batch changed conftest.py, so the full-suite gate had "no
    # comparable base" and its 12 base failures counted as new: verification could never pass.
    ilk.make_repo()
    root = ilk.repo
    (root / "tests" / "test_old.py").write_text("def test_old():\n    assert False\n")
    ilk.git(root, "add", "-A")
    ilk.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a failing base test")
    ilk.project()
    conftest = {"write": "tests/conftest.py", "text": "# the step changes the gate's config\n"}
    ilk.script([GOOD, conftest])
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_old.py")]))
    assert ilk.drive_inprocess(bid) == "shipped"
    gr = ilk.s.one("SELECT details FROM gate_results ORDER BY id DESC LIMIT 1")
    assert json.loads(gr["details"])["comparable_base"] is False   # recorded as before


def test_a_base_failure_whose_message_the_config_change_alters_is_not_new(ilk):
    # batch 18a's shape (docs/evidence/2026-10-18-batch-18a-retrospective.md): the step changes
    # the gate's configuration in a way that changes what a failure's report looks like
    # (--tb=long -> --tb=short changes the frames the fingerprint hashes). Measured under the
    # base's own configuration the pre-existing failure fingerprint-shifts and reads as new;
    # measured under this step's configuration both sides match again (docs/02 §2.5.4).
    ilk.make_repo()
    root = ilk.repo
    (root / "tests" / "test_old.py").write_text("def test_old():\n    assert False\n")
    (root / "pytest.ini").write_text("[pytest]\naddopts = --tb=long\n")
    ilk.git(root, "add", "-A")
    ilk.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a failing base test")
    ilk.project()
    conf = {"write": "pytest.ini", "text": "[pytest]\naddopts = --tb=short\n"}
    ilk.script([GOOD, conf])
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_old.py")]))
    assert ilk.drive_inprocess(bid) == "shipped"
    gr = ilk.s.one("SELECT new_failures, details FROM gate_results ORDER BY id DESC LIMIT 1")
    assert json.loads(gr["new_failures"]) == []
    details = json.loads(gr["details"])
    assert details["comparable_base"] is False       # the gate is still recorded as spec-changed
    assert details["base_under"] == "step-config"


def test_a_failure_the_step_introduced_is_still_new_when_the_spec_changed(ilk):
    # I6's strength under the overlay (docs/02 §2.5.4): a base-red test the step broke
    # differently, or a test that passed at the base and fails now, is still new.
    ilk.make_repo()
    root = ilk.repo
    (root / "tests" / "test_old.py").write_text("def test_old():\n    assert False\n")
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (root / "pytest.ini").write_text("[pytest]\naddopts = --tb=long\n")
    ilk.git(root, "add", "-A")
    ilk.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a failing base test")
    ilk.project()
    broke = {"write": "tests/test_ok.py", "text": "def test_ok():\n    assert False\n"}
    ilk.script([GOOD, {"write": "pytest.ini", "text": "[pytest]\naddopts = --tb=short\n"}, broke])
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_old.py",
                                           "tests/test_ok.py")]))
    assert ilk.drive_inprocess(bid) == "blocked"
    gr = ilk.s.one("SELECT new_failures FROM gate_results ORDER BY id LIMIT 1")
    assert json.loads(gr["new_failures"]) == ["tests/test_ok.py::test_ok"]


def test_a_head_config_that_cannot_run_at_the_base_falls_back_to_the_bases_own_base(ilk):
    # The overlay is A2's one exception (base measurement only). When the head's configuration
    # cannot run on the base source at all -- its conftest imports a module only the step adds
    # -- the overlaid measurement is unmeasured. That must not block the batch the way a gate
    # nothing can measure does (base-unmeasured): it falls back to the base's own measurement
    # and says so in the gate's evidence.
    ilk.make_repo()
    root = ilk.repo
    (root / "tests" / "test_old.py").write_text("def test_old():\n    assert False\n")
    ilk.git(root, "add", "-A")
    ilk.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a failing base test")
    ilk.project()
    conf = {"write": "tests/conftest.py", "text":
            "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
            "import headonly\n"}
    ilk.script([GOOD, conf, {"write": "headonly.py", "text": "X = 1\n"}])
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_old.py")]))
    assert ilk.drive_inprocess(bid) == "shipped"      # not "blocked (base-unmeasured)"
    gr = ilk.s.one("SELECT new_failures, details FROM gate_results ORDER BY id DESC LIMIT 1")
    assert json.loads(gr["new_failures"]) == []       # the base's own measurement still matched
    details = json.loads(gr["details"])
    assert details["comparable_base"] is False and details["base_under"] == "base-config"
    fell_back = [e for e in ilk.s.events_since(0, bid) if e["kind"] == "base.overlay-unmeasured"]
    assert [e["payload"]["gate"] for e in fell_back] == ["unit"]


def test_a_verification_step_gates_its_kept_work_first(ilk):
    # gh-resolve P5 batch, Oct 14: after a restart the kept work skipped gates-first, and the
    # worker was briefed with the previous attempt's failures.
    from dataclasses import asdict
    from ilk2 import batches, clock
    ilk.make_repo()
    ilk.project()
    cfg = batches.project_config(ilk.s, "proj")
    cfg.verify_command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    spec = _verify_spec()
    spec["plans"] = spec["plans"][1:]
    spec["plans"][0]["depends_on"] = []
    bid = ilk.submit(spec)
    root = Path(ilk.s.get("batches", bid)["execution_root"])
    vstep = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    rid = ilk.new_run(bid)
    ilk.s.insert("attempts", {"id": "a-old", "run_id": rid, "step_id": vstep["id"], "n": 1,
                              "status": "done", "outcome": "interrupted", "parent_commit": "x",
                              "created_at": clock.wall()})
    ilk.s.update("runs", rid, {"status": "ended", "terminal_reason": "drained"}, actor="test", reason="x")
    (root / "calc.py").write_text(GOOD["text"])            # the earlier attempt's kept work
    ilk.script([])
    assert ilk.drive_inprocess(bid) == "shipped"
    assert ilk.stub_calls() == 0                           # gated first: no worker needed
    assert ilk.git(root, "show", f"{ilk.s.get('batches', bid)['branch']}:calc.py") == GOOD["text"].strip()


def test_a_copy_of_the_repository_inside_the_worktree_is_red(ilk):
    # gh-resolve P5 batch, Oct 14: 1,111 files under .base-tree/ were nearly committed.
    ilk.make_repo()
    root = ilk.repo
    for i in range(60):
        (root / f"m{i}.txt").write_text(str(i))
    ilk.git(root, "add", "-A")
    ilk.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "60 tracked files")
    ilk.project()
    copy = [{"write": f".base-tree/m{i}.txt", "text": str(i)} for i in range(60)]
    ilk.script([GOOD] + copy, [GOOD, {"delete": ".base-tree"}])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    atts = ilk.s.all("SELECT outcome, evidence FROM attempts ORDER BY n")
    assert atts[0]["outcome"] == "red" and "copied the repository" in atts[0]["evidence"]


def test_a_red_gates_first_run_leaves_the_worker_its_attempt(ilk):
    # gh-resolve P5 batch, Oct 14: gates-first took the 5th attempt and blocked the step.
    from dataclasses import asdict
    from ilk2 import batches, bounds, clock
    ilk.make_repo()
    ilk.project()
    cfg = batches.project_config(ilk.s, "proj")
    cfg.verify_command = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test", reason="x")
    spec = _verify_spec()
    spec["plans"] = spec["plans"][1:]
    spec["plans"][0]["depends_on"] = []
    bid = ilk.submit(spec)
    root = Path(ilk.s.get("batches", bid)["execution_root"])
    (root / "tests" / "test_new_red.py").write_text("def test_x():\n    assert False\n")
    ilk.git(root, "add", "-A")
    ilk.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "red at the head")
    vstep = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    limit = bounds.ATTEMPTS_PER_STEP + bounds.ESCALATION_ATTEMPTS
    ilk.s.update("steps", vstep["id"], {"attempt_count": limit - 1}, actor="test", reason="4 used")
    ilk.script([{"delete": "tests/test_new_red.py"}])
    assert ilk.drive_inprocess(bid) == "shipped"
    assert ilk.stub_calls() == 1                            # the worker got the last attempt


def test_the_p6_consumer_sequence_end_to_end(ilk, tmp_path, capsys):
    # gh-resolve's P6 live adapter (Oct 14): submit without --root on branch resolver-<issue>;
    # ship; take the root's lease, add the consumer's own commit (merge/push side), release with
    # the new head; a rework batch on the same branch then builds on that head, in the same root.
    from ilk2 import cli

    def call(*argv):
        capsys.readouterr()
        rc = cli.main(list(argv))
        out = capsys.readouterr().out
        return rc, (json.loads(out) if out.strip().startswith("{") else out)
    ilk.make_repo()
    ilk.project()
    plan = tmp_path / "issue-1.json"
    plan.write_text(json.dumps(one_step()))
    ilk.script([GOOD], [{"write": "rework.txt", "text": "r"}])
    rc, out = call("submit", str(plan), "--project", "proj", "--branch", "resolver-1",
                   "--consumer-ref", "run-1", "--meta", '{"issue": 1, "run_id": "run-1"}', "--json")
    assert rc == 0
    bid = out["batch"]
    assert ilk.drive_inprocess(bid) == "shipped"
    rc, st = call("status", "--consumer-ref", "run-1", "--json")
    b = st["batches"][0]
    assert b["status"] == "shipped" and b["meta"] == {"issue": 1, "run_id": "run-1"}
    root, head, gen = b["execution_root"], b["accepted_head"], b["root_lease"]["generation"]
    assert b["branch"] == "resolver-1" and b["root_lease"]["holder"] == "released"
    assert checked_out(ilk, root) is None                       # shipped: the branch is free
    rc, lk = call("lease", "acquire", "--root", root, "--generation", str(gen),
                  "--expected-head", head, "--owner", "gh-resolve", "--length", "600")
    assert rc == 0
    assert checked_out(ilk, root) == "refs/heads/resolver-1"    # leased: back on the branch
    (Path(root) / "merged-from-base.txt").write_text("m")        # the consumer's own commit
    ilk.git(root, "add", "-A")
    ilk.git(root, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-qm", "merge base")
    new = ilk.git(root, "rev-parse", "HEAD")
    rc, _ = call("lease", "release", "--root", root, "--generation", str(lk["generation"]),
                 "--new-head", new)
    assert rc == 0
    assert checked_out(ilk, root) is None                       # released: free again
    rc, out = call("submit", str(plan), "--project", "proj", "--branch", "resolver-1",
                   "--consumer-ref", "run-1b", "--json")
    assert rc == 0
    rework = out["batch"]
    assert ilk.drive_inprocess(rework) == "shipped"
    r = ilk.s.get("batches", rework)
    assert r["execution_root"] == root                          # same root, reused for rework
    tip = ilk.git(root, "rev-parse", "resolver-1")
    assert ilk.git(root, "merge-base", "--is-ancestor", new, tip) == ""  # built on the consumer's head
    assert ilk.git(root, "show", "resolver-1:rework.txt") == "r"


def test_a_base_failure_with_a_changed_message_is_labelled_pre_existing(ilk):
    # gh-resolve, Oct 14: a test failing at the base whose message embeds the code's calls read as
    # new after the batch added calls; the worker was not told it had failed at the base already.
    ilk.make_repo({"calc.py": "def add(a, b):\n    return a - b\n",
                   "tests/conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n",
                   "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
                   "tests/test_calls.py": "import calc\ndef test_calls():\n    assert False, open(calc.__file__).read()\n"})
    ilk.project()
    ilk.script([{"write": "calc.py", "text": "def add(a, b):\n    return a + b\n# new call\n"}])
    bid = ilk.submit(one_step(gates_=[gate("tests/test_calc.py", "tests/test_calls.py")]))
    ilk.drive_inprocess(bid)
    ev = ilk.s.one("SELECT evidence FROM attempts WHERE evidence IS NOT NULL ORDER BY created_at LIMIT 1")
    assert ev and "tests/test_calls.py::test_calls [also failing at the batch base, message differs" in ev["evidence"]


def test_ilk2_apply_brings_a_main_fix_into_the_running_batch(ilk, tmp_path, capsys):
    # gh-resolve, Oct 14: a test-isolation fix landed on main (2e3ef059) while two batches ran;
    # the build session paused them and applied it by hand. `ilk2 apply` does it.
    from ilk2 import cli
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit({"plans": [{"slug": "p", "steps": [
        {"title": "s0", "instructions": "x", "gates": [gate("tests/test_calc.py")]},
        {"title": "s1", "instructions": "y", "gates": [gate("tests/test_calc.py")]}]}]})
    # a fix on another branch of the consumer's repo
    ilk.git(ilk.repo, "checkout", "-q", "-b", "fixbranch")
    (ilk.repo / "fix.txt").write_text("hermetic\n")
    ilk.git(ilk.repo, "add", "-A")
    ilk.git(ilk.repo, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-qm", "the fix")
    fix = ilk.git(ilk.repo, "rev-parse", "HEAD")
    ilk.git(ilk.repo, "checkout", "-q", "-")
    capsys.readouterr()
    assert cli.main(["apply", bid, fix[:10]]) == 0
    assert json.loads(capsys.readouterr().out)["commits"] == [fix]
    ilk.script([GOOD], [{"write": "notes.txt", "text": "n"}])
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    assert ilk.git(ilk.repo, "show", f"{b['branch']}:fix.txt") == "hermetic"
    res = [e["payload"]["result"] for e in ilk.s.events_since(0, bid) if e["kind"] == "commit.applied"]
    assert res == ["applied"]          # step 0 brought it in; step 1 does not re-apply it (P6, Oct 14)
    assert fix[:12] in ilk.stub_prompts()[0]                 # the worker is told
    assert cli.main(["apply", bid, "nosuchcommit"]) == 2


def test_ilk2_apply_leaves_a_conflict_with_kept_work_to_the_worker_once(ilk, capsys):
    # gh-resolve P6, Oct 14: the fix overlapped the worker's kept work. The conflict goes to the
    # worker (no needs_human), and a later entry into the same step does not apply it again over
    # the resolution.
    from ilk2 import cli
    from ilk2.driver import Driver
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    ilk.git(ilk.repo, "checkout", "-q", "-b", "fixbranch")
    (ilk.repo / "calc.py").write_text("def add(a, b):\n    return a + b  # main's fix\n")
    ilk.git(ilk.repo, "-c", "user.name=c", "-c", "user.email=c@c", "commit", "-qam", "the fix")
    fix = ilk.git(ilk.repo, "rev-parse", "HEAD")
    ilk.git(ilk.repo, "checkout", "-q", "-")
    assert cli.main(["apply", bid, fix]) == 0
    capsys.readouterr()
    d = Driver(ilk.s, ilk.new_run(bid))
    (d.root / "calc.py").write_text("def add(a, b):\n    return b + a  # the worker's\n")  # kept work
    step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    done, conflicted = d.apply_requested(step, kept=True)
    assert done == [fix[:12]] and conflicted == ["calc.py"]
    assert "<<<<<<<" in (d.root / "calc.py").read_text()
    assert not ilk.s.one("SELECT 1 FROM needs_human WHERE batch_id = ? AND state = 'open'", bid)
    (d.root / "calc.py").write_text("def add(a, b):\n    return a + b  # resolved\n")
    assert d.apply_requested(step, kept=True) == ([], [])  # not applied over the resolution
    assert "resolved" in (d.root / "calc.py").read_text()


def test_a_projects_batches_on_one_base_share_its_measurement(ilk):
    # F5 (docs/06): batches on the same base re-measured it; a rezmac base suite costs a median
    # 710 s (E2). Same project, spec and commit: reused, never older than a day.
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD], [GOOD], [GOOD])
    first = ilk.submit(one_step())
    assert ilk.drive_inprocess(first) == "shipped"
    second = ilk.submit(one_step())
    assert ilk.drive_inprocess(second) == "shipped"
    reused = [e["payload"] for e in ilk.s.events_since(0, second) if e["kind"] == "base.reused"]
    assert [r["from_batch"] for r in reused] == [first]
    rows = ilk.s.all("SELECT batch_id, at FROM base_measurements WHERE batch_id IN (?, ?)", first, second)
    assert len({r["at"] for r in rows}) == 1                   # the original time, not extended
    with ilk.s.tx() as c:
        c.execute("UPDATE base_measurements SET at = at - 90000")  # older than a day
    third = ilk.submit(one_step())
    assert ilk.drive_inprocess(third) == "shipped"
    assert not [e for e in ilk.s.events_since(0, third) if e["kind"] == "base.reused"]


def test_two_batches_started_together_measure_the_base_once(ilk, monkeypatch):
    # V3 (docs/09): 18b and 18c started 24 s apart and each recorded 11 base.measured events and
    # 0 base.reused -- F5 only reuses a measurement that is already stored, so both measured.
    # One claim per measurement key: the loser waits for the owner's result.
    import threading
    import time
    from ilk2 import gates, paths
    from ilk2.driver import Driver
    from ilk2.store import Store

    ilk.make_repo()
    ilk.project()
    a, b = ilk.submit(one_step()), ilk.submit(one_step())
    ready = {}
    for name, bid in (("a", a), ("b", b)):  # prepared serially: only the measurements race
        s = Store(paths.db())
        d = Driver(s, ilk.new_run(bid))
        ready[name] = {"s": s, "d": d, "gate": d.all_gates()[0], "where": d.diag_worktree(d.base)}
    calls, results, errors = [], {}, {}
    real = gates.measure

    def counted(*args, **kwargs):
        calls.append(1)
        time.sleep(0.3)  # the window in which both batches used to measure
        return real(*args, **kwargs)
    monkeypatch.setattr(gates, "measure", counted)
    barrier = threading.Barrier(2)

    def one(name):
        r = ready[name]
        try:
            barrier.wait()
            results[name] = r["d"].measure_base(r["gate"], r["where"], r["d"].base)
        except Exception as e:  # noqa: BLE001 - reported below
            errors[name] = e
    ts = [threading.Thread(target=one, args=(n,)) for n in ready]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    for r in ready.values():
        r["s"].close()
    assert not errors, errors
    assert len(calls) == 1  # the gate ran once for the pair
    rows = ilk.s.all("SELECT batch_id, at FROM base_measurements WHERE batch_id IN (?, ?)", a, b)
    assert len(rows) == 2 and len({r["at"] for r in rows}) == 1  # the copy keeps the owner's time
    if [e for e in ilk.s.events_since(0, a) if e["kind"] == "base.measured"]:
        winner, loser = a, b
    else:
        winner, loser = b, a
    assert not [e for e in ilk.s.events_since(0, loser) if e["kind"] == "base.measured"]
    reused = [e["payload"] for e in ilk.s.events_since(0, loser) if e["kind"] == "base.reused"]
    assert [r["from_batch"] for r in reused] == [winner]
    assert reused[0]["waited_s"] >= 0


def test_a_base_claim_whose_process_is_dead_is_taken_over(ilk):
    # V3 (docs/09): a claim is waited on only while its owner lives. Dead is the supervisor's
    # identity test (boot, then (pid, start time); supervisor.py reconcile): this owner is a
    # process that has exited.
    import subprocess
    from ilk2 import clock, procs
    from ilk2.driver import Driver

    ilk.make_repo()
    ilk.project()
    p = subprocess.Popen(["sleep", "0"])
    was = procs.start_time(p.pid)  # what a live owner records
    p.wait()
    dead = p.pid  # its pid, used after it exits
    bid = ilk.submit(one_step())
    d = Driver(ilk.s, ilk.new_run(bid))
    g, where = d.all_gates()[0], d.diag_worktree(d.base)
    key = d.measure_key(g, where, d.base)
    ilk.s.insert("base_claims", {"measure_key": key, "owner_run": "r-dead", "pid": dead,
                                 "proc_start": was, "boot_time": clock.boot_time(),
                                 "started_at": clock.wall()})
    d.measure_base(g, where, d.base)
    assert [e for e in ilk.s.events_since(0, bid) if e["kind"] == "base.measured"]  # not waited on
    took = [e["payload"] for e in ilk.s.events_since(0, bid) if e["kind"] == "base.takeover"]
    assert [t["from_run"] for t in took] == ["r-dead"] and took[0]["pid"] == dead
    assert ilk.s.one("SELECT * FROM base_measurements WHERE batch_id = ?", bid)
    assert not ilk.s.one("SELECT 1 FROM base_claims WHERE measure_key = ?", key)


def test_a_changed_setup_command_or_home_file_re_measures(ilk, monkeypatch):
    # V3 (docs/09): the measurement key adds what spec_version lacks -- the project's setup
    # command and the home files the pinned HOME copies in. A change in either is a different
    # measurement, so F5 never adopts it; with nothing changed, F5 still does.
    from dataclasses import asdict
    from ilk2 import batches
    from ilk2.driver import Driver

    host = ilk.tmp / "host-home"
    host.mkdir()
    (host / ".npmrc").write_text("//registry.npmjs.org/:_authToken=one\n")
    monkeypatch.setenv("HOME", str(host))
    ilk.make_repo()
    ilk.project(home_files=[".npmrc"], setup={"command": ["echo", "one"]})

    def measure() -> str:
        bid = ilk.submit(one_step())
        d = Driver(ilk.s, ilk.new_run(bid))
        d.measure_base(d.all_gates()[0], d.diag_worktree(d.base), d.base)
        return bid

    def kinds(bid: str) -> set[str]:
        return {e["kind"] for e in ilk.s.events_since(0, bid) if e["kind"].startswith("base.")}

    first = measure()
    assert kinds(first) == {"base.measured"}
    (host / ".npmrc").write_text("//registry.npmjs.org/:_authToken=two\n")  # a home file changed
    second = measure()
    assert kinds(second) == {"base.measured"}
    cfg = batches.project_config(ilk.s, "proj")
    cfg.setup = {"command": ["echo", "two"]}
    ilk.s.update("projects", "proj", {"config": json.dumps(asdict(cfg))}, actor="test",
                 reason="a changed setup command")
    third = measure()
    assert kinds(third) == {"base.measured"}
    fourth = measure()  # nothing changed
    assert kinds(fourth) == {"base.reused"}
    reused = [e["payload"] for e in ilk.s.events_since(0, fourth) if e["kind"] == "base.reused"]
    assert [r["from_batch"] for r in reused] == [third]  # F5 keeps working


def test_a_base_measurement_records_its_cost(ilk):
    # A live kira base measurement took 1,794 s against a 900 s cap (Oct 14) and nothing could
    # tell whether that was one slow run or a capped run plus the re-run gates.measure makes for
    # a transient (capped) result. Every measurement that actually runs records its cost.
    ilk.make_repo({"calc.py": "def add(a, b):\n    return a + b\n",
                   "tests/conftest.py": "import sys, os\n"
                   "sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n",
                   "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"})
    ilk.project()
    ilk.script([{"write": "note.md", "text": "done\n"}])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    ev = [e["payload"] for e in ilk.s.events_since(0, bid) if e["kind"] == "base.measured"]
    assert len(ev) == 1  # exactly one per gate, and only for a measurement that ran
    p = ev[0]
    assert p["gate"] == "unit" and len(p["commit"]) == 12
    assert p["outcome"] == "pass" and p["failures"] == 0
    assert p["seconds"] >= 0 and p["timeouts"] == 0 and p["reran"] is False
    # A gate that outlives its cap is a transient unmeasured, so measure() runs it again; the
    # cost event is written even though the batch then blocks as base-unmeasured.
    slow = {"name": "slow", "adapter": "shell", "declares_tests": False,
            "command": ["sleep", "3"], "cap_s": 1}
    bid = ilk.submit(one_step(gates_=[slow]))
    ilk.drive_inprocess(bid)  # blocks as base-unmeasured: assert the event, not the outcome
    ev = [e["payload"] for e in ilk.s.events_since(0, bid) if e["kind"] == "base.measured"]
    assert len(ev) == 1
    assert ev[0]["timeouts"] >= 1 and ev[0]["reran"] is True


def test_a_gate_path_another_plan_creates_is_owed_by_that_plan(ilk):
    # gh-resolve P6, Oct 14: V1 plans' gates list every sibling's new test file. A plan that
    # runs before (or beside) the plan that creates tests/test_two.py is not red for its absence.
    ilk.make_repo()
    ilk.project()
    both = gate("tests/test_calc.py", "tests/test_two.py")
    bid = ilk.submit({"plans": [
        {"slug": "p1", "steps": [{"title": "fix add", "instructions": "x", "gates": [both],
                                  "scope": ["calc.py"]}]},
        {"slug": "p2", "steps": [{"title": "add test two", "instructions": "y", "gates": [both],
                                  "scope": ["tests/test_two.py"]}]}]})
    ilk.script([GOOD], [{"write": "tests/test_two.py", "text": "def test_two():\n    assert True\n"}])
    assert ilk.drive_inprocess(bid) == "shipped"
    outs = [a["outcome"] for a in ilk.s.all("SELECT outcome FROM attempts ORDER BY created_at")]
    assert outs == ["accepted", "accepted"]
    owed = [e["payload"]["owed"] for e in ilk.s.events_since(0, bid) if e["kind"] == "gate.path-owed"]
    assert owed == [{"tests/test_two.py": "p2"}]


def test_kept_work_is_gated_before_the_worker_so_its_brief_is_current(ilk):
    # gh-resolve P6, Oct 14: after a restart the brief repeated the last attempt's evidence (a
    # sibling's test file "absent"), which the restarted driver no longer charged; the worker
    # wrote that file and it conflicted at the merge. Kept work is gated first, not counted.
    from ilk2 import clock
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    root = Path(ilk.s.get("batches", bid)["execution_root"])
    step = ilk.s.one("SELECT * FROM steps WHERE batch_id = ?", bid)
    rid = ilk.new_run(bid)
    ilk.s.insert("attempts", {"id": "a-old", "run_id": rid, "step_id": step["id"], "n": 1,
                              "status": "done", "outcome": "red", "parent_commit": "x",
                              "evidence": "STALE: tests/test_two.py (declared, absent after the step)",
                              "created_at": clock.wall()})
    ilk.s.update("steps", step["id"], {"attempt_count": 1}, actor="test", reason="one red")
    ilk.s.update("runs", rid, {"status": "ended", "terminal_reason": "drained"}, actor="test",
                 reason="drained")
    (root / "calc.py").write_text(GOOD["text"])           # kept work: already green
    ilk.script([])
    assert ilk.drive_inprocess(bid) == "shipped"
    prompt = ilk.stub_prompts()[0]
    assert "STALE" not in prompt and "gates pass on the kept changes" in prompt
    outs = [(a["n"], a["outcome"]) for a in ilk.s.all("SELECT n, outcome FROM attempts ORDER BY created_at")]
    assert outs == [(1, "red"), (2, "measured"), (2, "accepted")]  # the measurement is not counted
    from ilk2 import report
    row = report.steps(ilk.s, bid)["steps"][0]
    assert (row["attempts"], row["red"]) == (2, 1)                 # the report leaves it out too


def test_a_repair_whose_test_already_passes_is_accepted_with_no_worker(ilk):
    # gh-resolve P6, Oct 14: one bad line failed three guard tests, so verification added three
    # repairs. After the first fixed the line, the others had nothing to change; a no-change
    # attempt is red, and each would have blocked the batch after five attempts.
    from ilk2 import batches
    ilk.make_repo({"calc.py": "def add(a, b):\n    return a - b\n",
                   "tests/conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n",
                   "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n",
                   "tests/test_guard.py": "def test_guard():\n    assert True\n"})
    ilk.project()
    bid = ilk.submit(one_step())
    v = ilk.s.get("batches", bid)["version"]
    # the repair runs first (repairs come before other work) and its test passes already
    batches.amend(ilk.s, bid, [{"op": "add_plan", "slug": "repair-1", "repair_of_step_id": f"{bid}.p.0",
                                "steps": [{"title": "Repair", "instructions": "fix the guard",
                                           "gates": [gate("tests/test_guard.py")],
                                           "must_pass": ["tests/test_guard.py::test_guard"]}]}],
                  expect_version=v, actor="driver", reason="regression-attributed")
    ilk.script([GOOD])                        # only step 0's worker; the repair needs none
    assert ilk.drive_inprocess(bid) == "shipped"
    rep = ilk.s.all("SELECT a.outcome, a.reason FROM attempts a JOIN steps st ON st.id = a.step_id "
                    "WHERE st.plan_id = ?", f"{bid}.repair-1")
    assert [(r["outcome"], r["reason"]) for r in rep] == [("accepted", "gates green; no worker needed")]
    assert len(ilk.stub_prompts()) == 1


def test_an_interrupted_phase_still_records_its_end(ilk, monkeypatch):
    # A with-block that raises (Stop from a park --now or a cancel, an API back-off Stop, any
    # driver error) used to leave its phase.start unclosed, so the events could not say how
    # long the phase lasted and report.py had to guess at the next phase.start or the run end.
    from ilk2.driver import Driver
    from ilk2.runs import Stop

    def raise_stop(*args, **kwargs):
        raise Stop("parked", "test")

    # The step's dispatch, inside phase("dispatch"); Driver.dispatch itself holds that phase,
    # so raising from it would emit no dispatch phase at all.
    monkeypatch.setattr("ilk2.worker.dispatch", raise_stop)
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    rid = ilk.new_run(bid)
    assert Driver(ilk.s, rid).run() == "parked"  # the Stop still ends the run, unchanged
    evs = [(r["kind"], json.loads(r["payload"])) for r in ilk.s.all(
        "SELECT kind, payload FROM events WHERE run_id = ? AND kind IN "
        "('phase.start', 'phase.end') ORDER BY cursor", rid)]
    ends = [p for k, p in evs if k == "phase.end" and p["phase"] == "dispatch"]
    assert len(ends) == 1
    assert ends[0]["interrupted"] is True and ends[0]["error"] == "Stop"
    assert ends[0]["reason"] == "parked" and ends[0]["step"]  # the payload keeps its own fields
    starts = [p["phase"] for k, p in evs if k == "phase.start"]
    assert "dispatch" in starts
    assert sorted(starts) == sorted(p["phase"] for k, p in evs if k == "phase.end")


def test_ilk2_commits_as_the_repositorys_configured_user(ilk):
    # gh-resolve on rezmac kira-live, Oct 14: its foreign-commit guard trusts only the clone's
    # configured identity and declined #8716's publish of commits by ilk2@localhost. V1's
    # workers committed as the repository's user.
    ilk.make_repo()
    ilk.git(ilk.repo, "config", "user.name", "chad007")
    ilk.git(ilk.repo, "config", "user.email", "chad007@users.noreply.github.com")
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    who = ilk.git(ilk.repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>", b["branch"])
    assert who == "chad007 <chad007@users.noreply.github.com>|chad007 <chad007@users.noreply.github.com>"


def test_ilk2_commits_as_itself_when_the_repository_names_no_user(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    assert ilk.git(ilk.repo, "log", "-1", "--format=%ae", b["branch"]) == "ilk2@localhost"


def test_recovery_runs_record_when_they_ended(ilk):
    # Oct 16: park-recovery and cancel-recovery runs were inserted ended but with no ended_at, so
    # reports that read ended_at counted them as running until now.
    from ilk2 import batches as b_
    from ilk2.driver import finish_cancel
    from ilk2.supervisor import Supervisor
    ilk.make_repo()
    ilk.project()
    parked = ilk.submit(one_step())
    Supervisor(ilk.s, load_check=False).finish_park(parked)
    cancelled = ilk.submit(one_step())
    b_.cancel(ilk.s, cancelled, "human")
    finish_cancel(ilk.s, cancelled)
    rows = ilk.s.all("SELECT terminal_reason, created_at, ended_at FROM runs WHERE terminal_reason "
                     "IN ('park-recovery', 'cancel-recovery')")
    assert {r["terminal_reason"] for r in rows} == {"park-recovery", "cancel-recovery"}
    assert all(r["ended_at"] is not None and r["ended_at"] >= r["created_at"] for r in rows)


def test_status_says_when_a_batch_shipped_and_what_its_root_deps_were_installed_for(ilk):
    # gh-resolve D-549/D-550, Oct 16: a probe linked a root's node_modules installed for a
    # lockfile 900 lines behind its base, and a shipped batch had no ship time to cap reap with.
    from ilk2 import cli
    files = {"calc.py": "def add(a, b):\n    return a - b\n", "uv.lock": "version = 1\n",
             "tests/conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n",
             "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n"}
    ilk.make_repo(files)
    ilk.project(setup={"command": ["python", "-c", "open('installed', 'w').write('x')"], "cap_s": 60})
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    (b,) = cli.status(ilk.s, bid)["batches"]
    assert b["shipped_at"] and b["shipped_at"] >= ilk.s.get("batches", bid)["created_at"]
    su = b["setup"]
    assert su["ok"] is True and su["lock_digest"] and su["commit"] and su["deps_current"] is True
    assert su["seconds"] >= 0
    Path(b["execution_root"], "uv.lock").write_text("version = 2\n")      # a merge changed it
    assert cli.status(ilk.s, bid)["batches"][0]["setup"]["deps_current"] is False


def test_status_without_a_setup_hook_has_no_setup_record(ilk):
    from ilk2 import cli
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    (b,) = cli.status(ilk.s, bid)["batches"]
    assert b["setup"] is None and b["shipped_at"]


def test_a_batch_records_how_far_its_base_is_behind_the_default_branch(ilk, tmp_path, monkeypatch, capsys):
    # gh-resolve D-555, Oct 16: #8644 built on a stale dev commit whose checks were already red.
    import subprocess as sp
    from ilk2 import batches as b_, cli
    ilk.make_repo()
    origin = tmp_path / "origin.git"
    sp.run(["git", "clone", "-q", "--bare", str(ilk.repo), str(origin)], check=True)
    ilk.git(ilk.repo, "remote", "add", "origin", str(origin))
    other = tmp_path / "other"
    sp.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    for i in range(3):                                         # dev moves on by 3 commits
        sp.run(["git", "-C", str(other), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                "--allow-empty", "-m", f"dev {i}"], check=True)
    sp.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True)
    ilk.project()
    bid = ilk.submit(one_step())                               # base: the local main, now behind
    (b,) = cli.status(ilk.s, bid)["batches"]
    assert b["base_behind"]["commits"] == 3 and b["base_behind"]["default"] == "origin/main"
    assert b_.base_warning(b["base_behind"]) is None           # under the default threshold
    monkeypatch.setattr(b_, "BASE_BEHIND_WARN", (2, 99.0))
    assert "3 commits" in b_.base_warning(b["base_behind"])
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps(one_step()))
    capsys.readouterr()
    assert cli.main(["submit", str(spec), "--project", "proj", "--check"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["base_behind"]["commits"] == 3 and "refresh the branch first" in out["warnings"][0]


PLAN_WIDE = """---
plan: issue-1-work-x
status: pending
local_checks:
  - command: bash check.sh
  - command: python -m pytest -q -p no:cacheprovider tests/test_calc.py
scope_paths:
  - "calc.py"
---

## Steps

### Step 0

Fix add.
"""
CHECK_SH = 'n=$(grep -c . regressions.txt); while read -r l; do echo "❌ $l"; done < regressions.txt; ' \
           '[ "$n" -gt 0 ] && { echo "🚨 $n regression(s) found"; exit 1; }; exit 0\n'


def plan_wide_repo(ilk, text=PLAN_WIDE):
    ilk.make_repo({"calc.py": "def add(a, b):\n    return a - b\n", "check.sh": CHECK_SH,
                   "regressions.txt": "prd inventory missing apple-messages\nprd boundary snippet\n",
                   "tests/conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n",
                   "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(1, 2) == 3\n"})
    ilk.project()
    from ilk2 import batches as b_, plans
    return b_.submit(ilk.s, "proj", plans.BatchSpec(plans=[plans.parse_v1_subplan(text)]))


def test_a_plan_wide_check_red_at_the_base_does_not_make_the_step_fix_it(ilk):
    # gh-resolve #8644 (Chad's go, Oct 16): consistency-regression.sh was red at a stale base, and
    # the absolute rule made the worker fix two unrelated PRD regressions.
    bid = plan_wide_repo(ilk)
    ilk.script([GOOD])
    assert ilk.drive_inprocess(bid) == "shipped"
    ev = [e["payload"] for e in ilk.s.events_since(0, bid) if e["kind"] == "gate.inherited-red"]
    assert ev and any("prd inventory" in ln for ln in ev[0]["lines"])
    b = ilk.s.get("batches", bid)
    changed = ilk.git(ilk.repo, "diff", "--name-only", f"{b['base_commit']}..{b['branch']}")
    assert changed == "calc.py"                                   # the PRD regressions untouched


def test_a_new_regression_in_a_plan_wide_check_is_still_red_and_named(ilk):
    bid = plan_wide_repo(ilk, PLAN_WIDE.replace('  - "calc.py"', '  - "calc.py"\n  - "regressions.txt"'))
    third = {"append": "regressions.txt", "text": "a new one this step made\n"}
    ilk.script([GOOD, third], [GOOD, third], [GOOD, third], [GOOD, third], [GOOD, third])
    assert ilk.drive_inprocess(bid) == "blocked"
    ev = ilk.s.one("SELECT evidence FROM attempts WHERE evidence IS NOT NULL ORDER BY created_at LIMIT 1")
    assert "new since the batch base" in ev["evidence"] and "a new one this step made" in ev["evidence"]
    assert "prd inventory" not in ev["evidence"].split("new since the batch base")[1].split("\n")[0]


def _stub_args(ilk) -> list[list[str]]:
    log = Path(str(ilk.script_path) + ".calls")
    return [json.loads(line).get("args", []) for line in log.read_text().splitlines()]


def test_a_projects_worker_model_and_effort_reach_the_worker_and_are_recorded(ilk):
    # docs/03 §3.4's levers, built Oct 18 after batch 18a's retrospective: per project, the
    # worker's model and effort. Unset, the worker command is exactly as before.
    ilk.make_repo()
    ilk.project(worker_model="mimo-test", worker_effort="low")
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    args = _stub_args(ilk)[0]
    assert args[args.index("--model") + 1] == "mimo-test" and args[args.index("--effort") + 1] == "low"
    tm = json.loads(ilk.s.one("SELECT timings FROM attempts")["timings"])
    assert (tm["requested_model"], tm["effort"]) == ("mimo-test", "low")


def test_without_the_settings_the_worker_command_has_no_model_or_effort(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    assert ilk.drive_inprocess(ilk.submit(one_step())) == "shipped"
    args = _stub_args(ilk)[0]
    assert "--model" not in args and "--effort" not in args


def test_project_set_refuses_an_unknown_effort(ilk, capsys):
    from ilk2 import cli
    ilk.make_repo()
    ilk.project()
    assert cli.main(["project", "set", "proj", "worker_effort=fast"]) == 2
    assert cli.main(["project", "set", "proj", "worker_effort=low"]) == 0


def checked_out(ilk, root) -> str | None:
    """The branch `git worktree list` shows at root, as gh-resolve reads it (None: detached)."""
    entries = ilk.git(ilk.repo, "worktree", "list", "--porcelain").split("\n\n")
    entry = next(e for e in entries if Path(e.split("\n")[0].split(" ", 1)[1]).resolve() == Path(root).resolve())
    return next((ln.split(" ", 1)[1] for ln in entry.split("\n") if ln.startswith("branch ")), None)


def test_a_shipped_batch_frees_its_branch_and_a_rework_takes_it_back(ilk):
    # gh-resolve-84, Oct 21: a shipped batch's worktree kept its resolver branch checked out for
    # the 7-day keep window, so gh-resolve's conflict rework could not write that branch.
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    b = ilk.s.get("batches", bid)
    assert checked_out(ilk, b["execution_root"]) == f"refs/heads/{b['branch']}"
    assert ilk.drive_inprocess(bid) == "shipped"
    root = b["execution_root"]
    assert checked_out(ilk, root) is None
    assert ilk.git(root, "rev-parse", "HEAD") == ilk.git(root, "rev-parse", b["branch"])
    assert lease.get(ilk.s, root)["holder"] == "released"
    shipped = ilk.git(root, "rev-parse", b["branch"])
    ilk.script([{"write": "notes.txt", "text": "rework\n"}])
    again = ilk.submit(one_step(), branch=b["branch"])      # a rework on the same branch
    assert ilk.s.get("batches", again)["execution_root"] == root
    assert ilk.drive_inprocess(again) == "shipped"
    assert ilk.git(root, "rev-parse", f"{b['branch']}~1") == shipped   # it built on the branch
    assert checked_out(ilk, root) is None


def test_a_cancelled_batch_frees_its_branch_unless_another_batch_works_there(ilk):
    from ilk2 import batches
    ilk.make_repo()
    ilk.project()
    bid = ilk.submit(one_step())
    b = ilk.s.get("batches", bid)
    other = ilk.submit(one_step(), branch=b["branch"])      # shares the root, still queued
    assert ilk.s.get("batches", other)["execution_root"] == b["execution_root"]
    batches.cancel(ilk.s, bid, "stand-down")
    assert checked_out(ilk, b["execution_root"]) == f"refs/heads/{b['branch']}"
    batches.cancel(ilk.s, other, "stand-down")
    assert checked_out(ilk, b["execution_root"]) is None


def test_a_lease_on_a_branch_checked_out_elsewhere_is_refused_and_released(ilk, tmp_path, capsys):
    from ilk2 import cli
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    root = b["execution_root"]
    ilk.git(ilk.repo, "worktree", "add", "-q", str(tmp_path / "rework"), b["branch"])  # the consumer's
    row = lease.get(ilk.s, root)
    capsys.readouterr()
    rc = cli.main(["lease", "acquire", "--root", root, "--generation", str(row["generation"]),
                   "--expected-head", row["accepted_head"], "--owner", "gh-resolve", "--length", "600"])
    assert rc == 3 and "cannot check out" in capsys.readouterr().out
    assert lease.get(ilk.s, root)["holder"] == "released"
    assert checked_out(ilk, root) is None


def test_a_repo_that_cannot_take_a_commit_blocks_before_any_worker_runs(ilk):
    from ilk2 import batches
    # Oct 22: a consumer's test run inside a git hook rewrote kira-cloudflare's .git/config
    # (core.bare=true, user.email=test@example.com); 5 accepted commits authored "test".
    ilk.make_repo()
    ilk.project(commit_email="me@example.com")
    ilk.git(ilk.repo, "config", "user.name", "test")
    ilk.git(ilk.repo, "config", "user.email", "test@example.com")
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid).startswith("blocked")
    rows = ilk.s.open_human_rows(bid)
    assert [r["reason"] for r in rows] == ["commit-identity"]
    assert "test@example.com" in rows[0]["detail"] and "me@example.com" in rows[0]["detail"]
    assert ilk.stub_calls() == 0
    ilk.git(ilk.repo, "config", "--unset", "user.name")
    ilk.git(ilk.repo, "config", "--unset", "user.email")
    batches.resume(ilk.s, bid)
    assert ilk.drive_inprocess(bid).startswith("blocked")       # no user at all: ilk2@localhost
    assert "ilk2@localhost" in ilk.s.open_human_rows(bid)[0]["detail"]
    ilk.git(ilk.repo, "config", "user.name", "Me")
    ilk.git(ilk.repo, "config", "user.email", "me@example.com")
    batches.resume(ilk.s, bid)
    assert ilk.drive_inprocess(bid) == "shipped"
    b = ilk.s.get("batches", bid)
    assert ilk.git(b["execution_root"], "log", "-1", "--format=%ae", b["branch"]) == "me@example.com"


def test_a_bare_flag_set_during_an_attempt_commits_nothing(ilk):
    ilk.make_repo()
    ilk.project()                                               # no commit_email: the bare check only
    cfg = ilk.repo / ".git" / "config"
    corrupt = {"write": str(cfg), "text": cfg.read_text().replace("bare = false", "bare = true")}
    ilk.script([GOOD, corrupt])
    bid = ilk.submit(one_step())
    b = ilk.s.get("batches", bid)
    base = ilk.git(ilk.repo, "rev-parse", b["branch"])
    assert ilk.drive_inprocess(bid).startswith("blocked")
    assert [r["reason"] for r in ilk.s.open_human_rows(bid)] == ["commit-identity"]
    assert "core.bare" in ilk.s.open_human_rows(bid)[0]["detail"]
    assert ilk.git(ilk.repo, "--git-dir", str(ilk.repo / ".git"), "rev-parse", b["branch"]) == base


def test_a_configured_email_is_kept_when_user_name_is_unset(ilk):
    # chad-mbp, Oct 22: git has user.email and no user.name anywhere, and synthesises the name
    # from the host's gecos. identity() threw the email away, so a repository whose git identity
    # already carried the project's commit_email was blocked on ilk2@localhost. The synthesised
    # name is the host's to decide, so only the email is asserted.
    ilk.make_repo()
    ilk.git(ilk.repo, "config", "user.email", "me@example.com")
    ilk.project(commit_email="me@example.com")
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.drive_inprocess(bid) == "shipped"
    assert ilk.s.open_human_rows(bid) == []
    b = ilk.s.get("batches", bid)
    assert ilk.git(ilk.repo, "log", "-1", "--format=%ae", b["branch"]) == "me@example.com"
