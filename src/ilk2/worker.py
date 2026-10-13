"""The worker: one Claude Code session with MiMo, isolated to a worktree (docs/02 §2.6).

Dispatch goes through the CLI (Q1, decided by P1 step 2). Each attempt gets:
- a private config dir copied from the pinned template, with ilk2's hook added;
- a sandbox-exec profile that allows writes only to the worktree, a per-attempt temp dir,
  the config dir, the hook log, and Claude Code's own scratch (P1 finding 8);
- an environment without inherited provider or ilk variables;
- its own process group, terminated with every descendant group plus a cwd sweep.
Attempt outcome is one of: done, error, api, quota, crashed, timeout, cancelled.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, procs

HOOK = Path(__file__).with_name("worker_hook.py")
SCRUB = ("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDECODE", "CLAUDE_CONFIG_DIR", "ILK_", "ILK2_",
         # these override autoCompactEnabled, so the pin below would silently not hold (W2)
         "DISABLE_AUTO_COMPACT", "DISABLE_COMPACT")
# docs/06 W2: auto-compact pinned on, as a safety net only (one step is the context bound,
# docs/02 §2.6). Passed with --settings, which outranks the consumer repo's .claude/settings.json.
# Judgment call: the window (autoCompactWindow) is left to Claude Code; wrong if W4's peaks near it.
PINNED_SETTINGS = {"autoCompactEnabled": True}

API_ERROR = re.compile(r"API Error: (429|5\d\d)|overloaded|rate.?limit", re.I)
QUOTA = re.compile(r"quota|insufficient.?balance|usage limit|credit balance", re.I)


@dataclass
class WorkerConfig:
    claude_bin: str = "claude"
    # Follows ILK2_HOME, so tests never copy the real template (credentials) anywhere.
    template: Path = field(default_factory=lambda: paths.home() / "worker-template")
    max_turns: int = 400
    sandbox: bool = True
    sandbox_allow: list[str] = field(default_factory=list)
    hook: bool = True
    hook_python: str = sys.executable
    permission_mode: str = "bypassPermissions"


@dataclass
class AttemptFiles:
    root: Path

    @property
    def config(self) -> Path: return self.root / "config"
    @property
    def tmp(self) -> Path: return self.root / "tmp"
    @property
    def logs(self) -> Path: return self.root / "logs"


def tool_dirs(worktree: Path) -> list[str]:
    base = f"/private/tmp/claude-{os.getuid()}"
    keys = {re.sub(r"[^A-Za-z0-9-]", "-", str(p)) for p in (worktree, os.path.realpath(worktree))}
    return [f"{base}/{k}" for k in sorted(keys)]


def sandbox_profile(worktree: Path, files: AttemptFiles, extra: list[str] = ()) -> str:
    allowed = [os.path.realpath(p) for p in (worktree, files.tmp, files.config, files.logs, *extra)]
    allowed += tool_dirs(worktree)
    subpaths = " ".join(f'(subpath "{p}")' for p in allowed)
    return ("(version 1)\n(allow default)\n(deny file-write*)\n"
            f'(allow file-write* {subpaths} (subpath "/dev")\n'
            '  (regex #"^/private/tmp/claude-[0-9a-f]+-cwd$"))\n')


def prepare(cfg: WorkerConfig, files: AttemptFiles, worktree: Path, gate_commands: list[list[str]],
            fresh_config: bool = True) -> None:
    """Create the attempt's config dir, temp dir and logs. Values are never printed."""
    for d in (files.root, files.tmp, files.logs):
        d.mkdir(parents=True, mode=0o700, exist_ok=True)
    if fresh_config or not (files.config / "settings.json").exists():
        files.config.mkdir(parents=True, mode=0o700, exist_ok=True)
        src = cfg.template / "settings.json"
        settings = json.loads(src.read_text()) if src.exists() else {}
        settings.pop("hooks", None)
        if cfg.hook:
            settings["hooks"] = {"PreToolUse": [{
                "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
                "hooks": [{"type": "command", "command": f"{cfg.hook_python} -I {HOOK}"}]}]}
        dst = files.config / "settings.json"
        if dst.exists():
            dst.unlink()
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(settings, f, indent=2)
    (files.logs / "gates.json").write_text(json.dumps(gate_commands))
    (files.logs / "sandbox.sb").write_text(sandbox_profile(worktree, files, cfg.sandbox_allow))


def worker_env(files: AttemptFiles, worktree: Path) -> dict[str, str]:
    env = {k: v for k, v in procs.host_env().items() if not k.startswith(SCRUB)}
    env["CLAUDE_CONFIG_DIR"] = str(files.config)
    env["TMPDIR"] = os.path.realpath(files.tmp) + "/"
    env["ILK2_WORKTREE"] = os.path.realpath(worktree)
    env["ILK2_GATES_FILE"] = str(files.logs / "gates.json")
    env["ILK2_HOOK_LOG"] = str(files.logs / "hook.jsonl")
    return env


