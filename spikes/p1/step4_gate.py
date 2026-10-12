"""P1 step 4 gate: the driver's acceptance check for one V1 fix step.

A step is accepted when (a) every declared local_checks command exits 0, and (b) every test
that the pinned test file had at the base passes with --runxfail, so a worker cannot pass by
deleting or re-marking the pinned tests (a protected verification asset, docs/06 P2-1).
Gates run without sandbox-exec: a sandboxed process cannot exec setuid `ps`, which V1 tests
use (preflight: 43 environment exclusions for S3, and pinned S4 tests failing). Instead HOME,
ILK_DATA_HOME and ILK_DATA_DIR are pinned to the gate's temp dir (judgment call; wrong if a
V1 test writes a hard-coded absolute V1 path; checked by looking for ilk2-keyed projects in
~/.ilk-data after the preflight). PYTHONUSERBASE keeps the host's user-site pytest plugins.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from cli_adapter import host_env

USER_BASE = subprocess.run(["/usr/bin/python3", "-c", "import site; print(site.USER_BASE)"],
                           capture_output=True, text=True, check=True).stdout.strip()


def _sandboxed(cmd: list[str], wt: Path, tmp: Path, timeout: int) -> tuple[int, str, float]:
    tmp.mkdir(parents=True, exist_ok=True)
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in host_env().items() if not k.startswith(("ANTHROPIC_", "CLAUDE"))}
    env["PYTHONUSERBASE"] = USER_BASE
    env["HOME"] = str(home)
    env["ILK_DATA_HOME"] = env["ILK_DATA_DIR"] = str(tmp / "ilk-data")
    env["TMPDIR"] = os.path.realpath(tmp) + "/"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=wt, env=env,
                           capture_output=True, text=True, timeout=timeout,
                           start_new_session=True)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        rc, out = 124, f"gate timed out after {timeout}s\n{(e.stdout or '')[-4000:]}"
    return rc, out, time.monotonic() - t0


def pinned_ids(wt: Path, tmp: Path, pinned: str) -> list[str]:
    rc, out, _ = _sandboxed(["python3", "-m", "pytest", "--collect-only", "-q", "-p",
                             "no:cacheprovider", pinned], wt, tmp, 300)
    return sorted(l.strip() for l in out.splitlines() if "::" in l and not l.startswith(" "))


def run_gate(wt: Path, tmp: Path, commands: list[str], pinned: str,
             base_ids: list[str], exclude: list[str] = ()) -> dict:
    """exclude: test ids that fail at the real V1 commit under this gate environment
    (e.g. a test that execs setuid `ps`, which sandbox-exec forbids). Deselected for both arms."""
    res: dict = {"commands": [], "accepted": False}
    for c in commands:
        argv = shlex.split(c) + [x for t in exclude for x in ("--deselect", t)]
        rc, out, secs = _sandboxed(argv, wt, tmp, 1800)
        failed = sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)))
        res["commands"].append({"rc": rc, "secs": round(secs, 1), "failed": failed,
                                "tail": out[-3000:]})
    rc, out, secs = _sandboxed(["python3", "-m", "pytest", "--runxfail", "-q", "-rA", "-p",
                                "no:cacheprovider", "--timeout=120",
                                "--timeout-method=signal", pinned], wt, tmp, 900)
    passed = set(re.findall(r"^PASSED (\S+)", out, re.M))
    missing = [i for i in base_ids if i not in passed]
    res["pinned"] = {"rc": rc, "secs": round(secs, 1), "base_ids": len(base_ids),
                     "not_passing": missing, "tail": out[-3000:]}
    res["accepted"] = all(c["rc"] == 0 for c in res["commands"]) and rc == 0 and not missing
    return res


def red_evidence(res: dict) -> str:
    """The text a retry gets: failing tails only, trimmed."""
    parts = []
    for i, c in enumerate(res["commands"]):
        if c["rc"] != 0:
            parts.append(f"Gate command {i + 1} exited {c['rc']}:\n{c['tail'][-2500:]}")
    p = res["pinned"]
    if p["rc"] != 0 or p["not_passing"]:
        parts.append("The pinned tests must all pass with --runxfail (xfail markers removed). "
                     f"Not passing: {p['not_passing'][:20]}\n{p['tail'][-2500:]}")
    return "\n\n".join(parts)
