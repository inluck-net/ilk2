# Evidence: batch 18a's retrospective (Oct 18)

Batch `b-971382e9fef7` (`v1:MASTER-2026-10-18a-execution-plan`): gh-resolve's own V1 retirement (docs/07 §7.4.4), on chad-mbp, worker model `mimo-v2.6-pro`. Planned with V1's `/ilk-plan` before the ilk2-native planner existed. Chad asked for this retrospective on Oct 18 because the batch ran far longer than gh-resolve's usual batches. The question: what could ilk2 have done to make it faster?

Sources:
- `ilk2 report --batch b-971382e9fef7` and `--json` (the per-attempt records);
- `uv run python tools/batch_time.py b-971382e9fef7`;
- the store's attempts and events;
- for comparison, the same tools on batch 17e (`b-81176d1900ea`) and across gh-resolve's 26 other shipped batches.

## The numbers

| | 18a | 17e (same project, same week) | Median of gh-resolve's other batches |
|---|---|---|---|
| Submitted to shipped | **13.06 h** | 7.15 h | 1.9 h (75th percentile 2.9) |
| Work steps | 20 | 20 | 2 |
| Minutes per work step (all attempts) | **39.1** (11.8-80.9) | 18.1 | 28.7 |
| First-attempt share (all steps) | 0.64 | 1.0 | — |
| Repair minutes | 47.4 (2 merge repairs; 6 test repairs at 0.1) | 0 | — |
| Verification step | **94.2 min, 3 attempts** | — | median 9.1 |
| Dispatch minutes / wall minutes | 884 / 784 (average 1.13 workers) | — | — |
| ilk2 outside the worker | 61 min (gates 38.8, base 11.7, merge 7.4, attribute 3.2) | — | — |

18a mostly deletes: about 5,000 lines removed, and 391 test functions moved off the V1 engine. Its inserted lines per step (603) understate its work, so inserted-line throughput is not used here.

## Where the time went

**1. The worker, on every step.**
- Model requests are 80-97% of almost every attempt; tools take 0.3-6.6 min per attempt.
- Long requests (more than 60 s) take 0-20 min per attempt; reconcile step 0 spent 20.1 of its 55.2 min in 13 of them.
- Turn speed does not track context size here: carried sessions at 400-507k tokens ran 14.1-19.1 s per turn, and fresh ones at 145-376k ran 11.6-28.2.

**2. Steps too large for the attempt cap: 2 timeouts, about 160 min.** Two steps hit the 3600 s cap mid-work: `a-runs-plan-is-found-in-ilk2-or-standalone` 0 and `a-workers-plan-names-no-v1-script` 2. Each kept its work and resumed in about 20 min, so each step cost about 80 min in all. A third step's gate went red once and was fixed in 6 min.

**3. The verification step found everything at once: 94.2 min.**
- Its first measurement found **44 new failures**. 42 were also failing at a step's parent ("possibly inherited"), and **0 were attributed**.
- The batch changed the gate's own specification (its test configuration), so ilk2 had no comparable base and charged every failure to the verification step (docs/02 §2.5.4).
- One worker then fixed them in 3 attempts (57.5 + 13.0 + 15.5 min). With a comparable base, attribution would have turned them into repair plans for the steps that caused them.
- These are the failures that V1's D9 rule (importer tests in every step's gate) would have caught step by step, at the cost of about a quarter of the suite per step (trial #1 feedback).

**4. Merges: 47.4 min, both `merge-red`.** Both lane merges applied cleanly as text, but the combined code failed the gates, so a worker repaired each (20.7 and 26.6 min). They were not text conflicts, so `union_merge` would not have helped.

**5. Little parallelism.** After the first three plans, every plan depended on the earlier ones (they all edit `cli.py`, `config.py` and the reap code). Average concurrency was 1.13.

**6. ilk2 itself: 61 min** of 13 h, most of it the verification gate's full-suite runs.

## What would have made it faster, ranked

| # | Lever | Owner | Evidence | Estimated effect on 18a | Status |
|---|---|---|---|---|---|
| 1 | **Worker model** (`mimo-v2.5-pro` against `mimo-v2.6-pro`) | ilk2 setting (built `57ceb30`); the model is Chad's decision | V1, Sep 23: 8.6 against 22.1 s per turn over a week (n=42/54 iterations); a trial on v2.5 ran about 3 min per step commit, against 12-22. Oct 10 bench and Oct 18 probe agree. Quality not measured | Model time is 80-97% of each attempt; at 2.5x per turn, plausibly 13 h → 5-7 h | A/B on ilk-skills planned; why V1 returned to v2.6 after Sep 23 is unrecorded (asked Chad) |
| 2 | **Smaller steps** (planner) | ilk2-native planner, on trial | 17e: same 20 steps at 18.1 min each, 7.15 h | Fewer cap timeouts (about 160 min here); shorter steps | Trial batches 18b (converted) and 18c (native) next |
| 3 | **Attribute failures when the gate's specification changed** | ilk2 | 44 failures, 0 attributed, all on one verification worker: 86 min of attempts | Would turn them into repairs per causing step, smaller and with their own context. Hypothesis: whether that is faster than one worker fixing all 44 is unmeasured | To design: measure the base under the head's configuration as well, so failures can still be attributed when the batch changes its test configuration |
| 4 | **Extend the attempt cap once while the worker is progressing** | ilk2 | 2 cap hits; each second attempt took about 20 min, including re-orientation | Perhaps 10-20 min per hit | Approved; build next |
| 5 | **Lanes for plans that edit the same module** | ilk2 | 2 `merge-red` repairs, 47.4 min; concurrency 1.13 | Small either way here: fewer merge repairs if serialised, more parallelism if not | Leave as is; revisit with smaller steps |

Not levers here: ilk2's orchestration (61 min), context growth in carried sessions (no relation to turn speed in this batch), and the six single-test repairs (0.1 min in all; grouped per edge since `d4d2651`).

## Caveats

- One batch, and an unusual one: it deletes far more than it adds and changes its own test configuration.
- Item 1's effect assumes v2.5's per-turn speed carries over to ilk2's step shape and quality holds. The A/B is the test.
- Item 3's effect is a hypothesis. It is not built, and its cost (a second base measurement under the head's configuration) is not measured.
