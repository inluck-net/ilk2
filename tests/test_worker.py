import json
import os
import subprocess
from pathlib import Path

import pytest

from ilk2 import procs, worker
from ilk2.worker_hook import decide, full_suite

STUB = str(Path(__file__).with_name("stub_claude.py"))


@pytest.fixture
def env(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    files = worker.AttemptFiles(tmp_path / "attempt")
    tmpl = tmp_path / "template"
    tmpl.mkdir()
    (tmpl / "settings.json").write_text(json.dumps({"model": "stub-model", "env": {}}))
    cfg = worker.WorkerConfig(claude_bin=STUB, template=tmpl, max_turns=5)
    worker.prepare(cfg, files, wt, [["python3", "-m", "pytest", "tests"]])
    return cfg, files, wt


def script(files, **kw):
    (files.config / "stub.json").write_text(json.dumps(kw))


def calls(files):
    return [json.loads(l) for l in (files.config / "stub-calls.jsonl").read_text().splitlines()]


def test_dispatch_done_with_timings(env):
    cfg, files, wt = env
    script(files, actions=[{"write": "a.txt", "text": "hi"}])
    d = worker.dispatch(cfg, files, wt, "do it")
    assert d.wait(cap_s=30) == "done"
    s = d.summary()
    assert s["init_model"] == "stub-model" and s["session_id"]
    assert "first_edit_s" in s["timings"] and s["thinking_chars"] > 0
    assert (wt / "a.txt").read_text() == "hi"
    assert calls(files)[0]["pgid"] == d.pgid != os.getpgid(0)


def test_resume_keeps_session(env):
    cfg, files, wt = env
    script(files, actions=[], resume_actions=[{"append": "a.txt", "text": "2"}])
    d1 = worker.dispatch(cfg, files, wt, "start")
    d1.wait(30)
    d2 = worker.dispatch(cfg, files, wt, "fix", resume=d1.session_id)
    assert d2.wait(30) == "done" and d2.session_id == d1.session_id
    assert calls(files)[1]["resume"] == d1.session_id


def test_s2_hang_is_timeout_and_group_killed(env):
    cfg, files, wt = env
    script(files, actions=[{"bash_child": 300}, {"hang": True}])
    d = worker.dispatch(cfg, files, wt, "x")
    out = d.wait(cap_s=2, term_grace_s=1, kill_grace_s=2)
    assert out == "timeout"
    assert d.terminated["survivors"] == []
    assert len(d.terminated["groups"]) >= 2  # the worker's and its child's own group


def test_s17_sigterm_ignored_then_killed(env):
    cfg, files, wt = env
    script(files, actions=[{"ignore_term": True}, {"hang": True}])
    d = worker.dispatch(cfg, files, wt, "x")
    out = d.wait(cap_s=1, term_grace_s=1, kill_grace_s=2)
    assert out == "timeout" and d.terminated["signals"] == ["TERM", "KILL"]
    assert d.terminated["survivors"] == []
    assert d.terminated["seconds"] >= 1  # KILL came only after the TERM grace


def test_api_error_and_crash_outcomes(env):
    cfg, files, wt = env
    script(files, actions=[{"api_error": 429}])
    assert worker.dispatch(cfg, files, wt, "x").wait(30) == "api"
    script(files, actions=[{"exit": 3}])
    assert worker.dispatch(cfg, files, wt, "x").wait(30) == "crashed"


def test_orphan_swept_after_exit(env):
    cfg, files, wt = env
    script(files, actions=[{"bash_orphan": 300}])
    d = worker.dispatch(cfg, files, wt, "x")
    assert d.wait(30) == "done"
    assert d.timings.get("swept", 0) >= 1
    assert procs.processes_in(wt) == []


def test_sandbox_denies_write_outside(env, tmp_path):
    # Outside every allowed path: not the attempt dirs, not the per-user temp dir (where pytest's
    # tmp_path lives, and which the sandbox allows for mktemp).
    import shutil
    import tempfile
    cfg, files, wt = env
    out_dir = Path(tempfile.mkdtemp(dir="/private/tmp", prefix="ilk2-test-outside-"))
    try:
        script(files, actions=[{"write": str(out_dir / "outside.txt"), "text": "x"}])
        out = worker.dispatch(cfg, files, wt, "x").wait(30)
        assert out == "crashed"
        assert not (out_dir / "outside.txt").exists()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_prompt_not_in_argv_and_env_scrubbed(env, monkeypatch):
    cfg, files, wt = env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-pass")
    monkeypatch.setenv("ILK_SKILL_HOME", "/x")
    e = worker.worker_env(files, wt)
    assert "ANTHROPIC_API_KEY" not in e and "ILK_SKILL_HOME" not in e
    assert e["CLAUDE_CONFIG_DIR"] == str(files.config)
    settings = json.loads((files.config / "settings.json").read_text())
    assert "worker_hook.py" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert oct((files.config / "settings.json").stat().st_mode)[-3:] == "600"


# -- hook -------------------------------------------------------------------------------------

GATES = [["python3", "-m", "pytest", "tests/test_a.py", "tests/test_b.py", "-q"]]


@pytest.mark.parametrize("cmd,refused", [
    ("git commit -m x", True),
    ("git add . && git push", True),
    ("git status", False),
    ("python3 -m pytest", True),                                   # whole suite
    ("python3 -m pytest tests/test_a.py tests/test_b.py -q", True),  # a declared gate's suite
    ("python3 -m pytest tests/test_a.py", False),                  # targeted
    ("pytest tests/test_a.py::test_x", False),
    ("pytest -k foo", False),
    ("cd sub && pytest", True),
    ("ls -la", False),
])
def test_hook_bash(cmd, refused, tmp_path):
    ev = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    assert (decide(ev, str(tmp_path), GATES) is not None) is refused


def test_hook_edit_outside(tmp_path):
    inside = {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "a.py")}}
    outside = {"tool_name": "Write", "tool_input": {"file_path": "/etc/hosts"}}
    assert decide(inside, str(tmp_path), []) is None
    assert decide(outside, str(tmp_path), []) is not None


