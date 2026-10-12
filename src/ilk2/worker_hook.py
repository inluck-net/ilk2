"""PreToolUse hook for workers: the second layer behind the sandbox (docs/02 §2.6).

Refuses `git commit` / `git push`, edits outside the worktree, and (Q16) a test command that
runs the whole suite of a declared gate. Fails closed: Claude Code treats any exit code other
than 2 as non-blocking, so every internal error exits 2 (P1 finding 7).

Inputs from the environment the driver sets: ILK2_WORKTREE, ILK2_GATES_FILE (JSON list of
gate commands), ILK2_HOOK_LOG (optional JSON-lines log).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GIT_WRITE = re.compile(r"\bgit\b[^|;&\n]*\b(commit|push)\b")
RUNNERS = re.compile(r"(^|\s|/)(pytest|vitest)(\s|$)|-m\s+pytest|\b(npm|bun|pnpm|yarn)\s+(run\s+)?test\b")


def inside(path: str, root: str) -> bool:
    real, root = os.path.realpath(path), os.path.realpath(root)
    return real == root or real.startswith(root + os.sep)


def _paths(argv: list[str]) -> set[str]:
    out, skip = set(), False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in ("-k", "-m", "-p", "-c", "--deselect", "-n", "--timeout", "-o"):
            skip = True
            continue
        if not a.startswith("-") and ("/" in a or a.endswith((".py", ".ts", ".js"))) \
                and not a.endswith(("python", "python3")):
            out.add(a.rstrip("/"))
    return out


def full_suite(command: str, gates: list[list[str]]) -> bool:
    """A test command runs a gate's whole suite if it invokes a runner with no test paths, or
    with every path of a declared gate. Targeted runs (a subset, -k, ::node ids) are allowed.
    Judgment call: wrong if workers need a declared gate's full file list to verify a change."""
    for part in re.split(r"&&|\|\||;|\|", command):
        if not RUNNERS.search(part):
            continue
        try:
            argv = shlex.split(part)
        except ValueError:
            argv = part.split()
        if "-k" in argv or any("::" in a for a in argv):
            continue
        mine = _paths(argv)
        if not mine:
            return True
        for g in gates:
            theirs = _paths(g)
            if theirs and theirs <= mine:
                return True
    return False


def decide(ev: dict, root: str, gates: list[list[str]]) -> str | None:
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input") or {}
    if tool == "Bash":
        cmd = inp.get("command", "")
        if GIT_WRITE.search(cmd):
            return "git commit and git push are refused: the driver commits after its gates pass."
        if full_suite(cmd, gates):
            return ("Running a declared gate's whole suite is refused: the driver runs it after "
                    "your attempt. Run only the tests for the files you changed.")
    elif tool in EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if path and not inside(path, root):
            return f"Edits outside the worktree are refused: {path}"
    return None


def main() -> int:
    ev = json.load(sys.stdin)
    root = os.environ.get("ILK2_WORKTREE", "")
    gates: list[list[str]] = []
    gf = os.environ.get("ILK2_GATES_FILE")
    if gf and os.path.exists(gf):
        with open(gf) as f:
            gates = json.load(f)
    reason = decide(ev, root, gates) if root else "ILK2_WORKTREE is not set; refusing."
    log = os.environ.get("ILK2_HOOK_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"tool": ev.get("tool_name"), "refused": reason is not None,
                                "reason": reason}) + "\n")
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 - fail closed
        print(f"ilk2 hook error, refusing: {type(e).__name__}", file=sys.stderr)
        sys.exit(2)
