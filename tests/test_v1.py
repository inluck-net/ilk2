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
    # The plan documents travel with the batch: the worker reads them from a named copy, since
    # its sandbox cannot read V1's plans dir (gh-resolve P5 batch, Oct 13).
    docs = json.loads(b["snapshot"])["documents"]
    assert set(docs) == {"MASTER-2026-10-13.md", "2026-10-13-add.md"}
    prompt = ilk.stub_prompts()[0]
    assert "Plan documents" in prompt and "/plan/MASTER-2026-10-13.md`" in prompt


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


# -- shadow projects (docs/04 §4.2, P5) ---------------------------------------------------------

def _clone(ilk, tmp_path, push_url):
    c = tmp_path / "shadow"
    subprocess.run(["git", "clone", "-q", str(ilk.repo), str(c)], check=True)
    subprocess.run(["git", "-C", str(c), "remote", "set-url", "--push", "origin", push_url], check=True)
    return c


def test_shadow_project_must_be_a_separate_clone_that_cannot_push(ilk, tmp_path):
    from ilk2 import cli
    ilk.make_repo()
    assert cli.main(["project", "add", "sh", str(ilk.repo), "--shadow-of", str(ilk.repo)]) == 2
    pushes = _clone(ilk, tmp_path, "https://github.com/x/y.git")
    assert cli.main(["project", "add", "sh", str(pushes), "--shadow-of", str(ilk.repo)]) == 2
    subprocess.run(["git", "-C", str(pushes), "remote", "set-url", "--push", "origin",
                    "DISABLED-ilk2-shadow"], check=True)
    assert cli.main(["project", "add", "sh", str(pushes), "--shadow-of", str(ilk.repo)]) == 0
    assert batches.project_config(ilk.s, "sh").shadow_of == str(ilk.repo.resolve())


def test_shadow_root_is_never_leased_and_its_events_say_shadow(ilk, tmp_path):
    from conftest import one_step
    from ilk2 import lease
    from ilk2.store import Conflict
    ilk.make_repo()
    c = _clone(ilk, tmp_path, "DISABLED-ilk2-shadow")
    ilk.repo = c
    ilk.project(shadow_of=str(tmp_path / "consumer"))
    bid = ilk.submit(one_step())
    root = ilk.s.get("batches", bid)["execution_root"]
    head = ilk.git(root, "rev-parse", "HEAD")
    g = lease.driver_release(ilk.s, root, head)
    with pytest.raises(Conflict, match="shadow"):
        lease.consumer_acquire(ilk.s, root, g, head, "gh-resolve", head, 60)
    assert all(e["payload"].get("shadow") is True for e in ilk.s.events_since(0, bid))


def test_take_over_lists_non_runnable_masters_and_refuses_only_runnable_ones(ilk, v1data):
    # ilk-skills on Oct 13: 3 drafts never released, 2 paused, 1 blocked. V1 can run none of
    # them; they must not block the handover. A queued or active master still does.
    d, key = v1data
    plans_dir = d / "projects" / key / "plans"
    for name, st in (("MASTER-d.md", "draft"), ("MASTER-p.md", "paused"), ("MASTER-b.md", "blocked")):
        (plans_dir / name).write_text(master(st))
    done = v1.take_over(ilk.repo, key)
    assert sorted(done["left_in_v1"]) == ["MASTER-b.md: blocked", "MASTER-d.md: draft", "MASTER-p.md: paused"]
    assert (plans_dir / "MASTER-d.md").exists()  # untouched
    # a draft released later (status queued) is claimed by ilk2
    (plans_dir / "MASTER-d.md").write_text(master("queued"))
    assert [p.name for p in v1.claim(key)] == ["MASTER-d.md" + v1.CLAIMED]


def test_take_over_refuses_a_runnable_master(ilk, v1data):
    d, key = v1data
    (d / "projects" / key / "plans" / "MASTER-q.md").write_text(master("queued"))
    with pytest.raises(RuntimeError, match="V1 still has work"):
        v1.take_over(ilk.repo, key)


# -- the per-host switch-off (docs/04 §4.9, P7) ------------------------------------------------

@pytest.fixture
def agents(tmp_path, monkeypatch):
    a = tmp_path / "LaunchAgents"
    a.mkdir()
    (a / f"{v1.SCHEDULER}.plist").write_text("<plist/>")
    log = tmp_path / "launchctl.log"
    fake = tmp_path / "launchctl"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    fake.chmod(0o755)
    monkeypatch.setenv("ILK2_LAUNCH_AGENTS", str(a))
    monkeypatch.setenv("ILK2_LAUNCHCTL", str(fake))
    return a, log


def test_switch_off_is_refused_while_anything_still_needs_v1(ilk, v1data, agents):
    a, log = agents
    d, key = v1data
    (d / "projects" / key / "plans" / "MASTER-q.md").write_text(master("queued"))
    out = v1.switch_off(set(), apply=True)
    assert not out["applied"] and out["plist"] and not log.exists()
    assert any("not owned by ilk2" in b for b in out["blockers"])
    assert any("runnable master" in b for b in out["blockers"])


