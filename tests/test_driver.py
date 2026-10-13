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
                    "WHERE st.plan_id = ?", f"{bid}.repair-1")
    assert rep["outcome"] == "accepted"
