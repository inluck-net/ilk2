"""`gitops.apply_commit` against a worktree that holds a step's uncommitted work.

gh-resolve P6, Oct 14: the isolation fix 2cf192b2 was to be applied into a parked lane whose
worker had left 16 changed files. `git apply --3way` implies --index, so it refused every file
the worker had touched ("does not match index"), and the whole commit failed because it also
touched a file the batch's base did not have (a test added on main after the batch started).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ilk2 import gitops

ID = ["-c", "user.name=t", "-c", "user.email=t@t"]
LINES = "".join(f"line {i}\n" for i in range(1, 21))


def g(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *ID, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo at `base` with x.py, and a commit `fix` (on a side branch) that edits x.py's last
    line and also edits newer.py, a file added after `base`."""
    r = tmp_path / "r"
    r.mkdir()
    g(r, "init", "-q", "-b", "main")
    (r / "x.py").write_text(LINES)
    g(r, "add", "-A")
    g(r, "commit", "-qm", "base")
    g(r, "checkout", "-qb", "side")
    (r / "newer.py").write_text("a = 1\n")
    g(r, "add", "-A")
    g(r, "commit", "-qm", "newer file on main")
    (r / "x.py").write_text(LINES.replace("line 20\n", "line 20 fixed\n"))
    (r / "newer.py").write_text("a = 2\n")
    g(r, "commit", "-qam", "fix")
    fix = g(r, "rev-parse", "HEAD")
    g(r, "checkout", "-q", "main")
    return r, fix


def test_a_fix_applies_beside_uncommitted_work_in_the_same_file(tmp_path):
    r, fix = repo(tmp_path)
    (r / "x.py").write_text(LINES.replace("line 1\n", "line 1 worker\n"))   # the step's own work
    res = gitops.apply_commit(r, fix)
    assert res.startswith("applied"), res
    text = (r / "x.py").read_text()
    assert "line 1 worker" in text and "line 20 fixed" in text            # both kept
    assert "newer.py" in res and not (r / "newer.py").exists()           # skipped, and said so


def test_an_overlapping_edit_is_left_as_a_conflict_for_the_worker(tmp_path):
    r, fix = repo(tmp_path)
    (r / "x.py").write_text(LINES.replace("line 20\n", "line 20 worker\n"))
    res = gitops.apply_commit(r, fix)
    assert res == "conflicted: x.py", res
    text = (r / "x.py").read_text()
    assert "<<<<<<<" in text and "line 20 worker" in text and "line 20 fixed" in text


def test_a_fix_already_in_the_worktree_is_present(tmp_path):
    r, fix = repo(tmp_path)
    (r / "x.py").write_text(LINES.replace("line 20\n", "line 20 fixed\n"))
    assert gitops.apply_commit(r, fix) == "present"
