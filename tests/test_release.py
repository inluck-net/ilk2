import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from conftest import one_step

from ilk2 import release, supervisor

REPO = Path(__file__).resolve().parents[1]
GOOD = {"write": "calc.py", "text": "def add(a, b):\n    return a + b\n"}


def test_drain_admits_nothing_then_exits(ilk, monkeypatch):
    monkeypatch.setenv("ILK2_TIME_SCALE", "60")
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    with ilk.s.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES ('draining', '1')")
    sp = supervisor.Supervisor(ilk.s, load_check=False)
    sp.start()
    sp.tick()
    assert ilk.s.get("batches", bid)["status"] == "queued"  # nothing admitted while draining
    assert sp.exit_requested  # no live runs: exit so launchd restarts from the new release
    assert ilk.s.one("SELECT value FROM meta WHERE key = 'draining'")["value"] == "0"


def test_driver_stops_at_step_boundary_when_draining(ilk):
    ilk.make_repo()
    ilk.project()
    ilk.script([GOOD])
    bid = ilk.submit(one_step())
    with ilk.s.tx() as c:
        c.execute("INSERT INTO meta(key, value) VALUES ('draining', '1')")
    assert ilk.drive_inprocess(bid) == "drained"
    assert ilk.stub_calls() == 0


def test_build_and_install_without_launchd(ilk, monkeypatch, tmp_path):
    monkeypatch.setattr(release, "AGENTS", tmp_path / "agents")
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--", "src"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:  # a release is built from a commit; skip while src/ has uncommitted edits
        import pytest
        pytest.skip("src/ has uncommitted changes")
    rel = release.build(REPO)
    man = json.loads((rel / "manifest.json").read_text())
    assert man["version"] == rel.name and man["wheel_sha256"]
    out = subprocess.run([str(rel / "venv" / "bin" / "python"), "-m", "ilk2", "--version"],
                         capture_output=True, text=True)
    assert out.stdout.strip().startswith("ilk2 ")
    # the console script too: its shebang named the temporary build dir (gh-resolve-e1, Oct 13)
    cli = subprocess.run([str(rel / "venv" / "bin" / "ilk2"), "--version"], capture_output=True, text=True)
    assert cli.returncode == 0 and cli.stdout.strip().startswith("ilk2 "), cli.stderr
    info = release.install(rel.name, load=False)
    assert os.path.realpath(release.current()) == str(rel)
    sup = plistlib.loads((tmp_path / "agents" / f"{release.SUPERVISOR}.plist").read_bytes())
    assert sup["KeepAlive"] is True and sup["ProgramArguments"][1:] == ["-m", "ilk2", "supervise"]
    assert sup["ProgramArguments"][0].startswith(str(release.current()))
    hl = plistlib.loads((tmp_path / "agents" / f"{release.HEALTH}.plist").read_bytes())
    assert hl["StartInterval"] == 60 and "KeepAlive" not in hl
    assert info["current"] == str(rel)


def test_cli_contract_smoke(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    spec = tmp_path / "batch.json"
    spec.write_text(json.dumps(one_step()))
    env = {**os.environ}
    run = lambda *a: subprocess.run([sys.executable, "-m", "ilk2", *a], capture_output=True,
                                    text=True, env=env)
    r = run("submit", str(spec), "--project", "proj", "--consumer-ref", "issue-9", "--json")
    assert r.returncode == 0, r.stdout + r.stderr
    bid = json.loads(r.stdout)["batch"]
    st = json.loads(run("status", "--batch", bid, "--json").stdout)
    assert st["api_version"] == 1
    b = st["batches"][0]
    assert b["status"] == "queued" and b["consumer_ref"] == "issue-9"
    assert b["root_lease"]["holder"] == "driver"
    ev = json.loads(run("events", "--since", "0", "--json").stdout)
    assert ev["events"][0]["kind"] == "submitted" and ev["events"][0]["consumer_ref"] == "issue-9"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"plans": [{"slug": "p", "steps": [{"instructions": "x", "gates": []}]}]}))
    r = run("submit", str(bad), "--project", "proj")
    assert r.returncode == 2 and "declares no gate" in r.stdout
    assert run("park", bid).returncode == 0
    assert json.loads(run("status", "--batch", bid, "--json").stdout)["batches"][0]["status"] == "parked"
    assert run("cancel", bid, "--reason", "stand-down").returncode == 0


def test_relocate_venv_rewrites_scripts_that_name_the_build_dir(tmp_path):
    old, new = tmp_path / ".v.tmp", tmp_path / "v"
    (old / "venv" / "bin").mkdir(parents=True)
    (old / "venv" / "bin" / "ilk2").write_text(f"#!{old}/venv/bin/python\nimport sys\n")
    (old / "venv" / "bin" / "activate").write_text(f"VIRTUAL_ENV='{old}/venv'\n")
    (old / "venv" / "bin" / "python").symlink_to("/usr/bin/python3")
    old.rename(new)
    release.relocate_venv(new / "venv", old, new)
    assert (new / "venv" / "bin" / "ilk2").read_text().startswith(f"#!{new}/venv/bin/python")
    assert str(old) not in (new / "venv" / "bin" / "activate").read_text()
    assert (new / "venv" / "bin" / "python").is_symlink()  # symlinks are left alone


def test_upgrade_drains_only_when_the_supervisors_code_changed(tmp_path):
    # Oct 14: a drain admits nothing until every live run ends an attempt; driver-only releases
    # paid that for nothing. Drivers spawn from `current` and take new code at their next run.
    import hashlib
    from ilk2 import release
    pkg = tmp_path / "ilk2"
    pkg.mkdir()
    (pkg / "supervisor.py").write_text("A = 1\n")
    (pkg / "driver.py").write_text("B = 1\n")
    rec = {"supervisor.py": hashlib.sha256(b"A = 1\n").hexdigest()}
    assert release.needs_drain(None, pkg)                   # unknown: drain
    assert not release.needs_drain(rec, pkg)
    (pkg / "driver.py").write_text("B = 2\n")               # not loaded by the supervisor
    assert not release.needs_drain(rec, pkg)
    (pkg / "supervisor.py").write_text("A = 2\n")
    assert release.needs_drain(rec, pkg)


def test_supervisor_records_the_code_it_loaded(ilk):
    import json
    from ilk2 import supervisor
    sp = supervisor.Supervisor(ilk.s, load_check=False)
    sp.start()
    code = json.loads(ilk.s.one("SELECT value FROM meta WHERE key = 'supervisor_code'")["value"])
    assert "supervisor.py" in code and "store.py" in code
