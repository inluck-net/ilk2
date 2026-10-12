"""P1 step 3: PreToolUse hook for the worker, the second layer behind the sandbox (docs/02 §2.6).

Refuses `git commit`, `git push`, and file edits outside the worktree. The driver passes the
worktree root in ILK2_WORKTREE. Exit code 2 blocks the tool call and shows stderr to the model.
Each decision is appended to $ILK2_HOOK_LOG (one JSON line), so a run can count refusals.

Fails closed: Claude Code treats any exit code other than 2 as a non-blocking hook error,
so a crash here would let the call through (seen in step2-20261013-025927 with Python 3.9).
Runs under /usr/bin/python3 (3.9), so no 3.10+ syntax at runtime.
"""

from __future__ import annotations

import json
import os
import re
import sys

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GIT_WRITE = re.compile(r"\bgit\b[^|;&\n]*\b(commit|push)\b")


def inside(path: str, root: str) -> bool:
    real = os.path.realpath(path)
    root = os.path.realpath(root)
    return real == root or real.startswith(root + os.sep)


def decide(ev: dict, root: str) -> str | None:
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input", {}) or {}
    if tool == "Bash":
        if GIT_WRITE.search(inp.get("command", "")):
            return "git commit and git push are refused: the driver commits after its gates pass."
    elif tool in EDIT_TOOLS:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if path and not inside(path, root):
            return f"edits outside the worktree are refused: {path}"
    return None


def main() -> int:
    ev = json.load(sys.stdin)
    root = os.environ.get("ILK2_WORKTREE", "")
    reason = decide(ev, root) if root else "ILK2_WORKTREE is not set; refusing every tool call."
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
    except BaseException as e:  # noqa: BLE001 - fail closed on any hook error
        print(f"ilk2 hook error, refusing: {type(e).__name__}", file=sys.stderr)
        sys.exit(2)
