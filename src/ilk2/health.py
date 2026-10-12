"""The health job (docs/02 §2.2, §2.11.1): a separate launchd job, every 60 s.

- If the supervisor's last tick is older than its bound, or its process is gone, restart it
  with `launchctl kickstart -k`. Three kickstarts within an hour create a needs_human row.
- Deliver due needs_human notices (first sends and re-sends), per project policy.
ILK2_KICKSTART_CMD (JSON list) replaces the launchctl command, for tests.
"""

from __future__ import annotations

import json
import os
import subprocess

from . import bounds, clock, notify, paths, procs
from .store import Store

LABEL = "net.inluck.ilk2.supervisor"


def kickstart_cmd() -> list[str]:
    if os.environ.get("ILK2_KICKSTART_CMD"):
        return json.loads(os.environ["ILK2_KICKSTART_CMD"])
    return ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"]


def check(s: Store, b: bounds.Bounds | None = None) -> dict:
    b = b or bounds.load()
    row = s.one("SELECT * FROM supervisor WHERE id = 1")
    now_w, now_m = clock.wall(), clock.mono()
    stale = None
    if row is None or row["tick_wall"] is None:
        stale = "never ticked"
    else:
        same_boot = row["boot_time"] == clock.boot_time()
        age = (now_m - row["tick_mono"]) if same_boot else (now_w - row["tick_wall"])
        started_age = now_w - (row["started_wall"] or 0)
        bound = b.supervisor_startup if started_age < b.supervisor_startup else b.supervisor_stale
        if not procs.is_same_process(row["pid"], row["proc_start"]):
            stale = "process gone"
        elif age > bound:
            stale = f"last tick {age:.0f}s ago"
    out = {"stale": stale, "kickstarted": False}
    if stale:
        subprocess.run(kickstart_cmd(), capture_output=True, timeout=60)
        kicks = json.loads(row["kickstarts"] or "[]") if row else []
        kicks = [k for k in kicks if now_w - k < 3600] + [now_w]
        with s.tx() as c:
            c.execute("UPDATE supervisor SET kickstarts = ? WHERE id = 1", (json.dumps(kicks),))
        s.event("health.kickstart", {"why": stale, "in_last_hour": len(kicks)})
        out["kickstarted"] = True
        if len(kicks) >= bounds.KICKSTARTS_PER_HOUR:
            s.open_needs_human("health", f"supervisor kickstarted {len(kicks)} times in an hour "
                               f"({stale})", "check ~/.ilk2/data/logs/supervisor.log")
    out["notified"] = notify.due(s, b)
    return out


def main() -> int:
    paths.ensure()
    s = Store(paths.db())
    try:
        check(s)
    finally:
        s.close()
    return 0
