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
    # Self-resolution (docs/06): a crash loop backs off before it asks a human. Judgment call:
    # 15 min then 1 h; wrong if crash loops are nearly always deterministic (they then reach
    # the human 75 min later than before).
    events_keep: float = 30 * 24 * 60 * 60  # §2.8: events are kept 30 days
    # Judgment call (Oct 14): a finished batch's worktree, diag worktrees and run temp dirs go
    # 7 days after it ends (kira: ~7 GB per batch); its branch stays. Wrong if anyone inspects
    # finished worktrees by path after a week.
    worktrees_keep: float = 7 * 24 * 60 * 60
    crash_backoff_first: float = 15 * 60
    crash_backoff_second: float = 60 * 60


ATTEMPTS_PER_STEP = 3        # Q6
# Self-resolution (docs/06, Chad Oct 13): after 3 reds, one escalation round before a human:
# attempt 4 is a fresh session briefed with every earlier attempt's evidence, attempt 5
# resumes it. Judgment call: 2 more; wrong if escalated attempts rarely succeed (ilk2 report).
ESCALATION_ATTEMPTS = 2
# A verification step's repair rounds: each round opens repairs, they ship, and the full suite
# runs again (docs/09 V2). Judgment call: 3 rounds; wrong if real batches need more to converge
# (the block keeps the last full-suite evidence for the person who clears it).
VERIFY_REPAIR_ROUNDS = 3
RESUMED_ATTEMPTS = (2, ATTEMPTS_PER_STEP + 2)   # each resumes the attempt just before it
RESTARTS_PER_STEP = 3               # a backoff after every 3 restarts of one step ...
RESTARTS_BEFORE_HUMAN = 3 * RESTARTS_PER_STEP   # ... and a human after the third round
KICKSTARTS_PER_HOUR = 3
LOAD_TIMEOUTS = 3            # gate timeouts in the window that refuse new runs
PROBES_PER_FAILURE = 12      # §2.5.7: confirmation probes included, cached probes not


def load() -> Bounds:
    scale = float(os.environ.get("ILK2_TIME_SCALE", "1") or 1)
    b = Bounds()
    vals = {f.name: getattr(b, f.name) / scale for f in fields(Bounds)}
    vals.update(json.loads(os.environ.get("ILK2_BOUNDS") or "{}"))
    return Bounds(**vals)
