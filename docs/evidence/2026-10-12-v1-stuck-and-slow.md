# Evidence: where V1's time goes (stuck and slow)

Measured Oct 12, 2026 on chad-mbp by Claude Code session ilk2-design. rezmac was not measured. These numbers back the two design changes recorded on Oct 12: refusal accounting (invariant I10) and worker time in scope ([document 3](../03-v1-vs-v2.md) §3.4).

## 1. Stuck: queued work the scheduler refused

**Window:** Sep 15 to Oct 12, 2026.

**Sources:**
- `~/.ilk-data/logs/scheduler.log` (31,628 lines). It holds one decision per tick, and the tick interval has a median of 5.0 min over 6,711 idle-tick intervals.
- Each project's `logs/.ilk-loop.log` under `~/.ilk-data/projects/<key>/`.

**Method:**
- Every `skip-*` or `no-progress-bound` line naming a project is one tick where that project had queued work and was not dispatched.
- Ticks are de-duplicated per project and per 5-minute bucket, then converted to hours (ticks × 5 min).
- `skip-busy` (a run was already active) is excluded.
- Iteration hours are the sum of `duration_sec` over every iteration record in the window.

| Hours | ilk-skills | gh-resolve |
|---|---:|---:|
| Inside iterations | 159 | 161 |
| **Queued but refused, total** | **123.3** | **123.9** |
| `skip-train-only` (release train not yet run) | 41.0 | 45.6 |
| `skip-permits` | 30.8 | 0.6 |
| `skip-blacklist` | 23.2 | 14.5 |
| `skip-audit-failed` | 12.7 | 45.4 |
| `no-progress-bound` | 10.9 | 13.7 |
| `skip-releasing` | 2.8 | 0.3 |
| `skip-held` (deliberate human hold) | 1.0 | 2.1 |
| `skip-awaiting-postmortem` | 0.9 | 1.7 |

**Readings:**
- About 0.8 h was refused for every hour worked, on both projects.
- Train, audit and permits are 84.5 h (69%) of ilk-skills' refused time and 91.6 h (74%) of gh-resolve's. All three first appear between Oct 5 and Oct 8. They are guards V1 added because the worker could change state (root cause 1) and gates could not be trusted (root cause 5).
- **Not measured:** whether each refusal was justified. These numbers show what the refusals cost, not that they were wrong.
- **Remaining gap time** (about 340 h for ilk-skills): mostly empty queues, which is a planning-side cause, not the engine's.

## 2. Slow: where an iteration's time goes

**Sample:** the 24 close records written on Oct 12 (09:57 to 23:07): 10 for ilk-skills and 14 for gh-resolve, 998 min of iteration wall time.

**Source fields:** `worker_cost_observation` and `driver_phase_observation`.

**Caveat:** these clocks overlap. Shares are each measured against wall time and must not be summed.

| Measure | Minutes | Share of 998 min |
|---|---:|---:|
| Model thinking (`thinking_completed_seconds`) | 560 | 56% |
| Before the first edit (`root_tool_span_before_first_edit_seconds`) | 416 | 42% |
| Worker-run tests (`foreground_test_seconds`) | 69 | 7% |
| Driver integrity and state audit (median 51 s per iteration) | 19 | 1.9% |

- **Iterations with 0 commits:** 6 of 24. That is 4 timeouts, 1 failed gate with no commits, and 1 inconclusive gate.
- **Longer history:** iterations with 0 commits are 13-18% of iterations per month (Jul to Oct) and 5-12% of iteration hours.
- **Within a run:** gaps between iterations have a median of 1.2 min, and total 24.9 h (ilk-skills) and 19.2 h (gh-resolve) since Sep 15.

**Reading:** per accepted step, worker time dominates. The engine's own overhead within a run is small. The largest controllable share is the 42% spent before the first edit, which is reading and exploring the repo.

## 3. What this changes in the design

- **Stuck:** V2's driver-owned shipping makes the train, audit and permit guards unnecessary. Invariant I10 requires every remaining refusal to name its clearing action and actor. Refused hours become a reported metric ([document 2](../02-architecture.md) §2.1, §2.9; [document 4](../04-v1-relation-and-migration.md) §4.5).
- **Slow:** the engine cannot make MiMo think faster, but it controls how much the worker has to discover: brief contents, step size, effort settings and model choice. Minutes to first edit becomes a measured target, starting in the P1 spike ([document 3](../03-v1-vs-v2.md) §3.4; [document 5](../05-open-questions.md) Q10).
