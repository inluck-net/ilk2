"""The V1 inventory (docs/07 §7.4.0): every installed artifact that resolves into V1, and only those."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from ilk2 import cli, inventory


def _link(p: Path, target: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(target)


def planted_home(tmp_path: Path) -> tuple[Path, Path]:
    """A home holding one of each V1 artifact kind, beside look-alikes that are not V1."""
    home = tmp_path / "home"
    clone = home / "Projects/github/inluck-net/ilk-skills"
    (home / ".ilk/releases/v1/skills/ilk-loop").mkdir(parents=True)
    (home / ".ilk/current").symlink_to(home / ".ilk/releases/v1")
    (clone / "hooks").mkdir(parents=True)
    (clone / "hooks/no-full-suite.sh").write_text("#!/bin/sh\n")
    (clone / "tools/claude-worker").mkdir(parents=True)
    (clone / "tools/claude-worker/claude-worker.sh").write_text("#!/bin/sh\n")
    (home / "elsewhere").mkdir()
    _link(home / ".claude/skills/ilk-loop", home / ".ilk/current/skills/ilk-loop")
    _link(home / ".codex/commands/ilk-plan.md", home / ".ilk/current/skills/ilk-loop")
    _link(home / ".claude/skills/mine", home / "elsewhere")                      # not V1
    _link(home / ".claude/hooks/no-full-suite.sh", clone / "hooks/no-full-suite.sh")
    (home / ".claude/settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"command": "~/.claude/hooks/no-full-suite.sh"}]}]}}))
    (home / ".claude/CLAUDE.md").write_text("x\n<!-- ilk:auto-plan:start -->\nroute\n")
    _link(home / "bin/claude-worker", clone / "tools/claude-worker/claude-worker.sh")
    _link(home / "bin/python", Path("/usr/bin/true"))                           # not V1
    agents = home / "Library/LaunchAgents"
    (agents / "disabled-by-ilk2").mkdir(parents=True)
    (agents / "disabled-by-ilk2/net.inluck.ilk.scheduler.plist").write_text("<plist/>")
    (agents / "net.example.pocket.plist").write_text("<string>/u/.ilk-data/pocket/agent.log</string>")
    (agents / "net.inluck.ilk2.supervisor.plist").write_text("<string>/u/.ilk2/data</string>")  # not V1
    (home / ".claude-worker").mkdir()
    repo = tmp_path / "consumer"
    (repo / "pkg/tests").mkdir(parents=True)
    (repo / "pkg/a.py").write_text("A = '~/.ilk-data'; B = '~/.ilk-data/x'\nC = 1\n")
    (repo / "pkg/tests/t.py").write_text("T = '.ilk-data'\n")                       # tests skipped
    g = ["git", "-C", str(repo)]
    subprocess.run(g + ["init", "-q"], check=True)
    subprocess.run(g + ["add", "."], check=True)
    subprocess.run(g + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c"], check=True)
    return home, repo


def _scan(home: Path, repo: Path) -> dict:
    cons = [inventory.Consumer("consumer", str(repo), "pkg", r"\.ilk-data")]
    return inventory.scan(home=home, consumers=cons, path_dirs=[str(home / "bin")])


def test_every_planted_v1_artifact_is_reported_and_nothing_else(tmp_path):
    home, repo = planted_home(tmp_path)
    out = _scan(home, repo)
    found = {(a["kind"], Path(a["path"]).name) for a in out["artifacts"]}
    assert found == {("skill", "ilk-loop"), ("command", "ilk-plan.md"), ("hook", "no-full-suite.sh"),
                     ("hook-registration", "no-full-suite.sh"), ("auto-plan-block", "CLAUDE.md"),
                     ("path-link", "claude-worker"), ("launchd", "net.inluck.ilk.scheduler.plist"),
                     ("launchd", "net.example.pocket.plist"), ("worker-home", ".claude-worker")}
    pocket = next(a for a in out["artifacts"] if a["path"].endswith("net.example.pocket.plist"))
    assert pocket["matched"] == ["content"]
    c = out["consumers"][0]
    assert (c["lines"], c["occurrences"], c["files"]) == (1, 2, 1) and c["commit"]
    assert out["data"] == [str(home / ".ilk")]
    assert out["clean"] is False


def test_the_allow_list_clears_artifacts_and_consumers(tmp_path):
    home, repo = planted_home(tmp_path)
    paths = [a["path"] for a in _scan(home, repo)["artifacts"]]
    (home / ".ilk2").mkdir()
    (home / ".ilk2/v1-allowlist.json").write_text(json.dumps(
        [{"path": p, "reason": "test"} for p in paths] + [{"path": "consumer", "reason": "inert"}]))
    out = _scan(home, repo)
    assert out["clean"] is True and out["open"] == {"artifacts": 0, "consumers": 0}
    assert all(a["allowed"] == "test" for a in out["artifacts"])


def test_a_clean_home_is_clean_and_the_command_exits_0(ilk, tmp_path, monkeypatch, capsys):
    home = tmp_path / "empty"
    home.mkdir()
    out = inventory.scan(home=home, consumers=[], path_dirs=[])
    assert out["clean"] is True and out["artifacts"] == [] and out["data"] == []
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", os.defpath)
    rc = cli.main(["v1", "inventory"])
    assert rc == 0 and json.loads(capsys.readouterr().out)["clean"] is True


def test_the_command_exits_1_while_anything_v1_remains(ilk, tmp_path, monkeypatch, capsys):
    home, _ = planted_home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(home / "bin"))
    assert cli.main(["v1", "inventory"]) == 1
    assert json.loads(capsys.readouterr().out)["open"]["artifacts"] > 0