def test_hook_fails_closed(tmp_path):
    p = subprocess.run(["python3", "-I", str(worker.HOOK)], input="not json", text=True,
                       capture_output=True, env={"ILK2_WORKTREE": str(tmp_path), "PATH": "/usr/bin"})
    assert p.returncode == 2


def test_preload_includes_scope_tests_and_earlier_diff(tmp_path):
    (tmp_path / "calc.py").write_text("def add(a, b):\n    pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("def test_add():\n    pass\n")
    big = "\n".join(f"line {i}" for i in range(1000)) + "\ndef target_fn():\n    pass\n"
    (tmp_path / "big.py").write_text(big)
    out = worker.preload(tmp_path, ["calc.py", "big.py"], "fix `target_fn` in big.py",
                         [["python", "-m", "pytest", "tests/test_calc.py"]], "diff --git a b")
    assert "## calc.py (2 lines)" in out and "## tests/test_calc.py" in out
    assert "1001: def target_fn():" in out and "line 5\n" not in out  # windowed, not whole
    assert "Changes from this batch's earlier steps" in out


def test_full_suite_covers_collection_roots_and_scripts(tmp_path):
    # docs/06 F3 (ilk2-bd, confirmed at bfc7976): a gate that names no paths has a collection
    # root (pytest testpaths, the vitest config's include roots); naming that root, or a
    # package script that runs the runner under another name, is the whole suite.
    root = tmp_path
    (root / "pytest.ini").write_text("[pytest]\npythonpath = tools\ntestpaths = tests\n")
    cfg = "tests/convex-tests/vitest.config.ts"
    (root / "tests/convex-tests").mkdir(parents=True)
    (root / cfg).write_text('export default { test: { include: ["convex/__tests__/**/*.test.ts", '
                            '"tests/convex-tests/**/*.test.ts"] } }\n')
    (root / "package.json").write_text('{"scripts": {"check": "TZ=UTC vitest run -c ' + cfg + '", '
                                       '"lint": "eslint ."}}')
    vgate = [["vitest", "run", "-c", cfg]]
    pgate = [["python3", "-m", "pytest", "-q"]]
    r = str(root)
    assert full_suite(f"bunx vitest run -c {cfg} convex/__tests__", vgate, r)
    assert full_suite(f"bunx vitest run -c {cfg} convex/__tests__/", vgate, r)
    assert full_suite("python3 -m pytest tests/", pgate, r)
    assert full_suite("python3 -m pytest .", pgate, r)
    assert full_suite("bun run check", vgate, r)
    assert full_suite("cd sub && bun run check -- convex/__tests__", vgate, r)
    # targeted runs stay allowed
    assert not full_suite(f"bunx vitest run -c {cfg} convex/__tests__/foo.test.ts", vgate, r)
    assert not full_suite("python3 -m pytest tests/test_a.py", pgate, r)
    assert not full_suite("bun run lint", vgate, r)
    # a gate with explicit files: a directory that holds all of them is the whole suite
    egate = [["python3", "-m", "pytest", "tests/test_a.py", "tests/test_b.py"]]
    assert full_suite("python3 -m pytest tests", egate, r)
    assert not full_suite("python3 -m pytest tests/test_a.py", egate, r)


def test_attempt_records_peak_context_and_compactions(env):
    # docs/02 §2.6: one step is the context bound; P4's peak was 140k of 1M. Record it per
    # attempt so a session nearing the limit, or compacting, is visible (ilk2-f0, Oct 13).
    cfg, files, wt = env
    script(files, actions=[{"context": 20000}, {"context": 140224}, {"compact": True, "pre_tokens": 180000},
                           {"context": 30000}])
    d = worker.dispatch(cfg, files, wt, "x")
    assert d.wait(30) == "done"
    s = d.summary()
    # the turn just before a compaction can be larger than any turn the stream showed
    assert s["peak_context_tokens"] == 180000 and s["compactions"] == 1


def test_auto_compact_is_pinned_on_whatever_the_host_or_repo_says(env, monkeypatch):
    # docs/06 W2: --settings outranks a repo's .claude/settings.json, and DISABLE_AUTO_COMPACT
    # would override the setting, so it never reaches the worker.
    cfg, files, wt = env
    monkeypatch.setenv("DISABLE_AUTO_COMPACT", "1")
    monkeypatch.setenv("DISABLE_COMPACT", "1")
    script(files, actions=[])
    d = worker.dispatch(cfg, files, wt, "x")
    assert d.wait(30) == "done"
    c = calls(files)[0]
    assert json.loads(c["settings"]) == {"autoCompactEnabled": True}
    assert c["disable_auto_compact"] is None


def test_processes_in_reads_cwds_from_the_kernel_not_lsof(tmp_path, monkeypatch):
    # Oct 13: `lsof -d cwd -u <uid>` resolved every process's cwd, OneDrive's and other apps'
    # included, and macOS showed "python3.12 wants to access files managed by OneDrive" /
    # "data from other apps blocked". The kernel's proc_pidinfo gives the same paths without
    # touching the file system.
    import subprocess as sp_
    from ilk2 import procs
    real_run = sp_.run

    def no_lsof(cmd, *a, **kw):
        assert "lsof" not in str(cmd[0]), "processes_in must not run lsof"
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(procs.subprocess, "run", no_lsof)
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    p = sp_.Popen(["sleep", "30"], cwd=root / "sub")
    try:
        found = procs.processes_in(root)
        assert p.pid in [pid for pid, _ in found]
        assert all(pid != p.pid for pid, _ in procs.processes_in(tmp_path / "elsewhere"))
    finally:
        p.kill()
        p.wait()


def test_sandbox_hides_engine_data_but_not_the_worktree(env, tmp_path):
    # gh-resolve P5 batch, Oct 13: a worker read state.db and ilk2's run dirs for 21 minutes.
    cfg, files, wt = env
    data = tmp_path / "data"
    (data / "worktrees").mkdir(parents=True)
    (data / "state.db").write_text("project configs")
    inner = data / "worktrees" / "b-1"
    subprocess.run(["git", "init", "-q", str(inner)], check=True)
    (inner / "f.txt").write_text("ok")
    sb = tmp_path / "p.sb"
    sb.write_text(worker.sandbox_profile(inner, files, private=[os.path.realpath(data)]))

    def run(*argv):
        return subprocess.run(["/usr/bin/sandbox-exec", "-f", str(sb), *argv], capture_output=True,
                              text=True, cwd=inner)
    assert run("cat", str(data / "state.db")).returncode != 0
    assert run("cat", "f.txt").stdout == "ok"
    assert run("git", "status", "--short").returncode == 0  # lookups through data/ still work


def test_brief_states_scope_as_limit_or_advice():
    hard = worker.brief("t", "i", ["g"], scope=["a.py"], scope_enforced=True)
    soft = worker.brief("t", "i", ["g"], scope=["a.py"], scope_enforced=False)
    assert "may change" in hard and "advice" not in hard
    assert "advice" in soft and "no longer applies" in soft
    assert "may change" not in worker.brief("t", "i", ["g"])


def test_preload_puts_the_named_ranges_first_and_caps_identifier_windows(tmp_path):
    # gh-resolve, Oct 13: windows from 3 large files filled the 150k preload; the sub-plan's
    # file:line ranges and the gate's test files never made it in.
    big = "\n".join(f"x = 'engine' # {i}" for i in range(3000))
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py"):
        (tmp_path / name).write_text(big)
    (tmp_path / "far.py").write_text("\n".join(f"line_{i} = {i}" for i in range(1, 2001)))
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert True\n")
    text = worker.preload(tmp_path, ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py"],
                          "Read `far.py:1500-1504` and use `engine`.",
                          [["python3", "-m", "pytest", "test_x.py"]], "")
    assert "1502: line_1502 = 1502" in text and "1490: " in text  # padded by REF_PAD
    assert text.index("1502: line_1502") < text.index("## a.py")
    assert "def test_x" in text                                  # the gate's test file
    assert "[preload truncated]" not in text


def test_private_dirs_hide_data_not_v1_code(monkeypatch, tmp_path):
    # gh-resolve P5 batch, Oct 13: tests that load V1 skills via ~/.claude/skills -> ~/.ilk/current
    # failed in the worker's sandbox once ~/.ilk was hidden. ~/.ilk holds code releases only.
    monkeypatch.setenv("HOME", str(tmp_path))
    hidden = worker.private_dirs()
    assert os.path.realpath(tmp_path / ".ilk-data") in hidden
    assert os.path.realpath(tmp_path / ".ilk") not in hidden


def test_sandboxed_bare_mktemp_works(env, tmp_path):
    # macOS mktemp ignores TMPDIR; kira's consistency script's bare mktemp failed in V1's sandbox.
    cfg, files, wt = env
    sb = tmp_path / "m.sb"
    sb.write_text(worker.sandbox_profile(wt, files, private=[]))
    r = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(sb), "/bin/bash", "-c",
                        'f=$(mktemp) && echo ok && rm "$f"'], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_turns_count_main_loop_responses_once_and_their_model_time():
    # kira-cloudflare-44, Oct 16: steps sized against the worker's real pace; a session killed
    # at the cap emits no result with num_turns, so turns are counted from the stream.
    from ilk2.worker import Dispatch
    d = Dispatch(proc=None, log_path=None, worktree=None, t0=0.0)
    d._note({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "a"}]}}, 20.0)
    d._note({"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", "name": "Bash"}]}}, 21.0)
    d._note({"type": "user", "message": {"content": []}}, 25.0)                      # tool result
    d._note({"type": "assistant", "parent_tool_use_id": "t1",
             "message": {"id": "sub1", "content": []}}, 26.0)                        # a subagent
    d._note({"type": "assistant", "message": {"id": "m2", "content": []}}, 40.0)
    assert d.turns == 2 and d.model_s == 35.0                  # 20 s (start→m1) + 15 s (25→40)