@dataclass
class Dispatch:
    proc: subprocess.Popen
    log_path: Path
    worktree: Path
    t0: float
    session_id: str | None = None
    init_model: str | None = None
    result: dict | None = None
    timings: dict = field(default_factory=dict)
    tool_uses: list = field(default_factory=list)
    thinking_chars: int = 0
    thinking_s: float = 0.0
    peak_context: int = 0      # largest prompt (input + cache) of any turn: docs/02 §2.6
    compactions: int = 0       # compaction system events seen in the stream
    capped: bool = False
    cancelled: bool = False
    terminated: dict | None = None
    _last: float = 0.0

    @property
    def pgid(self) -> int:
        return self.proc.pid

    def _note(self, ev: dict, t: float) -> None:
        typ = ev.get("type")
        last, self._last = self._last, t
        if typ == "system" and ev.get("subtype") == "init":
            self.session_id = ev.get("session_id")
            self.init_model = ev.get("model")
            self.timings.setdefault("init_s", t)
        elif typ == "system" and ev.get("subtype") == "compact_boundary":
            # Agent SDK SDKCompactBoundaryMessage; pre_tokens is the context just before it
            self.compactions += 1
            pre = int((ev.get("compact_metadata") or {}).get("pre_tokens") or 0)
            self.peak_context = max(self.peak_context, pre)
        elif typ == "assistant":
            self.timings.setdefault("first_assistant_s", t)
            # main loop only: a subagent's turns (parent_tool_use_id set) have their own context
            u = {} if ev.get("parent_tool_use_id") else ((ev.get("message") or {}).get("usage") or {})
            ctx = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens"))
            self.peak_context = max(self.peak_context, ctx)
            for b in (ev.get("message") or {}).get("content", []):
                if b.get("type") == "thinking":
                    self.thinking_chars += len(b.get("thinking", ""))
                    self.thinking_s += t - last
                elif b.get("type") == "tool_use":
                    self.tool_uses.append(b.get("name", "?"))
                    self.timings.setdefault("first_tool_use_s", t)
                    if b.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                        self.timings.setdefault("first_edit_s", t)
        elif typ == "result":
            self.result = ev
            self.timings["result_s"] = t

    def wait(self, cap_s: float, term_grace_s: float = 30.0, kill_grace_s: float = 60.0) -> str:
        """Read events until the worker exits or the attempt cap passes. Returns the outcome.

        stdout is read on a thread and the main thread watches the process, so a descendant
        that inherited the pipe cannot hold the driver past the worker's own exit. The cap is
        on monotonic time, which does not advance while the Mac sleeps (§2.10, S16).
        """
        lock = threading.Lock()

        def read() -> None:
            with self.log_path.open("a") as log:
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    t = time.monotonic() - self.t0
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        ev = {"type": "_unparsed", "text": line[:200]}
                    log.write(json.dumps({"t": round(t, 3), "ev": ev}) + "\n")
                    log.flush()
                    with lock:
                        self._note(ev, t)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        deadline = self.t0 + cap_s
        while self.proc.poll() is None:
            if time.monotonic() >= deadline:
                self.capped = True
                self.terminate(term_grace_s, kill_grace_s)
                break
            time.sleep(0.1)
        reader.join(timeout=2)
        swept = procs.sweep(self.worktree)  # also frees a pipe an orphan still holds
        if swept:
            self.timings["swept"] = len(swept)
        reader.join(timeout=2)
        self.timings["exit_s"] = time.monotonic() - self.t0
        return self.outcome()

    def terminate(self, term_grace_s: float = 30.0, kill_grace_s: float = 60.0) -> dict:
        self.terminated = procs.terminate_group(self.pgid, self.proc.poll, term_grace_s,
                                                kill_grace_s, sweep_root=self.worktree)
        return self.terminated

    def cancel(self, term_grace_s: float = 30.0, kill_grace_s: float = 60.0) -> dict:
        self.cancelled = True
        return self.terminate(term_grace_s, kill_grace_s)

    def outcome(self) -> str:
        if self.capped:
            return "timeout"
        if self.cancelled:
            return "cancelled"
        r = self.result
        if r is None:
            return "crashed"
        if r.get("is_error"):
            text = str(r.get("result", ""))
            if QUOTA.search(text):
                return "quota"
            if API_ERROR.search(text):
                return "api"
            return "error"
        return "done"

    def summary(self) -> dict:
        r = self.result or {}
        return {
            "session_id": self.session_id, "init_model": self.init_model,
            "outcome": self.outcome(), "returncode": self.proc.returncode,
            "num_turns": r.get("num_turns"), "duration_ms": r.get("duration_ms"),
            "duration_api_ms": r.get("duration_api_ms"),
            "model_usage": sorted((r.get("modelUsage") or {}).keys()),
            "tool_counts": {n: self.tool_uses.count(n) for n in sorted(set(self.tool_uses))},
            "thinking_chars": self.thinking_chars, "thinking_s": round(self.thinking_s, 1),
            "peak_context_tokens": self.peak_context, "compactions": self.compactions,
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
            "terminated": self.terminated,
        }


