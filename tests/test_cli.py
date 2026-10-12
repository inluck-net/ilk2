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