def test_requests_run_to_their_last_event_and_split_long_ones_tool_time_and_the_first_edit():
    # docs/07 §7.4.1, evidence E5: a request's large tool_use block arrives minutes after its first
    # event. model_s stops at the first event; requests run to the last.
    from ilk2.worker import Dispatch
    d = Dispatch(proc=None, log_path=None, worktree=None, t0=0.0)
    d._note({"type": "assistant", "message": {"id": "m1", "content": [{"type": "thinking", "thinking": "x"}]}}, 10.0)
    d._note({"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", "name": "Read"}]}}, 12.0)
    d._note({"type": "user", "message": {"content": []}}, 15.0)                      # 3 s of tool
    d._note({"type": "assistant", "message": {"id": "m2", "content": [{"type": "thinking", "thinking": "y"}]}}, 100.0)
    d._note({"type": "assistant", "parent_tool_use_id": "t9",
             "message": {"id": "sub", "content": [{"type": "tool_use", "name": "Write"}]}}, 150.0)  # subagent
    d._note({"type": "assistant", "message": {"id": "m2", "content": [{"type": "tool_use", "name": "Write"}]}}, 400.0)
    d._note({"type": "user", "message": {"content": []}}, 402.0)                     # 2 s of tool
    r = d.requests()
    assert (r["n"], r["s"], r["long_n"], r["long_s"], r["longest_s"]) == (2, 397.0, 1, 385.0, 385.0)
    assert d.model_s == 95.0                            # unchanged: to each first event (10 + 85)
    assert d.tool_s == 5.0
    assert d.timings["first_edit_s"] == 150.0           # unchanged meaning: the first edit seen at all
    assert d.timings["first_edit_start_s"] == 15.0      # the main loop's first editing request
