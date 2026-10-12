"""P1 step 1 live run: dispatch, resume with red evidence, cancel by process group.

Usage: uv run python spikes/p1/run_step1.py
Outputs go to ~/.ilk2/spike-p1/step1-<ts>/ (summary.json plus per-attempt stream logs).
At most one MiMo session runs at a time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cli_adapter import CliAdapter, descendants, make_config_dir  # noqa: E402

STUB = '''def slugify(text: str) -> str:
    raise NotImplementedError
'''

# Driver-held gate: the worker never sees it, so the first attempt is likely red.
GATE = '''import importlib.util, sys
spec = importlib.util.spec_from_file_location("slug", sys.argv[1] + "/slug.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cases = [
    ("Hello World", "hello-world"),
    ("  Hello,   World!  ", "hello-world"),
    ("a--b__c", "a-b-c"),
    ("Crème Brûlée", "creme-brulee"),
    ("", ""),
]
bad = 0
for arg, want in cases:
    try:
        got = m.slugify(arg)
    except Exception as e:
        got = f"<{type(e).__name__}: {e}>"
    if got != want:
        bad += 1
        print(f"FAIL slugify({arg!r}) == {got!r}, expected {want!r}")
print(f"{len(cases) - bad} passed, {bad} failed")
sys.exit(1 if bad else 0)
'''

BRIEF = (
    "In this repository, implement slugify(text) in slug.py: lowercase the text and turn "
    "spaces into hyphens. Edit only slug.py. Do not run git commit or git push."
)

RESUME = (
    "The driver ran its gate on your change and it is red. Fix slug.py so every case passes. "
    "Edit only slug.py. Do not run git commit or git push.\n\nGate output:\n{out}"
)

CANCEL_BRIEF = (
    "Use the Bash tool to run exactly this command in the foreground and wait for it to "
    "finish: sleep 600"
)


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


def run_gate(gate: Path, wt: Path) -> tuple[int, str]:
    p = subprocess.run([sys.executable, "-I", str(gate), str(wt)], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def seed(run: Path) -> tuple[Path, Path, Path]:
    """Throwaway origin repo with the slug stub, a worktree on branch `attempt`, and the gate."""
    origin, wt, gate = run / "origin", run / "wt", run / "gate.py"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    (origin / "slug.py").write_text(STUB)
    sh("git", "add", ".", cwd=origin)
    sh("git", "-c", "user.name=ilk2-spike", "-c", "user.email=spike@ilk2.invalid",
       "commit", "-q", "-m", "seed", cwd=origin)
    sh("git", "worktree", "add", "-q", "-b", "attempt", str(wt), cwd=origin)
    gate.write_text(GATE)
    return origin, wt, gate


def main() -> int:
    run = Path.home() / ".ilk2" / "spike-p1" / f"step1-{datetime.now():%Y%m%d-%H%M%S}"
    run.mkdir(parents=True, mode=0o700)
    origin, wt, gate = seed(run)

    adapter = CliAdapter(make_config_dir(run / "config"), run)
    summary: dict = {"run_dir": str(run)}

    # 1. Dispatch.
    a1 = adapter.start(BRIEF, wt)
    a1.wait(timeout=1800)
    rc1, out1 = run_gate(gate, wt)
    summary["start"] = a1.summary() | {"gate_rc": rc1, "gate_out": out1}
    print("start:", json.dumps(summary["start"]), flush=True)

    # 2. Resume the same session with the red evidence.
    a2 = adapter.resume(a1.session_id, RESUME.format(out=out1), wt)
    a2.wait(timeout=1800)
    rc2, out2 = run_gate(gate, wt)
    summary["resume"] = a2.summary() | {"gate_rc": rc2, "gate_out": out2}
    summary["resume"]["same_session_id"] = a2.session_id == a1.session_id
    print("resume:", json.dumps(summary["resume"]), flush=True)
    summary["head_unchanged"] = sh("git", "rev-parse", "HEAD", cwd=wt) == sh(
        "git", "rev-parse", "main", cwd=origin)

    # 3. Cancel by process group while a tool subprocess is running.
    a3 = adapter.start(CANCEL_BRIEF, wt)
    sleep_row = None
    for ev in a3.events():
        if "Bash" in a3.tool_uses:
            break
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and sleep_row is None:
        sleep_row = next((r for r in descendants(a3.pgid) if r[2] == "sleep"), None)
        time.sleep(0.5)
    before = descendants(a3.pgid)
    result = CliAdapter.cancel(a3)
    sleep_alive = None
    if sleep_row:
        try:
            os.kill(sleep_row[0], 0)
            sleep_alive = True
        except ProcessLookupError:
            sleep_alive = False
    summary["cancel"] = {
        "pgid": a3.pgid,
        "session_id": a3.session_id,
        "init_model": a3.init_model,
        "tool_uses": a3.tool_uses,
        "descendants_before": [{"pid": p, "pgid": g, "comm": c} for p, g, c in before],
        "outside_group_before": [c for _, g, c in before if g != a3.pgid],
        "sleep": {"pid": sleep_row[0], "pgid": sleep_row[1]} if sleep_row else None,
        "sleep_alive_after": sleep_alive,
        "descendants_after": [list(r) for r in descendants(a3.pgid)],
    } | result
    print("cancel:", json.dumps(summary["cancel"]), flush=True)

    (run / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    ok = (
        rc2 == 0
        and summary["resume"]["same_session_id"]
        and summary["start"]["init_model"] == "mimo-v2.6-pro"
        and sleep_row is not None
        and sleep_alive is False
        and not result["survivors"]
    )
    print("STEP1", "PASS" if ok else "FAIL", run)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
