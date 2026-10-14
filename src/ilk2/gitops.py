"""Git plumbing for the driver: worktrees, the acceptance commit, recovery (docs/02 §2.4.1).

The acceptance commit is built with plumbing so each phase is checkable:
- expected tree = `git write-tree` of the worktree through a temporary index;
- commit = `git commit-tree <tree> -p <parent>` with an `ilk2-attempt: <id>` trailer;
- the branch moves by `git update-ref <ref> <new> <parent>`, a compare-and-set.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

TRAILER = "ilk2-attempt"
IDENT = {"GIT_AUTHOR_NAME": "ilk2", "GIT_AUTHOR_EMAIL": "ilk2@localhost",
         "GIT_COMMITTER_NAME": "ilk2", "GIT_COMMITTER_EMAIL": "ilk2@localhost"}


class GitError(RuntimeError):
    pass


# ilk2's own git operations never run the consumer's hooks: lefthook's post-checkout `bun install`
# made `git worktree add` exit non-zero on kira-cloudflare (Oct 13), outside the pinned env.
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")


def git(root: Path, *args: str, env: dict | None = None, check: bool = True) -> str:
    e = {k: v for k, v in os.environ.items() if k != "GIT_INDEX_FILE"}
    e.update(env or {})
    p = subprocess.run(["git", *NO_HOOKS, "-C", str(root), *args], capture_output=True, text=True,
                       env=e)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.strip()[:500]}")
    return p.stdout.strip()


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD")


def rev(root: Path, ref: str) -> str | None:
    out = git(root, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False)
    return out or None


def branch_head(root: Path, branch: str) -> str | None:
    return rev(root, f"refs/heads/{branch}")


def add_worktree(repo: Path, path: Path, commit: str, branch: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch:
        git(repo, "worktree", "add", "-q", "-b", branch, str(path), commit)
    else:
        git(repo, "worktree", "add", "-q", "--detach", str(path), commit)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)


def worktree_tree(root: Path) -> str:
    """The tree of the worktree as it stands, untracked files included (ignored excluded)."""
    with tempfile.TemporaryDirectory() as d:
        idx = str(Path(d) / "index")
        env = {"GIT_INDEX_FILE": idx}
        git(root, "read-tree", "HEAD", env=env)
        git(root, "add", "-A", env=env)
        return git(root, "write-tree", env=env)


def changed_paths(root: Path) -> list[str]:
    """Paths that differ between HEAD and the worktree, untracked included."""
    # Raw output: git() strips, which would eat the leading space of the first " M path"
    # entry and with it the path's first character (the M1 run of Oct 13: "odo.py").
    out = subprocess.run(["git", "-C", str(root), "status", "--porcelain=v1", "-z",
                          "--untracked-files=all"], capture_output=True, text=True,
                         check=True).stdout
    paths = []
    for entry in out.split("\0"):
        if len(entry) > 3:
            paths.append(entry[3:])
    return sorted(set(paths))


def diff_text(root: Path, limit: int = 200_000) -> str:
    git(root, "add", "-A", "-N", check=False)
    return git(root, "diff", check=False)[:limit]


def commit_tree(root: Path, tree: str, parent: str, message: str, attempt_id: str) -> str:
    msg = f"{message}\n\n{TRAILER}: {attempt_id}\n"
    p = subprocess.run(["git", "-C", str(root), "commit-tree", tree, "-p", parent, "-F", "-"],
                       input=msg, capture_output=True, text=True, env={**os.environ, **IDENT})
    if p.returncode != 0:
        raise GitError(f"commit-tree: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def move_branch(root: Path, branch: str, new: str, expected_old: str) -> None:
    """Compare-and-set the branch ref; fails if it is not at expected_old."""
    git(root, "update-ref", f"refs/heads/{branch}", new, expected_old)


def sync_index_to_head(root: Path) -> None:
    """After moving the branch under a worktree that already has the content: reset the index
    to HEAD (mixed), leaving the files as they are."""
    git(root, "reset", "-q")


def find_trailer_commit(root: Path, branch: str, attempt_id: str, depth: int = 50) -> str | None:
    tip = branch_head(root, branch)
    if not tip:
        return None
    out = git(root, "log", f"-{depth}", "--format=%H%x00%B%x1e", tip, check=False)
    for rec in out.split("\x1e"):
        if "\x00" not in rec:
            continue
        sha, body = rec.strip().split("\x00", 1)
        if f"{TRAILER}: {attempt_id}" in body:
            return sha
    return None


def commit_tree_of(root: Path, sha: str) -> str:
    return git(root, "rev-parse", f"{sha}^{{tree}}")


def parent_of(root: Path, sha: str) -> str | None:
    return rev(root, f"{sha}^")


def reset_hard(root: Path, sha: str) -> None:
    git(root, "reset", "-q", "--hard", sha)
    git(root, "clean", "-q", "-fd")


def apply_commit(root: Path, sha: str) -> str:
    """Apply a commit's change to the worktree, uncommitted, beside the step's own uncommitted
    work. Returns 'present' (the reverse applies), 'applied' (with '; skipped absent: …' when the
    commit edits files this worktree does not have, e.g. tests added on main after the batch
    began), 'conflicted: <paths>' (conflict markers left for the worker), or 'failed: <reason>'.
    gh-resolve P6, Oct 14: a plain `git apply --3way` refused every file the worker had touched,
    since --3way implies --index, and refused the whole commit over one absent file."""
    patch = subprocess.run(["git", "-C", str(root), "show", "--format=", "--binary", sha],
                           capture_output=True, text=True).stdout
    if not patch.strip():
        return "failed: empty or unknown commit"
    absent = []
    for line in git(root, "show", "--format=", "--name-status", "--no-renames", sha,
                    check=False).splitlines():
        status, _, path = line.partition("\t")
        if status in ("M", "D") and not (root / path).exists():
            absent.append(path)
    excl = [f"--exclude={p}" for p in absent]
    rev = subprocess.run(["git", "-C", str(root), "apply", "--check", "-R", *excl, "-"],
                         input=patch, capture_output=True, text=True)
    if rev.returncode == 0:
        return "present"
    # --3way needs the index to match the files it touches: stage the step's work first. The
    # acceptance commit is built through a temporary index, so this staging changes nothing else.
    git(root, "add", "-A")
    p = subprocess.run(["git", "-C", str(root), "apply", "--3way", *excl, "-"], input=patch,
                       capture_output=True, text=True)
    if p.returncode == 0:
        return "applied" + (f"; skipped absent: {', '.join(absent)}" if absent else "")
    conflicted = git(root, "diff", "--name-only", "--diff-filter=U", check=False).split()
    if conflicted:
        return "conflicted: " + ", ".join(conflicted)
    return f"failed: {p.stderr.strip()[:300]}"


def is_ancestor(root: Path, a: str, b: str) -> bool:
    """True if commit a is an ancestor of (or equal to) commit b."""
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", a, b],
                          capture_output=True).returncode == 0
