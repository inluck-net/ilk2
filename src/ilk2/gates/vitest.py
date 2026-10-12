"""vitest adapter and package-script resolution (docs/02 §2.5.1, S20, S21).

- A gate written as a package script (`bun run test:x`, `npm test`) is resolved at submit to
  the runner command its package.json entry calls, with that runner's adapter. Its spec
  version then hashes only that script entry, not the whole package.json.
- vitest runs with ilk2's reporter; test ids are `<file>::<describe > ... > test>`.
- Config files that decide what runs: `-c/--config` if given, else the default vitest/vite
  config and workspace files in the gate's cwd. (Per-file owning-config resolution across
  workspace projects is not done yet; recorded in docs/m1-progress.md.)
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Callable

REPORTER = Path(__file__).with_name("ilk2_vitest_reporter.mjs")
PMS = {"npm", "bun", "pnpm", "yarn"}
DEFAULT_CONFIGS = [f"{n}.config.{e}" for n in ("vitest", "vite") for e in ("ts", "mts", "cts", "js", "mjs", "cjs")] \
    + [f"vitest.workspace.{e}" for e in ("ts", "js", "json")]


def script_name(command: list[str]) -> str | None:
    """`bun run x` / `npm run x` / `npm test` / `yarn x` -> the package script name."""
    if not command or Path(command[0]).name not in PMS:
        return None
    rest = command[1:]
    if rest[:1] == ["run"]:
        return rest[1] if len(rest) > 1 else None
    if rest[:1] == ["test"]:
        return "test"
    if Path(command[0]).name == "yarn" and rest:
        return rest[0]
    return None


def resolve(command: list[str], cwd: str, read: Callable[[str], str | None]) -> dict | None:
    """Resolve a package-script gate. Returns {adapter, command, script, script_entry} or None.

    read(relpath) returns a file's text at the batch base, or None."""
    name = script_name(command)
    if name is None:
        return None
    pkg = read(str(Path(cwd) / "package.json"))
    if pkg is None:
        return None
    entry = (json.loads(pkg).get("scripts") or {}).get(name)
    if not entry:
        return None
    argv = shlex.split(entry)
    while argv and "=" in argv[0] and not argv[0].startswith("-"):
        argv = argv[1:]  # leading VAR=value assignments
    if argv[:1] in (["npx"], ["bunx"], ["pnpm"]) and argv[1:2] == ["vitest"]:
        argv = argv[1:]
    extra = command[3:] if command[1:2] == ["run"] else command[2:]
    if extra[:1] == ["--"]:
        extra = extra[1:]
    if argv[:1] == ["vitest"] and not any(t in argv for t in ("&&", "||", ";", "|")):
        return {"adapter": "vitest", "command": argv + extra, "script": name, "script_entry": entry}
    if any(t in ("pytest",) for t in argv[:3]) or argv[:3] == ["python", "-m", "pytest"]:
        return {"adapter": "pytest", "command": argv + extra, "script": name, "script_entry": entry}
    return {"adapter": "shell", "command": command, "script": name, "script_entry": entry}


def config_files(command: list[str], cwd: str) -> list[str]:
    for i, a in enumerate(command):
        if a in ("-c", "--config") and i + 1 < len(command):
            return [str(Path(cwd) / command[i + 1])]
        if a.startswith("--config="):
            return [str(Path(cwd) / a.split("=", 1)[1])]
    return [str(Path(cwd) / c) for c in DEFAULT_CONFIGS]


def run_command(command: list[str], root: Path, cwd: str, report: Path) -> list[str]:
    """The command actually run: the project's own vitest binary, `run` mode, our reporter."""
    argv = list(command)
    if argv and argv[0] == "vitest":
        local = root / cwd / "node_modules" / ".bin" / "vitest"
        argv[0] = str(local) if local.exists() else "vitest"
    if len(argv) < 2 or argv[1] not in ("run", "bench"):
        argv.insert(1, "run")
    return argv + [f"--reporter={REPORTER}", f"--outputFile={report}"]


def with_test_ids(command: list[str], ids: list[str]) -> list[str]:
    """Re-run specific tests: files as positional args, names as -t (one name per call)."""
    files = sorted({i.split("::", 1)[0] for i in ids})
    names = [i.split("::", 1)[1].split(" > ")[-1] for i in ids if "::" in i]
    base = [a for a in command if not a.endswith((".ts", ".js", ".mts", ".tsx", ".jsx"))
            or a in config_files(command, ".")]
    out = base + files
    if len(names) == 1:
        out += ["-t", names[0]]
    return out
