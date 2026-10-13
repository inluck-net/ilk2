"""V1 coexistence and handover (docs/04 §4.2-4.3): one owner per repo, claim of queued masters."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from conftest import gate

from ilk2 import batches, supervisor, v1

GOOD = {"write": "calc.py", "text": "def add(a, b):\n    return a + b\n"}
SUB = """---
plan: add
---

# Sub-plan: add

## Steps

### Step 0 — implement add
```yaml
local_checks:
  - command: python -m pytest -q -p no:cacheprovider tests/test_calc.py
    timeout: 120
```
Make add() return the sum.
"""


def master(status: str) -> str:
    return (f"---\ntitle: t\nstatus: {status}\n---\n\n# MASTER\n\n"
            "| # | Sub-plan |\n|---|---|\n| 1 | [add](./2026-10-13-add.md) |\n")


@pytest.fixture
def v1data(ilk, tmp_path, monkeypatch):
    d = tmp_path / "ilk-data"
    monkeypatch.setenv("ILK2_V1_DATA", str(d))
    ilk.make_repo()
    key = "users-x-consumer-abc123"
    launcher = d / "projects" / key / "runtime" / "launcher"
    launcher.mkdir(parents=True)
    (launcher / "last-launch.json").write_text(json.dumps({"project_path": str(ilk.repo)}))
    (d / "projects" / key / "plans").mkdir()
    (d / "projects.json").write_text(json.dumps({"_comment": "x", "projects": [
        {"name": "consumer", "path": str(ilk.repo)}, {"name": "other", "path": "/elsewhere"}]}))
    return d, key


def test_project_add_refuses_a_repo_v1_still_lists(ilk, v1data):
    from ilk2 import cli
    assert cli.main(["project", "add", "proj", str(ilk.repo), "--config", "{}"]) == 2
    assert ilk.s.one("SELECT COUNT(*) n FROM projects")["n"] == 0


def test_take_over_refuses_while_v1_has_work(ilk, v1data):
    d, key = v1data
    (d / "projects" / key / "plans" / "MASTER-a.md").write_text(master("active"))
    with pytest.raises(RuntimeError, match="V1 still has work"):
        v1.take_over(ilk.repo, key)
    assert str(ilk.repo.resolve()) in v1.registry_roots()  # nothing changed


def test_take_over_makes_v1_skip_the_repo(ilk, v1data):
    from ilk2 import cli
    d, key = v1data
    (d / "projects" / key / "plans" / "MASTER-old.md").write_text(master("shipped"))
    assert cli.main(["project", "add", "proj", str(ilk.repo), "--take-over", "--config",
                     json.dumps({"path_prelude": [str(__import__("pathlib").Path(sys.executable).parent),
                                                  "/usr/bin", "/bin"]})]) == 0
    assert str(ilk.repo.resolve()) not in v1.registry_roots()
    assert "/elsewhere" in {e["path"] for e in json.loads((d / "projects.json").read_text())["projects"]}
    assert list(d.glob("projects.json.bak-ilk2-*"))  # reversible
    launcher = d / "projects" / key / "runtime" / "launcher"
    assert not (launcher / "last-launch.json").exists()
    assert (launcher / ("last-launch.json" + v1.HANDOVER)).exists()
    assert (d / "projects" / key / v1.MARKER).exists()
    assert batches.project_config(ilk.s, "proj").v1_key == key


def test_supervisor_claims_a_queued_v1_master_and_ships_it(ilk, v1data, monkeypatch):
    # /ilk-plan keeps writing to V1's plans dir; for an ilk2-owned project ilk2 claims it.
    monkeypatch.setenv("ILK2_TIME_SCALE", "60")
    d, key = v1data
    v1.take_over(ilk.repo, key)
    ilk.project(v1_key=key)
    plans_dir = d / "projects" / key / "plans"
    (plans_dir / "2026-10-13-add.md").write_text(SUB)
    (plans_dir / "MASTER-2026-10-13.md").write_text(master("queued"))
    (plans_dir / "MASTER-done.md").write_text(master("shipped"))
    ilk.script([GOOD])
    sp = supervisor.Supervisor(ilk.s, load_check=False)
    sp.start()
    sp.tick()
    assert not (plans_dir / "MASTER-2026-10-13.md").exists()  # out of V1's MASTER-*.md glob
    assert (plans_dir / ("MASTER-2026-10-13.md" + v1.CLAIMED)).exists()
    assert (plans_dir / "MASTER-done.md").exists()  # shipped masters are left alone
    b = ilk.s.one("SELECT * FROM batches WHERE consumer_ref = 'v1:MASTER-2026-10-13'")
    assert b is not None
    sp.tick()  # a second tick claims nothing new
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 1
    from test_stall import until
    assert until(sp, lambda: ilk.s.get("batches", b["id"])["status"] == "shipped", timeout=60)


def test_claim_waits_while_a_v1_runner_is_alive(ilk, v1data):
    d, key = v1data
    plans_dir = d / "projects" / key / "plans"
    (plans_dir / "MASTER-q.md").write_text(master("queued"))
    p = subprocess.Popen(["sleep", "30"])
    try:
        (d / "projects" / key / "runtime" / "launcher" / "running.pid").write_text(f"{p.pid}\n")
        assert v1.claim(key) == []
        assert (plans_dir / "MASTER-q.md").exists()
    finally:
        p.kill()
        p.wait()
    assert len(v1.claim(key)) == 1
