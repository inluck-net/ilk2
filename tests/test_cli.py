import subprocess
import sys

import pytest

from ilk2 import __version__
from ilk2.cli import main


def test_version_flag_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"ilk2 {__version__}"


def test_module_runs_as_a_script():
    result = subprocess.run(
        [sys.executable, "-m", "ilk2.cli", "--version"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"ilk2 {__version__}"


def test_project_show_prints_settings_with_env_values_redacted(ilk, capsys):
    # gh-resolve-65, Oct 18: the planner needs a project's verify_command; there was no command,
    # so it was read from the store. Environment values can be credentials: keys only.
    import json as _json
    from ilk2 import cli
    ilk.make_repo()
    ilk.project(verify_command=["python", "-m", "pytest"], env_set={"API_TOKEN": "s3cret"},
                setup={"command": ["true"], "env": {"PW": "hunter2"}})
    capsys.readouterr()
    assert cli.main(["project", "show", "proj"]) == 0
    out = capsys.readouterr().out
    d = _json.loads(out)
    assert d["config"]["verify_command"] == ["python", "-m", "pytest"]
    assert d["config"]["env_set"] == {"API_TOKEN": "<set>"} and d["config"]["setup"]["env"] == {"PW": "<set>"}
    assert "s3cret" not in out and "hunter2" not in out
    assert cli.main(["project", "show", "nope"]) == 2
