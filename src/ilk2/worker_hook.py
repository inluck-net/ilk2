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


PMS = ("npm", "bun", "pnpm", "yarn")
GLOB = re.compile(r"[*?[{]")


def _norm(p: str) -> str:
    p = os.path.normpath(p.rstrip("/"))
    return "." if p in ("", ".") else p


def _covers(outer: str, inner: str) -> bool:
    """outer is inner or one of its ancestor directories."""
    outer, inner = _norm(outer), _norm(inner)
    return outer == "." or inner == outer or inner.startswith(outer + "/")


def _read(root: str, rel: str) -> str | None:
    try:
        with open(os.path.join(root, rel)) as f:
            return f.read()
    except OSError:
        return None


def _script(argv: list[str], root: str) -> str | None:
    """`bun run x` / `npm run x` / `npm test` / `yarn x` -> the package.json entry (+ extra args).
    Matched by what the script runs, not by its name (docs/06 F3)."""
    if not argv or os.path.basename(argv[0]) not in PMS:
        return None
    rest = argv[1:]
    if rest[:1] == ["run"] and len(rest) > 1:
        name, extra = rest[1], rest[2:]
    elif rest[:1] == ["test"]:
        name, extra = "test", rest[1:]
    elif os.path.basename(argv[0]) == "yarn" and rest:
        name, extra = rest[0], rest[1:]
    else:
        return None
    pkg = _read(root, "package.json")
    try:
        entry = (json.loads(pkg).get("scripts") or {}).get(name) if pkg else None
    except ValueError:
        entry = None
    if not entry:
        return None
    if extra[:1] == ["--"]:
        extra = extra[1:]
    return entry + ("" if not extra else " " + " ".join(shlex.quote(a) for a in extra))


def _strip_assignments(argv: list[str]) -> list[str]:
    while argv and "=" in argv[0] and not argv[0].startswith("-"):
        argv = argv[1:]
    return argv


def _collection_roots(gate: list[str], root: str) -> list[str]:
    """Where a gate that names no paths collects from: pytest's testpaths, or the include
    roots of the vitest config it uses; the project root when neither says."""
    if any(os.path.basename(a) == "vitest" for a in gate[:2]):
        cfg = None
        for i, a in enumerate(gate):
            if a in ("-c", "--config") and i + 1 < len(gate):
                cfg = gate[i + 1]
            elif a.startswith("--config="):
                cfg = a.split("=", 1)[1]
        names = [cfg] if cfg else [f"vitest.config.{e}" for e in ("ts", "mts", "js", "mjs")]
        for n in names:
            text = _read(root, n)
            m = re.search(r"include\s*:\s*\[([^\]]*)\]", text or "")
            if m:
                roots = []
                for pat in re.findall(r"[\"']([^\"']+)[\"']", m.group(1)):
                    parts = []
                    for seg in pat.split("/"):
                        if GLOB.search(seg):
                            break
                        parts.append(seg)
                    roots.append("/".join(parts) or ".")
                return roots or ["."]
        return ["."]
    for n, section in (("pytest.ini", "pytest"), ("pyproject.toml", "tool.pytest.ini_options"),
                       ("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        text = _read(root, n)
        if not text or f"[{section}]" not in text:
            continue
        body = text.split(f"[{section}]", 1)[1].split("\n[", 1)[0]
        m = re.search(r"^testpaths\s*=\s*(.+(?:\n[ \t]+.+)*)", body, re.M)
        if m:
            return [x for x in re.findall(r"[\w./-]+", m.group(1)) if x not in ("testpaths",)] or ["."]
        return ["."]
    return ["."]


def full_suite(command: str, gates: list[list[str]], root: str = ".", depth: int = 0) -> bool:
    """A test command runs a gate's whole suite if it invokes a runner with no test paths, with
    every path of a declared gate (or a directory holding them all), or with a directory that
    holds a collection root of a gate that names no paths (pytest testpaths, vitest include
    roots). Package scripts count by what they run. Targeted runs (files under those roots,
    -k, ::node ids) are allowed. Judgment call: wrong if workers need a declared gate's full
    file list to verify a change."""
    for part in re.split(r"&&|\|\||;|\|", command):
        try:
            argv = shlex.split(part)
        except ValueError:
            argv = part.split()
        argv = _strip_assignments(argv)
        entry = _script(argv, root)
        if entry is not None:
            if depth < 3 and full_suite(entry, gates, root, depth + 1):
                return True
            continue
        if not RUNNERS.search(part):
            continue
        if "-k" in argv or any("::" in a for a in argv):
            continue
        mine = {_norm(p) for p in _paths(argv)} | {"." for a in argv[1:] if a in (".", "./")}
        if not mine:
            return True
        for g in gates:
            theirs = {_norm(p) for p in _paths(g)}
            if theirs:
                if theirs <= mine or any(all(_covers(m, t) for t in theirs) for m in mine):
                    return True
            elif any(_covers(m, r) for m in mine for r in _collection_roots(g, root)):
                return True
    return False


def decide(ev: dict, root: str, gates: list[list[str]]) -> str | None:
    tool = ev.get("tool_name", "")
    inp = ev.get("tool_input") or {}
    if tool == "Bash":
        cmd = inp.get("command", "")
        if GIT_WRITE.search(cmd):
            return "git commit and git push are refused: the driver commits after its gates pass."
        if full_suite(cmd, gates, root):
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