def dispatch(cfg: WorkerConfig, files: AttemptFiles, worktree: Path, prompt: str,
             resume: str | None = None) -> Dispatch:
    argv = [cfg.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
            "--max-turns", str(cfg.max_turns), "--permission-mode", cfg.permission_mode,
            "--strict-mcp-config", "--settings", json.dumps(PINNED_SETTINGS)]
    if resume:
        argv += ["--resume", resume]
    if cfg.sandbox:
        argv = ["/usr/bin/sandbox-exec", "-f", str(files.logs / "sandbox.sb"), *argv]
    log = files.logs / ("resume.jsonl" if resume else "start.jsonl")
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, cwd=worktree, env=worker_env(files, worktree),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=(files.logs / "stderr.txt").open("a"), text=True,
                            start_new_session=True)
    assert proc.stdin is not None
    try:
        proc.stdin.write(prompt)  # over stdin: the prompt never appears in process arguments
        proc.stdin.close()
    except BrokenPipeError:
        pass
    return Dispatch(proc=proc, log_path=log, worktree=worktree, t0=t0)


# -- the brief (§2.6) ------------------------------------------------------------------------

BRIEF = """You are the worker in an automated build loop, in a git worktree at the base of this
step. Complete the step below, then reply with a short summary of what you changed.

Rules:
- Do not run git commit or git push. The driver commits after its gates pass.
- Run only targeted tests for the files you change. The driver runs the declared gates after
  your attempt; running a gate's whole suite yourself is refused.
- You can write only inside this worktree. Setuid programs such as `ps` cannot run here; a
  test that needs one may fail for you but pass in the driver's gate.
- Do not weaken tests to make them pass.

Declared gates (the driver runs these after you finish):
{gates}

## Step: {title}

{instructions}
"""

EVIDENCE = """

## The previous attempt left the gate red

{evidence}
"""


def brief(title: str, instructions: str, gate_lines: list[str], evidence: str | None = None,
          preload: str | None = None) -> str:
    b = BRIEF.format(title=title, instructions=instructions,
                     gates="\n".join(f"- `{g}`" for g in gate_lines) or "- (none)")
    if preload:
        b += "\n\n" + preload
    if evidence:
        b += EVIDENCE.format(evidence=evidence)
    return b


# -- preloaded context (§2.6, Q10) ------------------------------------------------------------

WHOLE_FILE_MAX_LINES = 600
WINDOW = 30
PRELOAD_CAP = 150_000


def _windows(lines: list[str], idents: set[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if any(x in line for x in idents):
            a, b = max(0, i - WINDOW), min(len(lines), i + WINDOW + 1)
            if spans and a <= spans[-1][1]:
                spans[-1] = (spans[-1][0], max(spans[-1][1], b))
            else:
                spans.append((a, b))
    return spans


def preload(root: Path, scope: list[str], instructions: str, gate_commands: list[list[str]],
            earlier_diff: str) -> str:
    """Context the driver assembles at no model cost: the files the step names, the gates'
    test files, and the diff of the batch's earlier accepted steps. Same rules as P1 step 4
    (whole files up to 600 lines, else windows around named identifiers; 150k characters)."""
    idents = {m for m in re.findall(r"`([A-Za-z_][A-Za-z0-9_.]{3,})`", instructions)}
    idents |= set(re.findall(r"\b([a-z_][a-z0-9_]{3,})\(", instructions))
    files: list[str] = []
    for s in scope:
        p = root / s
        if p.is_file():
            files.append(s)
        elif p.is_dir():
            files += [str(f.relative_to(root)) for f in sorted(p.rglob("*")) if f.is_file()
                      and f.suffix in (".py", ".ts", ".js", ".sh", ".md")][:20]
    for cmd in gate_commands:
        for a in cmd[1:]:
            if not a.startswith("-") and (root / a.split("::")[0]).is_file():
                files.append(a.split("::")[0])
    parts = ["# Preloaded context (assembled by the driver)\n"]
    for rel in dict.fromkeys(files):
        body = (root / rel).read_text(errors="replace")
        lines = body.splitlines()
        if len(lines) <= WHOLE_FILE_MAX_LINES:
            parts.append(f"\n## {rel} ({len(lines)} lines)\n```\n{body}\n```\n")
        else:
            parts.append(f"\n## {rel} ({len(lines)} lines; windows around names the step uses)\n")
            for a, b in _windows(lines, idents):
                parts.append("```\n" + "\n".join(f"{i + 1}: {lines[i]}" for i in range(a, b)) + "\n```\n")
    if earlier_diff:
        parts.append(f"\n## Changes from this batch's earlier steps\n```diff\n{earlier_diff}\n```\n")
    text = "".join(parts)
    return text[:PRELOAD_CAP] + ("\n[preload truncated]\n" if len(text) > PRELOAD_CAP else "")
