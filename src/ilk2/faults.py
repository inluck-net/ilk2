"""Named fault-injection points for the stall suite (docs/02 §2.11.4).

ILK2_FAULTS is a comma-separated list of point names; a listed point acts when reached:
`<name>` kills this process (SIGKILL, as a crash would), `<name>=hang` blocks forever.
Unset in production, so every call is a dictionary lookup that does nothing.
"""

from __future__ import annotations

import os
import signal
import time


def _points() -> dict[str, str]:
    out = {}
    for item in filter(None, (os.environ.get("ILK2_FAULTS") or "").split(",")):
        name, _, mode = item.partition("=")
        out[name.strip()] = mode.strip() or "kill"
    return out


def hit(name: str) -> None:
    mode = _points().get(name)
    if mode is None:
        return
    if mode == "hang":
        while True:
            time.sleep(3600)
    os.kill(os.getpid(), signal.SIGKILL)
