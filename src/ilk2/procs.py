"""Process ownership and termination (docs/02 I4, §2.10; P1 findings 1-2).

- Each child ilk2 starts gets its own process group.
- Termination signals the group and every process group its descendants opened (Claude Code's
  Bash tool runs commands in a new group), then sweeps processes whose cwd is inside the
  worktree, which catches double-forked orphans. Matching is by pgid, parent chain and cwd;
  never by argv.
- Identity of a recorded process is (pid, start time), so a recycled pid is never signalled.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


def process_table() -> list[tuple[int, int, int, str]]:
    """(pid, ppid, pgid, comm) for every live process; zombies left out; no argv."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,comm="],
                         capture_output=True, text=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and not parts[3].startswith("Z"):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), Path(parts[4]).name))
    return rows


def start_time(pid: int) -> str | None:
    """The process's start time as ps reports it; None if it is gone."""
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
    s = out.stdout.strip()
    return s or None


def is_same_process(pid: int | None, recorded_start: str | None) -> bool:
    return bool(pid) and recorded_start is not None and start_time(pid) == recorded_start


def descendants(root: int) -> list[tuple[int, int, str]]:
    table = process_table()
    kids: dict[int, list[tuple[int, int, str]]] = {}
    for pid, ppid, pgid, comm in table:
        kids.setdefault(ppid, []).append((pid, pgid, comm))
    out, stack = [], [root]
    while stack:
        for row in kids.get(stack.pop(), []):
            out.append(row)
            stack.append(row[0])
    return out


def processes_in(root: Path) -> list[tuple[int, str]]:
    """Processes of this user whose cwd is inside root."""
    real = os.path.realpath(root)
    out = subprocess.run(["lsof", "-a", "-d", "cwd", "-u", str(os.getuid()), "-F", "pcn"],
                         capture_output=True, text=True).stdout
    rows, pid, comm = [], None, ""
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            pid, comm = int(val), ""
        elif tag == "c":
            comm = val
        elif tag == "n" and pid is not None and (val == real or val.startswith(real + os.sep)):
            rows.append((pid, comm))
    return rows


def sweep(root: Path) -> list[tuple[int, str]]:
    found = [r for r in processes_in(root) if r[0] != os.getpid()]
    for pid, _ in found:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return found


def terminate_group(pgid: int, reap=lambda: None, term_grace_s: float = 30.0,
                    kill_grace_s: float = 60.0, sweep_root: Path | None = None) -> dict:
    """SIGTERM the group and its descendants' groups, wait, SIGKILL, wait (§2.11.1).

    Returns survivors after the kill grace; a non-empty list means `unkillable`.
    """
    t0 = time.monotonic()
    before = descendants(pgid)
    groups = [pgid] + sorted({g for _, g, _ in before if g != pgid})
    pids = {p for p, _, _ in before}

    def survivors() -> list[tuple[int, str]]:
        return [(p, c) for p, _, g, c in process_table() if g in groups or p in pids]

    def signal_all(sig: int) -> None:
        for g in groups:
            try:
                os.killpg(g, sig)
            except (ProcessLookupError, PermissionError):
                pass  # gone, or zombie-only (macOS gives EPERM)

    def wait(limit: float) -> None:
        end = time.monotonic() + limit
        while time.monotonic() < end and survivors():
            reap()
            time.sleep(0.1)

    sent = []
    if term_grace_s > 0:
        signal_all(signal.SIGTERM)
        sent.append("TERM")
        wait(term_grace_s)
    if survivors():
        signal_all(signal.SIGKILL)
        sent.append("KILL")
        wait(kill_grace_s)
    reap()
    swept = sweep(sweep_root) if sweep_root else []
    return {"signals": sent, "groups": groups, "seconds": round(time.monotonic() - t0, 2),
            "survivors": survivors(), "swept": swept}


def host_env() -> dict[str, str]:
    """The parent environment minus any virtualenv a `uv run` put on PATH (P1 finding 12)."""
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    if venv:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                      if not p.startswith(venv))
    env.pop("UV_RUN_RECURSION_DEPTH", None)
    return env
