"""Progress bounds (docs/02 §2.11.1, judgment calls listed in §2.11.5; Q7, Q11).

All in seconds of monotonic time. ILK2_TIME_SCALE (a float, tests only) divides every value,
so the stall suite can run the real logic in seconds; ILK2_BOUNDS (JSON, tests only) then
overrides single values, e.g. {"attempt_cap": 5}.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class Bounds:
    attempt_cap: float = 60 * 60          # Q7
    dispatch_grace: float = 5 * 60        # attempt cap + 5 min
    run_outer_extra: float = 15 * 60      # any run: attempt cap + 15 min without progress
    phase: float = 10 * 60                # prepare, scope check, accept
    spawn: float = 2 * 60                 # driver spawned -> first phase event
    gate_grace: float = 5 * 60
    tick: float = 30
    health_interval: float = 60
    supervisor_stale: float = 3 * 60
    supervisor_startup: float = 5 * 60
    term_grace: float = 30
    kill_grace: float = 60
    api_backoff_first: float = 60
    api_backoff_max: float = 30 * 60
    api_budget_per_run: float = 2 * 60 * 60
    api_needs_human: float = 6 * 60 * 60
    api_probe: float = 30 * 60
    quota_max: float = 24 * 60 * 60
    quota_probe: float = 60 * 60
    renotify_first: float = 4 * 60 * 60
    renotify_every: float = 12 * 60 * 60
    consumer_offhost: float = 24 * 60 * 60
    lease: float = 30 * 60
    setup_cap: float = 15 * 60
    host_slept_gap: float = 60
    load_timeout_window: float = 30 * 60
    attribution_wall: float = 20 * 60     # the whole attribute phase (§2.5.7)


ATTEMPTS_PER_STEP = 3        # Q6
RESTARTS_PER_STEP = 3
KICKSTARTS_PER_HOUR = 3
LOAD_TIMEOUTS = 3            # gate timeouts in the window that refuse new runs
PROBES_PER_FAILURE = 12      # §2.5.7: confirmation probes included, cached probes not


def load() -> Bounds:
    scale = float(os.environ.get("ILK2_TIME_SCALE", "1") or 1)
    b = Bounds()
    vals = {f.name: getattr(b, f.name) / scale for f in fields(Bounds)}
    vals.update(json.loads(os.environ.get("ILK2_BOUNDS") or "{}"))
    return Bounds(**vals)
