"""P1 step 1: dispatch adapter with the Claude Code CLI as the baseline (docs/02 §2.6, Q1).

Spike code: evidence, not the core. Interface: start, resume, cancel, stream events.

Rules kept here:
- The per-run config dir is a copy of ~/.ilk2/worker-template; V1 homes are never read.
- Nothing logs settings, environment or full argv. Only flag names are recorded.
- The worker runs in its own process group (start_new_session) so cancel can killpg it.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

TEMPLATE = Path.home() / ".ilk2" / "worker-template"

# Inherited variables that could override the template's provider settings.
_SCRUB_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_CONFIG_DIR")


def make_config_dir(dest: Path) -> Path:
    """Copy the pinned template into a private per-run config dir (mode 700, settings 600)."""
    dest.mkdir(parents=True, mode=0o700, exist_ok=False)
    src = TEMPLATE / "settings.json"
    shutil.copy2(src, dest / "settings.json")
    os.chmod(dest / "settings.json", 0o600)
    return dest


def _worker_env(config_dir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(_SCRUB_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


@dataclass
class Attempt:
    """One `claude -p` process: a start or a resume."""

    proc: subprocess.Popen
    log_path: Path
    t_spawn: float
    kind: str
    session_id: str | None = None
    init_model: str | None = None
    result: dict | None = None
    timings: dict[str, float] = field(default_factory=dict)
    tool_uses: list[str] = field(default_factory=list)

    @property
    def pgid(self) -> int:
        return self.proc.pid  # session leader: pgid == pid

    def events(self) -> Iterator[dict]:
        """Yield stream-json events, logging each with a monotonic offset from spawn."""
        assert self.proc.stdout is not None
        with self.log_path.open("a") as log:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                t = time.monotonic() - self.t_spawn
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    ev = {"type": "_unparsed", "text": line[:200]}
                log.write(json.dumps({"t": round(t, 3), "ev": ev}) + "\n")
                self._note(ev, t)
                yield ev

    def _note(self, ev: dict, t: float) -> None:
        typ = ev.get("type")
        if typ == "system" and ev.get("subtype") == "init":
            self.session_id = ev.get("session_id")
            self.init_model = ev.get("model")
            self.timings.setdefault("init_s", t)
        elif typ == "assistant":
            self.timings.setdefault("first_assistant_s", t)
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    self.tool_uses.append(name)
                    self.timings.setdefault("first_tool_use_s", t)
                    if name in ("Edit", "Write", "MultiEdit"):
                        self.timings.setdefault("first_edit_s", t)
        elif typ == "result":
            self.result = ev
            self.timings["result_s"] = t

    def wait(self, timeout: float | None = None) -> int:
        for _ in self.events():
            pass
        rc = self.proc.wait(timeout=timeout)
        self.timings["exit_s"] = time.monotonic() - self.t_spawn
        return rc

    def summary(self) -> dict:
        r = self.result or {}
        return {
            "kind": self.kind,
            "pid": self.proc.pid,
            "returncode": self.proc.returncode,
            "session_id": self.session_id,
            "init_model": self.init_model,
            "result_subtype": r.get("subtype"),
            "is_error": r.get("is_error"),
            "num_turns": r.get("num_turns"),
            "duration_ms": r.get("duration_ms"),
            "duration_api_ms": r.get("duration_api_ms"),
            "model_usage": sorted(r.get("modelUsage", {})),
            "tool_uses": self.tool_uses,
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
        }


class CliAdapter:
    """Dispatch through `claude -p --output-format stream-json`."""

    def __init__(self, config_dir: Path, log_dir: Path, max_turns: int = 30):
        self.config_dir = config_dir
        self.log_dir = log_dir
        self.max_turns = max_turns
        self._n = 0

    def _spawn(self, kind: str, prompt: str, cwd: Path, extra: list[str]) -> Attempt:
        self._n += 1
        argv = [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose",
            "--max-turns", str(self.max_turns),
            "--permission-mode", "bypassPermissions",
            "--strict-mcp-config",
            *extra,
        ]
        log_path = self.log_dir / f"attempt-{self._n:02d}-{kind}.jsonl"
        stderr_path = log_path.with_suffix(".stderr.txt")
        t0 = time.monotonic()
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=_worker_env(self.config_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_path.open("w"),
            text=True,
            start_new_session=True,
        )
        # Prompt over stdin keeps it out of the process arguments.
        assert proc.stdin is not None
        proc.stdin.write(prompt)
        proc.stdin.close()
        return Attempt(proc=proc, log_path=log_path, t_spawn=t0, kind=kind)

    def start(self, prompt: str, cwd: Path) -> Attempt:
        return self._spawn("start", prompt, cwd, [])

    def resume(self, session_id: str, prompt: str, cwd: Path) -> Attempt:
        return self._spawn("resume", prompt, cwd, ["--resume", session_id])

    @staticmethod
    def cancel(attempt: Attempt, grace_s: float = 5.0, term_first: bool = True) -> dict:
        """Stop the worker and every process group its descendants opened.

        Claude Code's Bash tool runs commands in a new process group, so killpg on the
        worker's group alone orphans them under SIGKILL (probe sigkill-20261013-*).
        The descendant groups are snapshotted before signalling, because once the worker
        dies its children are reparented to launchd and can no longer be found from it.
        term_first=False skips SIGTERM, standing in for a worker that cannot clean up.
        """
        pgid = attempt.pgid
        t0 = time.monotonic()
        before = descendants(pgid)
        groups = [pgid] + sorted({g for _, g, _ in before if g != pgid})
        pids = {pid for pid, _, _ in before}

        def survivors() -> list[tuple[int, str]]:
            return [(p, c) for p, _, g, c in process_table() if g in groups or p in pids]

        def signal_all(sig: signal.Signals) -> None:
            for g in groups:
                try:
                    os.killpg(g, sig)
                except (ProcessLookupError, PermissionError):
                    pass  # gone, or zombie-only (macOS returns EPERM for those)

        sent = []
        if term_first:
            signal_all(signal.SIGTERM)
            sent.append("TERM")
            deadline = t0 + grace_s
            while time.monotonic() < deadline and survivors():
                attempt.proc.poll()  # reap the leader: macOS killpg on a zombie-only group is EPERM
                time.sleep(0.1)
        if survivors():
            signal_all(signal.SIGKILL)
            sent.append("KILL")
        try:
            attempt.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.5)
        return {
            "signals": sent,
            "groups": groups,
            "cancel_s": round(time.monotonic() - t0, 3),
            "returncode": attempt.proc.returncode,
            "survivors": survivors(),
        }


def process_table() -> list[tuple[int, int, int, str]]:
    """(pid, ppid, pgid, comm) for every live process; zombies are left out.

    comm only: never full arguments.
    """
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,pgid=,stat=,comm="],
        capture_output=True, text=True, check=True,
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and not parts[3].startswith("Z"):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), Path(parts[4]).name))
    return rows


def group_members(pgid: int) -> list[tuple[int, str]]:
    return [(pid, comm) for pid, _, g, comm in process_table() if g == pgid]


def descendants(root: int) -> list[tuple[int, int, str]]:
    """(pid, pgid, comm) of every descendant of root, whatever group it is in."""
    table = process_table()
    children: dict[int, list[tuple[int, int, str]]] = {}
    for pid, ppid, pgid, comm in table:
        children.setdefault(ppid, []).append((pid, pgid, comm))
    out, stack = [], [root]
    while stack:
        for row in children.get(stack.pop(), []):
            out.append(row)
            stack.append(row[0])
    return out
