"""P1 step 1 probe: does SIGKILL of the worker's process group reach Bash-tool children?

Claude Code's Bash tool starts its shell in a new process group (seen in step1-20261013-022428),
so this checks whether those children outlive a SIGKILL that skips Claude Code's own cleanup.
First run (killpg of the worker group only): zsh and sleep survived. Now runs cancel(term_first=False).
"""
import json, os, signal, sys, time
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from cli_adapter import CliAdapter, descendants, make_config_dir
from run_step1 import CANCEL_BRIEF

run = Path.home() / ".ilk2" / "spike-p1" / f"sigkill-{datetime.now():%Y%m%d-%H%M%S}"
run.mkdir(parents=True, mode=0o700)
wt = run / "wt"; wt.mkdir()
a = CliAdapter(make_config_dir(run / "config"), run).start(CANCEL_BRIEF, wt)
for _ in a.events():
    if "Bash" in a.tool_uses:
        break
row = None
for _ in range(120):
    row = next((r for r in descendants(a.pgid) if r[2] == "sleep"), None)
    if row: break
    time.sleep(0.5)
before = descendants(a.pgid)
res = CliAdapter.cancel(a, term_first=False)
def alive(pid):
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False
out = {"pgid": a.pgid, "before": before, "sleep": row, "cancel": res,
       "alive_after": {f"{c}:{p}": alive(p) for p, _, c in before}}
(run / "summary.json").write_text(json.dumps(out, indent=2))
print(json.dumps(out))
print("SIGKILL-PATH", "PASS" if row and not any(out["alive_after"].values()) and not res["survivors"] else "FAIL")
