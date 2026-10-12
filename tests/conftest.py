"""Shared fixtures: a private ILK2_HOME, a throwaway consumer repo, and the stub worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ilk2 import batches, clock, paths, plans
from ilk2.store import Store, new_id

STUB = str(Path(__file__).with_name("stub_claude.py"))
PY_BIN = str(Path(sys.executable).parent)

CALC = "def add(a, b):\n    raise NotImplementedError\n"
TEST_CALC = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


class Env:
    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.home = tmp / "ilk2home"
        monkeypatch.setenv("ILK2_HOME", str(self.home))
        monkeypatch.setenv("ILK2_TIME_SCALE", os.environ.get("ILK2_TIME_SCALE", "600"))
        self.script_path = tmp / "stub-script.json"
        monkeypatch.setenv("STUB_SCRIPT", str(self.script_path))
        (self.home / "worker-template").mkdir(parents=True)
        (self.home / "worker-template" / "settings.json").write_text(
            json.dumps({"model": "stub-model", "env": {}}))
        paths.ensure()
        self.s = Store(paths.db())
        self.repo = tmp / "consumer"

    def make_repo(self, files: dict[str, str] | None = None) -> Path:
        files = files or {"calc.py": CALC, "tests/test_calc.py": TEST_CALC,
                          "tests/conftest.py": "import sys, os\n"
                          "sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"}
        self.repo.mkdir()
        for rel, body in files.items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        g = ["git", "-C", str(self.repo)]
        subprocess.run(g + ["init", "-q", "-b", "main"], check=True)
        subprocess.run(g + ["add", "."], check=True)
        subprocess.run(g + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m",
                            "seed"], check=True)
        return self.repo

    def project(self, **cfg) -> str:
        cfg.setdefault("sandbox_allow", [str(self.tmp)])  # the stub's call counter lives here
        c = batches.ProjectConfig(claude_bin=STUB, path_prelude=[PY_BIN, "/usr/bin", "/bin"],
                                  **cfg)
        batches.project_add(self.s, "proj", self.repo, c)
        return "proj"

    def script(self, *calls: list[dict]) -> None:
        self.script_path.write_text(json.dumps({"calls": [{"actions": a} for a in calls]}))
        log = Path(str(self.script_path) + ".calls")
        if log.exists():
            log.unlink()

    def stub_calls(self) -> int:
        log = Path(str(self.script_path) + ".calls")
        return sum(1 for _ in log.open()) if log.exists() else 0

    def submit(self, spec: dict, **kw) -> str:
        return batches.submit(self.s, "proj", plans.from_dict(spec), **kw)

    def new_run(self, bid: str) -> str:
        b = self.s.get("batches", bid)
        if b["status"] == "queued":
            self.s.transition("batches", bid, "active", actor="test", reason="admit")
        rid = new_id("r")
        self.s.insert("runs", {"id": rid, "batch_id": bid, "status": "starting",
                               "created_at": clock.wall(), "spawned_mono": clock.mono()})
        return rid

    def drive_inprocess(self, bid: str) -> str:
        from ilk2.driver import Driver
        return Driver(self.s, self.new_run(bid)).run()

    def drive_subprocess(self, bid: str, faults: str = "") -> int:
        rid = self.new_run(bid)
        env = {**os.environ, "ILK2_FAULTS": faults}
        p = subprocess.run([sys.executable, "-c",
                            f"import sys; from ilk2.driver import main; sys.exit(main({rid!r}))"],
                           env=env, capture_output=True, text=True)
        return p.returncode

    def git(self, root: Path, *args) -> str:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              check=True).stdout.strip()


@pytest.fixture
def ilk(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.s.close()


def gate(*paths_, name="unit"):
    return {"name": name, "adapter": "pytest",
            "command": ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths_]}


def one_step(instructions="implement add", gates_=None, **step):
    step.setdefault("must_pass", ["tests/test_calc.py::test_add"])
    return {"plans": [{"slug": "p", "steps": [{"title": "add", "instructions": instructions,
                                               "gates": gates_ or [gate("tests/test_calc.py")],
                                               **step}]}]}
