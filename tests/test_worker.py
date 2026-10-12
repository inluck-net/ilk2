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
    cfg, files, wt = env
    script(files, actions=[{"write": str(tmp_path / "outside.txt"), "text": "x"}])
    out = worker.dispatch(cfg, files, wt, "x").wait(30)
    assert out == "crashed"
    assert not (tmp_path / "outside.txt").exists()


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
