"""P1 step 2 live run: the Agent SDK adapter against the adoption criteria (docs/02 §2.6, Q1).

Usage: uv run --group spike python spikes/p1/run_step2.py
Every attempt runs under the step 3 isolation (sandbox-exec + file hook), so this also checks
that the SDK keeps the same sandbox behaviour. One MiMo session at a time.

Checks: (1) MiMo compatibility, (2) session identity on resume, (3) hook parity: the
settings-file hook and an SDK callback hook both fire and refuse `git commit`,
(4) cancellation by process group, (5) streaming timing metrics, (6) sandbox: a Bash write
outside the worktree is refused.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cli_adapter import Isolation, descendants, make_config_dir  # noqa: E402
from run_step1 import BRIEF, CANCEL_BRIEF, RESUME, run_gate, seed, sh  # noqa: E402
from sdk_adapter import BUNDLED_CLI, SdkAdapter  # noqa: E402

PROBE = (
    "This is an isolation test of your environment. Using the Bash tool, run each command "
    "below separately, even if an earlier one fails, then report each command's exact "
    "output or error:\n1. git commit --allow-empty -m probe\n2. echo probe > {outside}\n"
    "Do not try workarounds."
)


async def deny_git_commit(inp, tool_use_id, ctx):
    cmd = (inp.get("tool_input") or {}).get("command", "")
    if "git commit" in cmd or "git push" in cmd:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "sdk hook: git commit/push refused",
        }}
    return {}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def main() -> int:
    run = Path.home() / ".ilk2" / "spike-p1" / f"step2-{datetime.now():%Y%m%d-%H%M%S}"
    run.mkdir(parents=True, mode=0o700)
    origin, wt, gate = seed(run)
    outside = run / "outside"
    outside.mkdir()
    config = make_config_dir(run / "config", hook=True)
    logs = run / "logs"
    logs.mkdir()
    iso = Isolation(worktree=wt, tmp=run / "tmp", log_dir=logs, config_dir=config)
    ad = SdkAdapter(config, logs, isolation=iso, sdk_hook=deny_git_commit)
    s: dict = {"run_dir": str(run), "cli": str(BUNDLED_CLI),
               "cli_version": sh(str(BUNDLED_CLI), "--version").strip()}
    head0 = sh("git", "rev-parse", "HEAD", cwd=wt)

    a1 = await ad.run(BRIEF, wt)
    rc1, out1 = run_gate(gate, wt)
    s["start"] = a1.summary() | {"gate_rc": rc1, "pgid": a1.pgid, "driver_pgid": os.getpgid(0)}
    print("start:", json.dumps(s["start"]), flush=True)

    a2 = await ad.run(RESUME.format(out=out1), wt, resume=a1.session_id)
    rc2, out2 = run_gate(gate, wt)
    s["resume"] = a2.summary() | {"gate_rc": rc2, "same_session_id": a2.session_id == a1.session_id}
    print("resume:", json.dumps(s["resume"]), flush=True)

    target = outside / "probe.txt"
    a3 = await ad.run(PROBE.format(outside=target), wt)
    hook_log = [json.loads(l) for l in (logs / "hook.jsonl").read_text().splitlines()] \
        if (logs / "hook.jsonl").exists() else []
    s["probe"] = a3.summary() | {
        "file_hook_refusals": [h for h in hook_log if h["refused"]],
        "outside_file_exists": target.exists(),
        "head_unchanged": sh("git", "rev-parse", "HEAD", cwd=wt) == head0,
        "result_text": (a3.result or {}).get("result", "")[:600],
    }
    print("probe:", json.dumps(s["probe"]), flush=True)

    def sleep_running(att) -> bool:
        return att.pgid is not None and any(r[2] == "sleep" for r in descendants(att.pgid))

    a4 = await ad.run(CANCEL_BRIEF, wt, stop_when=lambda att: "Bash" in att.tool_uses)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not sleep_running(a4):
        await asyncio.sleep(0.5)
    before = descendants(a4.pgid) if a4.pgid else []
    res = await SdkAdapter.cancel(a4)
    s["cancel"] = res | {
        "pgid": a4.pgid, "driver_pgid": os.getpgid(0),
        "descendants_before": before,
        "alive_after": {f"{c}:{p}": alive(p) for p, _, c in before},
        "driver_alive": True,
    }
    print("cancel:", json.dumps(s["cancel"], default=str), flush=True)

    (run / "summary.json").write_text(json.dumps(s, indent=2, default=str) + "\n")
    checks = {
        "1_mimo": s["start"]["init_model"] == "mimo-v2.6-pro"
                  and s["start"]["model_usage"] == ["mimo-v2.6-pro"],
        "2_resume_identity": s["resume"]["same_session_id"] and rc1 != 0 and rc2 == 0,
        "3_hook_parity": bool(s["probe"]["file_hook_refusals"]) and bool(a3.hook_calls)
                         and s["probe"]["head_unchanged"],
        "4_cancel_pgroup": bool(before) and a4.pgid != os.getpgid(0)
                           and not res.get("survivors") and not any(s["cancel"]["alive_after"].values()),
        "5_timings": all(k in s["start"]["timings"] for k in ("init_s", "first_assistant_s", "result_s")),
        "6_sandbox": not s["probe"]["outside_file_exists"],
    }
    s["checks"] = checks
    (run / "summary.json").write_text(json.dumps(s, indent=2, default=str) + "\n")
    print("checks:", json.dumps(checks))
    print("STEP2", "PASS" if all(checks.values()) else "FAIL", run)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
