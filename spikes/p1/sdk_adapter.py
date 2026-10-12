"""P1 step 2: dispatch adapter over the Claude Agent SDK (docs/02 §2.6, Q1).

Same interface as cli_adapter: start, resume, cancel, stream events. The SDK drives a CLI
subprocess; sdk_launch.sh gives that subprocess its own process group and a scrubbed
provider environment, and optionally the step 3 sandbox. Spike code, not the core.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from pathlib import Path

import claude_agent_sdk as sdk

from cli_adapter import Isolation, cancel_group

LAUNCHER = Path(__file__).parent / "sdk_launch.sh"
BUNDLED_CLI = Path(sdk.__file__).parent / "_bundled" / "claude"


def _jsonable(msg) -> dict:
    try:
        d = dataclasses.asdict(msg)
    except TypeError:
        d = {"repr": repr(msg)[:500]}
    d["_type"] = type(msg).__name__
    return d


@dataclasses.dataclass
class SdkAttempt:
    kind: str
    log_path: Path
    pid_file: Path
    t_spawn: float = 0.0
    session_id: str | None = None
    init_model: str | None = None
    result: dict | None = None
    timings: dict = dataclasses.field(default_factory=dict)
    tool_uses: list = dataclasses.field(default_factory=list)
    hook_calls: list = dataclasses.field(default_factory=list)

    @property
    def pgid(self) -> int | None:
        try:
            return int(self.pid_file.read_text())
        except (FileNotFoundError, ValueError):
            return None

    def note(self, msg) -> None:
        t = time.monotonic() - self.t_spawn
        with self.log_path.open("a") as f:
            f.write(json.dumps({"t": round(t, 3), "msg": _jsonable(msg)}, default=str) + "\n")
        if isinstance(msg, sdk.SystemMessage) and msg.subtype == "init":
            self.session_id = msg.data.get("session_id")
            self.init_model = msg.data.get("model")
            self.timings.setdefault("init_s", t)
        elif isinstance(msg, sdk.AssistantMessage):
            self.timings.setdefault("first_assistant_s", t)
            for block in msg.content:
                if isinstance(block, sdk.ToolUseBlock):
                    self.tool_uses.append(block.name)
                    self.timings.setdefault("first_tool_use_s", t)
                    if block.name in ("Edit", "Write", "MultiEdit"):
                        self.timings.setdefault("first_edit_s", t)
        elif isinstance(msg, sdk.ResultMessage):
            self.result = _jsonable(msg)
            self.timings["result_s"] = t

    def summary(self) -> dict:
        r = self.result or {}
        return {
            "kind": self.kind,
            "session_id": self.session_id,
            "init_model": self.init_model,
            "result_subtype": r.get("subtype"),
            "is_error": r.get("is_error"),
            "num_turns": r.get("num_turns"),
            "duration_ms": r.get("duration_ms"),
            "model_usage": sorted((r.get("model_usage") or {}).keys()),
            "tool_uses": self.tool_uses,
            "sdk_hook_calls": self.hook_calls,
            "timings": {k: round(v, 3) for k, v in self.timings.items()},
        }


class SdkAdapter:
    def __init__(self, config_dir: Path, log_dir: Path, max_turns: int = 30,
                 isolation: Isolation | None = None, cli: Path = BUNDLED_CLI,
                 sdk_hook=None):
        self.config_dir = config_dir
        self.log_dir = log_dir
        self.max_turns = max_turns
        self.isolation = isolation
        self.cli = cli
        self.sdk_hook = sdk_hook  # optional async PreToolUse callback, for hook parity
        self._n = 0

    def _options(self, att: SdkAttempt, cwd: Path, resume: str | None) -> sdk.ClaudeAgentOptions:
        env = {
            "CLAUDE_CONFIG_DIR": str(self.config_dir),
            "ILK2_CLI": str(self.cli),
            "ILK2_PID_FILE": str(att.pid_file),
        }
        iso = self.isolation
        if iso is not None:
            iso.tmp.mkdir(parents=True, mode=0o700, exist_ok=True)
            env |= iso.env()
            if iso.sandbox:
                profile = self.log_dir / "sandbox.sb"
                profile.write_text(iso.profile())
                env["ILK2_SANDBOX_PROFILE"] = str(profile)
        hooks = None
        if self.sdk_hook is not None:
            async def wrapped(inp, tool_use_id, ctx):
                out = await self.sdk_hook(inp, tool_use_id, ctx)
                att.hook_calls.append({"tool": inp.get("tool_name"), "out": out})
                return out
            hooks = {"PreToolUse": [sdk.HookMatcher(matcher="Bash", hooks=[wrapped])]}
        return sdk.ClaudeAgentOptions(
            cwd=str(cwd),
            cli_path=str(LAUNCHER),
            env=env,
            setting_sources=["user"],  # CLAUDE_CONFIG_DIR/settings.json: MiMo env + file hooks
            permission_mode="bypassPermissions",
            strict_mcp_config=True,
            max_turns=self.max_turns,
            resume=resume,
            hooks=hooks,
        )

    def _attempt(self, kind: str) -> SdkAttempt:
        self._n += 1
        base = self.log_dir / f"sdk-{self._n:02d}-{kind}"
        return SdkAttempt(kind=kind, log_path=base.with_suffix(".jsonl"),
                          pid_file=base.with_suffix(".pid"))

    async def run(self, prompt: str, cwd: Path, resume: str | None = None,
                  stop_when=None) -> SdkAttempt:
        """Run one attempt to completion, or until stop_when(attempt) is true (then return live)."""
        att = self._attempt("resume" if resume else "start")
        att.t_spawn = time.monotonic()
        client = sdk.ClaudeSDKClient(self._options(att, cwd, resume))
        await client.connect()
        att.client = client  # type: ignore[attr-defined]
        await client.query(prompt)
        async for msg in client.receive_response():
            att.note(msg)
            if stop_when is not None and stop_when(att):
                return att
        await client.disconnect()
        att.timings["exit_s"] = time.monotonic() - att.t_spawn
        return att

    @staticmethod
    async def cancel(att: SdkAttempt, grace_s: float = 5.0, term_first: bool = True) -> dict:
        pgid = att.pgid
        res = cancel_group(pgid, lambda: None, grace_s, term_first) if pgid else {"error": "no pgid"}
        try:
            await asyncio.wait_for(att.client.disconnect(), timeout=15)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 - the process is already gone; record how close behaved
            res["disconnect_error"] = type(e).__name__
        return res
