"""The ilk2 menu bar item for SwiftBar/xbar (`ilk2 xbar`; plugin: contrib/xbar/ilk2.10s.sh).

Title, by what needs attention first:
  ilk2 ✗    the supervisor is not ticking (or never ticked)
  ilk2 ⚠ N  N batches need you: an open needs_human row, or blocked
  ilk2 ▶ N  N batches running
  ilk2 ok   nothing running, nothing waiting
Pages stay opt-in (Q12); this item is the passive view (docs/06 "Self-resolution").
"""

from __future__ import annotations

import shlex

LIVE = ("queued", "active")


def _trunc(s: str, n: int = 70) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _current_step(b: dict) -> dict | None:
    for p in b.get("plans", []):
        for st in p.get("steps", []):
            if st.get("status") not in ("accepted", "cancelled"):
                return st
    return None


def _mins(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    return f"{int(seconds // 60)}m" if seconds >= 60 else f"{int(seconds)}s"


def _steps(b: dict) -> list[dict]:
    return [st for p in b.get("plans", []) for st in p.get("steps", [])]


def live_runs(s) -> dict[str, dict]:
    """batch id -> the live run's phase, its age, and the running attempt's age."""
    from . import clock
    out = {}
    for r in s.all("SELECT * FROM runs WHERE status IN ('starting', 'running')"):
        same_boot = r["boot_time"] in (None, clock.boot_time())
        phase_age = (clock.mono() - r["phase_started_mono"]) if (same_boot and r["phase_started_mono"]) else None
        a = s.one("SELECT created_at FROM attempts WHERE run_id = ? AND status = 'running' "
                  "ORDER BY created_at DESC LIMIT 1", r["id"])
        info = {"phase": r["phase"] or "starting", "phase_age_s": phase_age,
                "attempt_age_s": (clock.wall() - a["created_at"]) if a else None}
        plan = s.one("SELECT slug FROM plans WHERE id = ?", r["plan_id"]) if "plan_id" in r.keys() \
            and r["plan_id"] else None
        cur = out.setdefault(r["batch_id"], {**info, "lanes": []})
        if plan:
            cur["lanes"].append(plan["slug"])  # lanes: several live runs on one batch
    return out


def render(st: dict, tick_age: float | None, stale: float) -> str:
    batches = st.get("batches", [])
    rows = st.get("needs_human", [])
    by_batch: dict[str | None, list[dict]] = {}
    for r in rows:
        by_batch.setdefault(r.get("batch_id"), []).append(r)
    attention = [b for b in batches if b["status"] == "blocked" or b["id"] in by_batch]
    hostwide = by_batch.get(None, [])
    running = [b for b in batches if b["status"] in LIVE and b["id"] not in by_batch]
    n_need = len(attention) + len(hostwide)

    if tick_age is None or tick_age > stale:
        title = "ilk2 ✗ | color=red"
    elif n_need:
        title = f"ilk2 ⚠ {n_need} | color=orange"
    elif running:
        title = f"ilk2 ▶ {len(running)}"
    else:
        title = "ilk2 ok"
    out = [title, "---"]
    if tick_age is None:
        out.append("supervisor has never ticked | color=red")
    elif tick_age > stale:
        out.append(f"supervisor last ticked {tick_age:.0f}s ago (bound {stale:.0f}s) | color=red")

    def clear_lines(r: dict) -> list[str]:
        cmd = r.get("clear_command") or ""
        lines = [f"--clear: {_trunc(cmd, 90)} | font=Menlo size=11"]
        if cmd:
            lines.append(f"--copy the clear command | bash=/bin/sh param1=-c "
                         f"param2={shlex.quote('printf %s ' + shlex.quote(cmd) + ' | pbcopy')} "
                         "terminal=false")
        return lines

    out.append(f"⚠ needs you ({n_need})" + ("" if n_need else "  — none"))
    for b in attention:
        ref = f" [{b['consumer_ref']}]" if b.get("consumer_ref") else ""
        why = by_batch.get(b["id"]) or []
        head = why[0]["reason"] if why else (b.get("blocked_reason") or "blocked")
        out.append(f"{b['id']}{ref}  {b['project']}: {_trunc(head, 60)}")
        for r in why:
            out.append(f"--{_trunc(r['detail'], 100)}")
            out += clear_lines(r)
        if not why and b.get("blocked_reason"):
            out.append(f"--{_trunc(b['blocked_reason'], 100)}")
    for r in hostwide:
        out.append(f"host: {r['reason']}: {_trunc(r['detail'], 60)}")
        out += clear_lines(r)

    out += ["---", f"▶ running ({len(running)})" + ("" if running else "  — none")]
    for b in running:
        ref = f" [{b['consumer_ref']}]" if b.get("consumer_ref") else ""
        steps = _steps(b)
        cur = _current_step(b)
        parts = []
        if cur:
            # Done of total, not the current step's position: repair plans sort first, so a
            # position read "step 2/15" with 12 of 15 accepted (Chad, Oct 14).
            done = sum(1 for x in steps if x.get("status") == "accepted")
            plan = next((p.get("slug") for p in b.get("plans", []) if cur in p.get("steps", [])), None)
            repairs = sum(1 for p in b.get("plans", []) if p.get("repair_of") for _ in p.get("steps", []))
            total = f"{done}/{len(steps)} done" + (f" (+{repairs} repair{'s' if repairs > 1 else ''})"
                                                     if repairs else "")
            parts += [total] + ([plan] if plan else []) + \
                [f"attempt {max(cur.get('attempts') or 1, 1)}"]
        live = b.get("live")
        lanes = (live or {}).get("lanes") or []
        if len(lanes) > 1:
            parts.append(f"{len(lanes)} lanes")
        if live:
            parts.append(f"{live['phase']} {_mins(live.get('phase_age_s'))}")
        elif b["status"] == "queued":
            parts.append("queued")
        out.append(f"{b['id']}{ref}  {b['project']} · " + " · ".join(parts))
        lane_cur = [next((x for x in p.get("steps", []) if x.get("status") != "accepted"), None)
                    for p in b.get("plans", []) if p.get("slug") in lanes]
        for st_ in steps:
            mark = {"accepted": "✓", "blocked": "✗", "cancelled": "–"}.get(
                st_["status"], "▶" if st_ is cur or any(st_ is x for x in lane_cur) else "·")
            commit = f"  ({st_['commit'][:7]})" if st_.get("commit") else ""
            out.append(f"--{mark} {_trunc(st_['title'], 70)}{commit}")

    done = [b for b in batches if b["status"] in ("shipped", "cancelled")][:3]
    if done:
        out += ["---", "recent"]
        for b in done:
            ref = f" [{b['consumer_ref']}]" if b.get("consumer_ref") else ""
            out.append(f"{b['id']}{ref}  {b['project']}: {b['status']} | color=gray")
    out += ["---", "Refresh | refresh=true"]
    return "\n".join(out) + "\n"


def main() -> int:
    """Build the menu from the live store. Never raises: a menu item must always render."""
    try:
        from . import bounds, clock, paths
        from .cli import status
        from .store import Store
        s = Store(paths.db())
        try:
            st = status(s, None)
            live = live_runs(s)
            for b in st["batches"]:
                if b["id"] in live:
                    b["live"] = live[b["id"]]
            row = s.one("SELECT tick_mono, tick_wall, boot_time FROM supervisor WHERE id = 1")
            tick_age = None
            if row and row["tick_wall"] is not None:
                same_boot = row["boot_time"] == clock.boot_time()
                tick_age = (clock.mono() - row["tick_mono"]) if same_boot else (clock.wall() - row["tick_wall"])
        finally:
            s.close()
        print(render(st, tick_age, bounds.load().supervisor_stale), end="")
    except Exception as e:  # noqa: BLE001 - show the failure in the menu bar, not a blank item
        print(f"ilk2 ✗ | color=red\n---\nilk2 xbar failed: {type(e).__name__}: {_trunc(str(e), 90)}")
    return 0