def test_switch_off_disables_and_switch_on_restores(ilk, v1data, agents):
    a, log = agents
    d, key = v1data
    (d / "projects.json").write_text(json.dumps({"projects": []}))  # everything moved to ilk2
    dry = v1.switch_off(set(), apply=False)
    assert dry["blockers"] == [] and not dry["applied"] and dry["plist"]
    out = v1.switch_off(set(), apply=True)
    assert out["applied"] and not out["plist"] and out["disabled_plist"]  # moved, never deleted
    assert "bootout" in log.read_text()
    back = v1.switch_on(apply=True)
    assert back["applied"] and back["plist"] and not back["disabled_plist"]
    assert "bootstrap" in log.read_text()


def test_v1_status_cli_lists_what_still_needs_v1(ilk, v1data, agents, capsys):
    from ilk2 import cli
    assert cli.main(["v1", "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert any("not owned by ilk2" in b for b in out["needs_v1"]) and out["plist"]
    assert cli.main(["v1", "off", "--apply"]) == 2  # refused: the registry still lists repos


def test_a_shadow_projects_remote_refs_are_refreshed_and_the_brief_says_when(ilk, tmp_path):
    # kira-cloudflare-a2, Oct 14: kira-shadow's origin/main was July's; a worker's fetch cannot
    # write the shared .git from its sandbox, and -q hid the failure: wrong "on main?" verdicts.
    from conftest import one_step
    origin = ilk.make_repo()
    c = _clone(ilk, tmp_path, "DISABLED-ilk2-shadow")
    branch = ilk.git(origin, "rev-parse", "--abbrev-ref", "HEAD")
    ilk.git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
            "-m", "landed after the clone")
    new = ilk.git(origin, "rev-parse", "HEAD")
    assert ilk.git(c, "rev-parse", f"origin/{branch}") != new
    ilk.repo = c
    ilk.project(shadow_of=str(tmp_path / "consumer"))
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    assert ilk.git(c, "rev-parse", f"origin/{branch}") == new  # refreshed at submit
    assert ilk.drive_inprocess(bid) == "shipped"
    assert "fetched by the driver at" in ilk.stub_prompts()[0]


def test_project_remove_gives_the_repo_back_to_v1(ilk, v1data):
    # The rollback of a take-over (kira-live on rezmac, Oct 14): V1 resolves the repo again,
    # and registry changes made since the take-over are kept.
    from ilk2 import cli
    d, key = v1data
    assert cli.main(["project", "add", "proj", str(ilk.repo), "--take-over", "--config",
                     json.dumps({"path_prelude": [str(__import__("pathlib").Path(sys.executable).parent),
                                                  "/usr/bin", "/bin"]})]) == 0
    assert str(ilk.repo.resolve()) not in v1.registry_roots()
    reg = d / "projects.json"
    data = json.loads(reg.read_text())
    data["projects"].append({"name": "added-later", "path": "/later"})   # a change after the take-over
    reg.write_text(json.dumps(data))
    from conftest import one_step
    bid = ilk.submit(one_step())
    assert cli.main(["project", "remove", "proj", "--give-back"]) == 2   # an unfinished batch
    from ilk2 import batches
    batches.cancel(ilk.s, bid, "human")
    assert cli.main(["project", "remove", "proj", "--give-back"]) == 0
    roots = v1.registry_roots()
    assert str(ilk.repo.resolve()) in roots and "/later" in roots
    launcher = d / "projects" / key / "runtime" / "launcher"
    assert (launcher / "last-launch.json").exists() and not (d / "projects" / key / v1.MARKER).exists()
    assert batches.project_config(ilk.s, "proj").retired               # kept, with its history
    with pytest.raises(batches.SubmitRejected, match="retired"):
        ilk.submit(one_step())


def test_two_rejected_masters_open_two_needs_human_rows(ilk, v1data):
    # Oct 14: 14a's plan-rejected merged into 14b's open row (both batch-less): 14a sat claimed
    # with nothing telling anyone.
    d, key = v1data
    v1.take_over(ilk.repo, key)
    ilk.project(v1_key=key)
    plans_dir = d / "projects" / key / "plans"
    bad = SUB.replace("python -m pytest -q -p no:cacheprovider tests/test_calc.py",
                      "python3 /Users/x/.ilk/current/skills/ilk-loop/scripts/verification_record.py --project .")
    bad = bad.replace("plan: add", "plan: add-batch-verify")
    (plans_dir / "2026-10-13-add.md").write_text(bad)
    (plans_dir / "MASTER-a.md").write_text(master("queued"))
    (plans_dir / "MASTER-b.md").write_text(master("queued"))
    sp = supervisor.Supervisor(ilk.s, load_check=False)
    sp.claim_v1()
    rows = ilk.s.all("SELECT consumer_ref FROM needs_human WHERE reason = 'plan-rejected' AND state = 'open'")
    assert sorted(r["consumer_ref"] for r in rows) == ["v1:MASTER-a", "v1:MASTER-b"]
