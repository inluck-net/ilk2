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
        # Not stale yet: a supervisor launchd is starting gets the start-up bound to its first
        # tick, counted from when this job first saw it un-ticked (§2.11.1). Kickstarting at
        # first sight killed the supervisor mid-start on the first real install (Oct 13).
        first = s.one("SELECT value FROM meta WHERE key = 'health_first_unticked'")
        if first is None:
            with s.tx() as c:
                c.execute("INSERT INTO meta(key, value) VALUES ('health_first_unticked', ?)",
                          (str(now_w),))
        elif now_w - float(first["value"]) > b.supervisor_startup:
            stale = f"never ticked in {b.supervisor_startup:.0f}s"
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
    out["rsi"] = rsi_health(s, now_w)
    out["notified"] = notify.due(s, b)
    return out


# RSI watched from outside RSI (docs/08 §8.6): a tick that stops or keeps failing is otherwise
# silent (Oct 16-19: the issue reader failed on every tick for 3 days and nothing said so).
RSI_STALE_S = 3 * 30 * 60   # 3 x the supervisor's RSI_EVERY_S (a test keeps the two in step)
RSI_FAILS = 3               # judgment call: 3 failed ticks in a row; wrong if one-off failures are common
RSI_HEALTH_CLEAR = "read ~/.ilk2/data/logs/rsi.log; `ilk2 rsi tick` runs one pass by hand"


def rsi_health(s: Store, now: float) -> dict:
    """Open one rsi-health row when the ilk2 project's rsi_auto is on and no RSI tick has
    completed in RSI_STALE_S, or the last RSI_FAILS ticks failed; clear it otherwise. The grace
    period counts from when this job first saw rsi_auto on, so a release that introduces the
    tick events, or turning rsi_auto back on, does not alarm at once."""
    from . import rsi
    row = s.one("SELECT config FROM projects WHERE id = ?", rsi.PROJECT)
    on = bool(row and json.loads(row["config"] or "{}").get("rsi_auto"))
    since_row = s.one("SELECT value FROM meta WHERE key = 'rsi_health_since'")
    if not on:
        if since_row:
            with s.tx() as c:
                c.execute("DELETE FROM meta WHERE key = 'rsi_health_since'")
        s.clear_needs_human_reason("rsi-health", rsi.PROJECT)
        return {"on": False}
    if since_row is None:
        with s.tx() as c:
            c.execute("INSERT INTO meta(key, value) VALUES ('rsi_health_since', ?)", (str(now),))
        since = now
    else:
        since = float(since_row["value"])
    last = s.one("SELECT MAX(at) at FROM events WHERE kind = 'rsi.tick-completed'")["at"]
    stale = now - max(since, last or 0.0) > RSI_STALE_S
    outcomes = _rsi_outcomes(s, now, rsi.PLAN_CAP_S)
    failing = len(outcomes) >= RSI_FAILS and all(o == "failed" for o in outcomes[:RSI_FAILS])
    if stale or failing:
        why = (f"no RSI tick has completed in {RSI_STALE_S // 60} min" if stale
               else f"the last {RSI_FAILS} RSI ticks failed")
        s.open_needs_human("rsi-health", why, RSI_HEALTH_CLEAR, project_id=rsi.PROJECT)
    else:
        s.clear_needs_human_reason("rsi-health", rsi.PROJECT)
    return {"on": True, "stale": stale, "failing": failing, "last_completed": last}


def _rsi_outcomes(s: Store, now: float, plan_cap_s: float) -> list[str]:
    """The latest ticks' outcomes, newest first: completed or failed. A started tick with no
    ending counts as failed once it is older than 2 x plan_cap_s; younger, it is still running."""
    rows = s.all("SELECT kind, payload, at FROM events WHERE kind IN ('rsi.tick-started', "
                 "'rsi.tick-completed', 'rsi.tick-failed') ORDER BY cursor DESC LIMIT 60")
    ended: dict[str, str] = {}
    for r in rows:
        tid = json.loads(r["payload"] or "{}").get("tick_id")
        if r["kind"] != "rsi.tick-started" and tid:
            ended.setdefault(tid, "completed" if r["kind"] == "rsi.tick-completed" else "failed")
    out = []
    for r in rows:
        if r["kind"] != "rsi.tick-started":
            continue
        tid = json.loads(r["payload"] or "{}").get("tick_id")
        if tid in ended:
            out.append(ended[tid])
        elif now - r["at"] > 2 * plan_cap_s:
            out.append("failed")
    return out


def main() -> int:
    paths.ensure()
    s = Store(paths.db())
    try:
        check(s)
    finally:
        s.close()
    return 0
