"""Telling a human (docs/02 §2.11.3; Q12).

The durable record is the needs_human row; delivery is best effort and never proof.
- `owner` policy: macOS notification plus one off-host channel, re-sent after 4 h, then
  every 12 h.
- `consumer` policy: the event only; one off-host notice if still open after 24 h.
The off-host channel is still open in Q12 (Lark bot if the app can send, else Teams), so it is
a configured command in ~/.ilk2/notify.json: {"offhost_command": ["/path/to/send", "{text}"]}.
ILK2_NOTIFY=log writes deliveries to a log file only (tests, and hosts without a GUI session).
"""

from __future__ import annotations

import json
import os
import subprocess

from . import batches, bounds, clock, paths
from .store import Store


def _log(text: str) -> None:
    paths.logs().mkdir(parents=True, exist_ok=True)
    with (paths.logs() / "notify.log").open("a") as f:
        f.write(json.dumps({"at": clock.wall(), "text": text}) + "\n")


def _macos(title: str, text: str) -> None:
    if os.environ.get("ILK2_NOTIFY") == "log":
        _log(f"[macos] {title}: {text}")
        return
    script = f"display notification {json.dumps(text[:200])} with title {json.dumps(title)}"
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


def _offhost(text: str) -> None:
    if os.environ.get("ILK2_NOTIFY") == "log":
        _log(f"[offhost] {text}")
        return
    cfg = paths.home() / "notify.json"
    if not cfg.exists():
        _log(f"[offhost: no channel configured] {text}")
        return
    cmd = json.loads(cfg.read_text()).get("offhost_command")
    if cmd:
        subprocess.run([a.replace("{text}", text[:1000]) for a in cmd], capture_output=True,
                       timeout=30)


def policy_of(s: Store, row) -> str:
    """A row's project policy. Rows with no project (health, an expired consumer lease) page
    only if this host opted in with ~/.ilk2/notify.json."""
    if not row["project_id"]:
        return "owner" if (paths.home() / "notify.json").exists() else "record"
    try:
        return batches.project_config(s, row["project_id"]).notify_policy
    except Exception:  # noqa: BLE001 - a missing project records only
        return "record"


def due(s: Store, b: bounds.Bounds | None = None) -> int:
    """Deliver every needs_human notice that is due. Returns how many were sent."""
    b = b or bounds.load()
    now = clock.wall()
    sent = 0
    for row in s.open_human_rows():
        pol = policy_of(s, row)
        n, last = row["notify_count"], row["last_notified_at"]
        text = f"ilk2 {row['reason']}: {row['detail'][:300]} | clear: {row['clear_command']}"
        if row["consumer_ref"]:
            text = f"[{row['consumer_ref']}] {text}"
        send = None
        if pol == "owner":
            if n == 0:
                send = "first"
            elif n == 1 and now - last >= b.renotify_first:
                send = "renotify"
            elif n >= 2 and now - last >= b.renotify_every:
                send = "renotify"
        elif pol == "consumer" and n == 0 and now - row["created_at"] >= b.consumer_offhost:
            send = "consumer-unhandled"
        if send is None:
            continue
        if pol == "owner":
            _macos("ilk2 needs you", text)
            _offhost(text)
        else:
            _offhost(f"consumer has not handled this for 24 h: {text}")
        with s.tx() as c:
            c.execute("UPDATE needs_human SET notify_count = notify_count + 1, "
                      "last_notified_at = ? WHERE id = ?", (now, row["id"]))
        s.event("notified", {"id": row["id"], "kind": send, "policy": pol},
                batch_id=row["batch_id"])
        sent += 1
    return sent
