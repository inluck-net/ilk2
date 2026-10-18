# 8. Standing goals for RSI

**Status:** revision 6, Oct 19, 2026. Codex reviewed revisions 1-5 the same day, and every finding was checked against the code and accepted (8.9).
- **Stage A: accepted, and built Oct 19** (uncommitted until Chad's go; 8.10).
- **Stage B: rework in round 5.** Its fixes are folded in here, and it is split into **B1** (landing intent and lock) and **B2** (the goal ledger). Both wait for a sixth review.

**Asked by Chad, Oct 19:** "is the RSI still working? not a single auto improving batch for a few days", then "can we set this expected working RSI as the goal of the RSI itself, so that it can find all these defects/problems/reasons and get them handled properly".

**Two stages** (8.10):
- **Stage A** fixes the broken input, watches RSI from outside, and splits the daily allowances. It is small and does not depend on stage B.
- **Stage B** makes goals standing.

**Touches:** `src/ilk2/rsi.py`, `src/ilk2/rsi_land.py`, `src/ilk2/store.py` (a migration, two reasons, one store operation, indexes), `src/ilk2/report.py` (an internal range query), `src/ilk2/health.py`, `src/ilk2/cli.py` (`ilk2 rsi goals`), their tests, `docs/rsi/goals.json`, and docs/02 §2.11a. AGENTS.md lets an RSI batch change RSI machinery if it lands by review. This change is built by hand instead, as a choice: it redefines RSI's own lifecycle, and `goals.json` is governance.

## 8.1 What was measured (Oct 19, chad-mbp)

- **The trigger runs.** 208 `rsi.tick` events; the last one about 18 min before the check. `rsi_auto` and `rsi_land` are both on for project `ilk2`.
- **It has nothing to plan.** Every tick ends `{"did": "nothing", "why": "no open candidate", "found": 12, "new": 0, "reopened": 0}` (`~/.ilk2/data/logs/rsi.log`). Candidates ever: 13, of which 12 are `done` and 1 is `shelved`, all first seen Oct 14-15. Landings ever: 2, the last pushed Oct 16 23:19.
- **Chad's inputs are empty.** `docs/rsi/requests.json`: 2 entries, both landed. `docs/rsi/goals.json`: does not exist. GitHub issues labelled `rsi:req`: 0, any state.
- **One input has never worked under launchd.** `rsi.issues-failed`: 144 of 144 since Oct 16 06:20, every one `[Errno 2] No such file or directory: 'gh'`. `issues()` runs a bare `gh` (`src/ilk2/rsi.py:103`) on the supervisor's PATH, which lacks `/opt/homebrew/bin`.
- **The finder does not see where the time went.** 18a's 13.06 h with 8 timed-out attempts (E6), 18c's 128.5-min verification step, and the repeated ~15-min base measurements are not candidates: a step's own red attempts are excluded (`NOT_DEFECTS_NH`, `src/ilk2/rsi.py:35`), and no source measures step time. `ilk2 improvement list` has 0 rows.
- **Nothing watches RSI from outside.** `health.py` (`kickstart_cmd`, `check`, `main`) has 0 mentions of RSI. It does watch the supervisor itself (`src/ilk2/health.py:27-63`).

So the trigger is alive, and the finder is both starved and partly broken.

## 8.2 Why goals cannot carry this today

A goal is `{"id", "project", "metric", "max" or "min", "host"?}` (`goals()`, `src/ilk2/rsi.py:122-150`). When the metric is off target, the goal becomes candidate `goal:<id>` (weight 90), and the planner plans "the one step that most moves the metric" (`:326`). Six gaps:

1. **A goal fires once.** A `done` row stays done (`record()`, `src/ilk2/rsi.py:247-249`). Landing, `already_fixed` and `not_a_defect` all set `done` (`src/ilk2/rsi_land.py:201`; `src/ilk2/rsi.py:380-382`, `:524`).
2. **A goal can stick in `submitted`.**
   - Only a canary success or a rollback closes the row (`src/ilk2/rsi_land.py:201`, `:208`).
   - A cancelled batch only stops counting against the daily cap (`src/ilk2/rsi.py:428-432`).
   - A review hold is never revisited (`land()` resumes `merged` and `released` only, `src/ilk2/rsi_land.py:258`).
   - With `rsi_land` off, no landing row is written at all (`:254-255`).
3. **Only off-target goals are seen** (`src/ilk2/rsi.py:141-145`, `record()` at `:252`). A met or deleted goal is never looked at again.
4. **Metrics are all-time** (`src/ilk2/report.py:20-26`).
5. **Misses are silent.**
   - A metric that is not a top-level number returns `None`, and the goal is skipped (`src/ilk2/rsi.py:139-140`, `:165-166`).
   - A goal with no bound is never off target (`:141-142`).
   - Duplicate ids share a candidate (`:145`).
6. **Nothing reports RSI's own health.**
   - With `rsi_auto` off, the supervisor does not spawn (`src/ilk2/supervisor.py:131-133`).
   - A tick that raises, for example on a broken `requests.json` (`src/ilk2/rsi.py:77`), never writes `rsi_last_tick` (`:441-449`).

## 8.3 Decisions

| # | Decision | Stage | Kind |
|---|---|---|---|
| G1 | `issues()` resolves `gh` with `shutil.which`, falling back to `/opt/homebrew/bin/gh` and `/usr/local/bin/gh` | A | fix (8.1) |
| G5 | RSI's health is watched from outside RSI, and each input source reports its own result (8.6) | A | design |
| G6 | Separate allowances, counted from immutable batch metadata (8.7) | A | design |
| G2 | Goals are standing: an append-only round ledger, and state derived each tick (8.4) | B | design |
| G3 | A closed registry of goal metrics, measured by an internal range query (8.5). The public `ilk2 report` is unchanged | B | design |
| G4 | `goals.json` validated as a whole (8.4, "Validation") | B | design |
| G7 | The first goals (8.8) | B | content |

## 8.4 Goals (G2, G4)

**Validation.** `goals.json` is validated as a whole before any goal is measured. Each goal needs:
- a unique non-empty `id`;
- a registered `project`;
- a `metric` in the 8.5 registry;
- exactly one numeric `min` or `max`;
- a positive integer `window_days`;
- a positive integer `min_samples`;
- optionally, an integer `reset`.

A `host` field is rejected in this version: goals are measured on this host's store, and remote goals are deferred (8.11). A failing goal emits `rsi.goal-failed` with its id and the reason, and the other goals are still measured.

**The ledger: `rsi_goal_rounds`, append-only.** Each row has these fields:
- `id` and `goal_id`;
- `goal_json`: the goal's canonical definition at the start, `reset` included;
- `goal_hash` (of `goal_json`) and `registry_version`;
- `tolerance`: copied from the registry at the start;
- `tick_id`, `tick_pid` and `tick_proc_start`: the tick that started the round. `tick_proc_start` is `procs.start_time(os.getpid())` when the row is inserted, so that `procs.is_same_process` can reject a reused PID (`src/ilk2/procs.py:32-40`);
- `started_at`, `before_value`, `before_samples`;
- `batch_id`, `result_kind`, `detail`, `closed_at`;
- `after_value`, `after_samples`, `verdict`.

The row is inserted before planning starts. Each later field is written once. Every judgment of a round uses its own `goal_json`, never the current file. So editing or deleting a goal never changes how an earlier round is judged.

**Durable results.**
- The planner's answer (`already_fixed`, `not_a_defect`) or failure is written to the round first, before the candidate row is touched.
- A submitted batch carries `meta.rsi_goal_round_id` and `meta.rsi_source` from the moment of submission. A tick killed between submit and the round update is reattached on the next tick by that metadata.
- **Closing an orphaned round.** A round with neither a result nor a batch carrying its id closes as `plan-failed` only when both hold:
  - 2 × `PLAN_CAP_S` have passed;
  - the process that started it (`tick_pid`, matched by its start time) is gone.

  `batches.submit()` does validation, preflight and worktree setup before it inserts the batch (`src/ilk2/batches.py:287-344`), and a manual `ilk2 rsi tick` can run beside the supervisor's. Without the second condition, a concurrent tick could close a round that is still being submitted.
- **A late submit cannot attach to a closed round.** The check runs in the same `BEGIN IMMEDIATE` transaction as the batch insert (`src/ilk2/store.py:204-208`; `src/ilk2/batches.py:370`).
  - If the round has closed, that transaction inserts the batch already `cancelled`, reason `rsi-round-closed`, and commits.
  - The cancellation is finalized outside the transaction, because the store forbids non-SQL I/O inside one (`src/ilk2/store.py:6`).
  - A crash between the commit and finalization is picked up by the supervisor's existing retry of unfinished cancellations (`src/ilk2/supervisor.py:368`).
  - The worktree, created before the transaction (`src/ilk2/batches.py:344`), therefore always has an owning batch row, and follows the normal 7-day GC (`src/ilk2/supervisor.py:520`).

**Closing a round**, done first on every tick for every open round, whatever the current file says:

| The round's record shows | `result_kind` | Verdict when closed | `closed_at` |
|---|---|---|---|
| planner answered `already_fixed` / `not_a_defect` | `answered` | `miss` | the answer |
| planner failed, invalid output, submit rejected, or 2 × `PLAN_CAP_S` with nothing | `plan-failed` | `retry` | the failure |
| batch `cancelled` (a batch transition exists even before any landing row) | `cancelled` | `miss` | the transition |
| landing `verified` or `pushed` (written only after the canary passes) | `landed` | judged later | `verified_at` |
| landing `rolled_back` | `rolled-back` | `miss` | `rolled_back_at` |
| batch shipped, its branch is an ancestor of `main`, and its landing row is absent or `review` (a merge by hand; never `merge-interrupted`) | `landed-by-hand` | judged later | first tick that sees it |
| goal's current hash differs from the round's (Chad edited or reset it) while the round is held | `abandoned` | `miss` | that tick |
| batch shipped and not in `main`, landing `review` or none (`rsi_land` off); or landing `merge-interrupted`, in `main` or not | still open: **held** | | |
| anything else (planning, queued, running, `merging`, merged, released) | still open | | |

**Landing intent and lock (stage B1).** Today `merge()` commits to `main` and runs the suite before any landing row exists (`src/ilk2/rsi_land.py:153-162`). So an unattended merge mid-verification, or one that crashed there, looks exactly like a hand merge. `land()` also skips any batch with a row and picks the next one (`:258-277`), so with a second `ilk2 rsi land` (`src/ilk2/cli.py:573`), two merges could overlap. That breaks the rule of one change per canary. The design:
- **`rsi_landings` gains `merge_pid` and `merge_proc_start`** (today it has no process fields, `src/ilk2/store.py:135`).
- **`merging` is a global lock.** After the clean, kernel and test-removal checks, and immediately before `git merge`, one `BEGIN IMMEDIATE` transaction does two things:
  - it re-checks that no `merging`, `merged` or `released` landing exists;
  - it writes this batch's row as `merging`, with its own process identity.

  The existing flow then moves the row to `merged` or `review`. While a `merging` row's process is alive (`procs.is_same_process`), `land()` returns at once without considering any other batch.
- **A `merging` row whose process is gone becomes `merge-interrupted`**, never `review` and never a landing. `main` may already hold the unverified merge.
  - It is a hold: one `rsi-review` row says to verify and keep the merge, or to revert it.
  - `pending()` and `land()` treat it like `review`.
  - The round stays held until Chad resolves it. Reverting and resetting the goal closes the round as `abandoned`. Keeping the merge by setting the landing to `review` after verifying it by hand lets the next tick close it as `landed-by-hand`.

`landed-by-hand` therefore excludes `merging`, `merge-interrupted`, `merged` and `released`.

**A held round is resolved only by what Chad does**, and both actions leave an audit trail:
- **Merging the branch by hand** closes it as `landed-by-hand`.
- **Editing the goal, or bumping its `reset`** in `goals.json`, closes it as `abandoned`, and git records the edit. Abandoning also disposes of the branch. The batch's landing row is written or updated to `abandoned`, and its `rsi-review` row is cleared. `pending()` excludes `abandoned` landings, so abandoned branches no longer fill `REVIEW_CAP` (`src/ilk2/rsi.py:411-419`, `:492-494`). `land()` already skips any batch with a landing row (`src/ilk2/rsi_land.py:274-277`), so the branch can never land later by itself.

An abandoned round stays abandoned. If its branch is later merged by hand, the round is not credited after the fact; an `rsi.abandoned-merged` event records that it happened.

`ilk2 ack` closes nothing: it only means "seen" (`src/ilk2/store.py:384`).

**Judging a landed round.** `after_value` is the metric over samples whose clock time (8.5) is after `closed_at`, taken once `after_samples` reaches the round's `min_samples`. The verdict is `improved` if `after_value` is better than `before_value` by at least the round's own `tolerance`; otherwise it is `no-effect`. The query is the one for the round's `registry_version` (8.5). If that version is not supported, the round is `unjudgeable`: the goal plans nothing, and one `rsi-goal` row is open, until the implementation is restored or the goal is reset. Because only new samples count, old bad samples ageing out of a window cannot credit a fix.

The credit is **for the portfolio, not for the cause**: `improved` means "the metric improved after this round", not "this round's change caused it". Another goal's batch, or a consumer's own change, can move the same metric in the meantime. This is a choice: RSI cares that the system got better, and serializing every goal on a project would be slow. It is wrong if Chad wants each change's effect measured; then only one round may be judging per project and metric at a time.

**State of each goal, derived after closing** (first match wins). `open_round` is shown beside the state whenever a round is open:

| State | When | Plans? |
|---|---|---|
| `invalid` / `retired` | G4 rejects it / it is no longer in the file | no |
| `in-round` / `held` | a round of the current hash is open / held | no; `held` opens one `rsi-goal` row: "resolve the review: merge it, or reset the goal" |
| `judging` | the last round is landed and not yet judged | no ("needs N more samples") |
| `unjudgeable` | the last round's `registry_version` has no implementation | no; one `rsi-goal` row |
| `met` | the metric is on target | no; clears the goal's `rsi-goal` row |
| `stopped` | the last 2 judged rounds of the current hash are `no-effect`/`miss`, or the last 3 are `retry` | no; one `rsi-goal` row naming the rounds |
| `waiting` | fewer than `min_samples` new samples since the last round closed (`retry` waits 2 h instead) | no |
| `eligible` | otherwise | yes, subject to 8.7 |

`met` comes before `stopped`, so a goal that reaches target by other means is not left alarmed. A changed hash (an edit, or a `reset` bump) restarts the stop count, because only the current hash's rounds count. A `met` goal that drifts off target becomes `eligible` again with no stored state to update. Zero traffic means nothing to fix, so `waiting` has no time bound; `ilk2 rsi goals` shows each goal's state, value, sample count and open round.

**Judgment calls**, each checkable from the ledger:
- **2 judged misses in a row stop a goal.** Wrong if, among goals Chad restarts with only a `reset` bump, the next round is `improved` in more than 1 of 3 restarts.
- **3 `retry` rounds in a row, 2 h apart, stop a goal.** Wrong if more than 1 in 3 such stops are followed by a restart whose first round succeeds in planning.

## 8.5 The goal metric registry (G3)

An internal function, `report.metric(s, project, path, since, until, registry_version)`, returns `(value, samples)`. This version keeps registry version 1 permanently, and retiring a version is out of scope. A test asserts that version 1 is implemented. The `unjudgeable` state (8.4) is only the loud fallback for a missing implementation. It is used for `before_value` (the `window_days` before the round started), for `after_value` (after `closed_at`), and for `met`. Only these paths are goal metrics (`registry_version` 1):

| Path | Clock | Value | Samples | Tolerance |
|---|---|---|---|---|
| `minutes_per_accepted_step` | the step's transition to `accepted` (`transitions.at`; `steps` has no accepted time) | mean minutes of all attempts of those steps | those steps | 5% of `before_value` |
| `share_attempts_producing_nothing` | `attempts.ended_at` | as today's report | judged attempts | 0.02 |
| `human_transitions.per_batch` | `transitions.at`, `actor = 'human'` | human transitions ÷ project batches with any transition in the range | those batches | 0.1 |
| `rsi_input_failures_last_tick` (stage A's events) | the last `rsi.tick-completed` | failed sources in that tick | completed ticks | any decrease |

**New indexes in stage B:**
- `transitions(entity, to_state, at, entity_id)`;
- `transitions(actor, at, entity, entity_id)`;
- `attempts(ended_at, step_id)`;
- `rsi_goal_rounds(goal_id, started_at)`;
- `rsi_goal_rounds(goal_id, goal_hash, closed_at)`.

The `events` and `batches` indexes are in stage A (8.6). Today there are 3 indexes (`src/ilk2/store.py:144-146`).

Judgment calls:
- **Tolerances are about a tenth of each 8.8 target gap.** Wrong if a `no-effect` round's after-value proves stable at more than the tolerance (the next round's `before_value` shows it).
- **`window_days` 7.** gh-resolve had 172 accepted steps in 30 batches in the last 7 days. ilk2 itself had 8 accepted steps, too few for a step-time goal. Wrong if a 7-day value swings more than its target gap from day to day.

## 8.6 Watching RSI from outside (G5, stage A)

- **Each input source reports its own result.** Today `find()` runs every source in one call (`src/ilk2/rsi.py:237-239`):
  - a broken `requests.json` raises out of the whole tick (`:77`);
  - `issues()` swallows its error into an empty list (`:103-111`);
  - only remote hosts are caught one by one (`:479-483`).

  Each source (`requests`, `goals`, `issues`, `remote:<host>`, and the record-based finders as one) becomes a call returning `(candidates, error)`. A failed source is recorded and the others still run. A store or core failure still fails the whole tick.
- **Each tick has an id and records how it ended.** `rsi.tick-started {tick_id}` is recorded first, then exactly one of:
  - `rsi.tick-completed {tick_id, did, why, sources}`, with `sources`: `requests`, `goals`, `issues` and each `remote:<host>`, as `ok` or an error;
  - `rsi.tick-failed {tick_id, error}`, recorded by an outer `try`.

  A tick that does nothing is `completed`. `rsi_last_tick` stays as the panel's summary. Events are pruned after 30 days (`src/ilk2/bounds.py:47`), so health reads only recent ones.
- **When `rsi_auto` is on, `health.py` opens one `rsi-health` row** for project `ilk2` if either:
  - no `rsi.tick-completed` has been recorded in 3 × `RSI_EVERY_S` (90 min); or
  - the last 3 tick-ending events are all `failed`.

  A start with no ending within 2 × `PLAN_CAP_S` counts as failed. A later completed tick clears the row.
- **No false alarm at rollout or when re-enabled.** The new event kind does not exist until the first tick after release, and the health job runs every 60 s (`src/ilk2/health.py:1`). So health keeps `rsi_health_since` in `meta`:
  - it is set at the first observation with `rsi_auto` on, or seeded from a recent `rsi_last_tick.at`;
  - it is removed while `rsi_auto` is off;
  - the 90-minute rule counts from the later of `rsi_health_since` and the last completed tick.
- **Clearing.** A new store operation, `clear_needs_human_reason(reason, project_id, consumer_ref=None)`:
  - health clears by reason and project;
  - a goal clears only its own `rsi-goal` row, `consumer_ref = "goal:<id>"`.

  Today `clear_needs_human` clears by batch only (`src/ilk2/store.py:388-391`). `rsi-health` and `rsi-goal` join `NEEDS_HUMAN_REASONS` (`:38`).
- **An acked row is still active.** For `rsi-health` and `rsi-goal`, duplicate detection treats `open` and `acked` rows alike. Today it looks only at `open` (`src/ilk2/store.py:363-365`). So acknowledging an ongoing failure does not spawn a second row, and the clear operation clears both states.
- **Indexes for stage A:** `events(kind, cursor)` (health reads recent tick events every minute) and `batches(project_id, created_at)` (allowances).
- **What it covers.** RSI inside a live supervisor. A dead supervisor is already `health.py`'s job (`src/ilk2/health.py:27-63`), and its `health` row has a different reason, so the two rows never merge.
- **The machinery boundary.** The tick and health logic lives in `rsi.py` and `health.py`. `health.py` and its test join `MACHINERY` (`src/ilk2/rsi_land.py:38-39`), so an RSI batch cannot weaken the alarm without review.

## 8.7 Allowances (G6, stage A)

- **Every RSI batch records its kind at submission.** `meta.rsi_source` is the candidate's source. A host-prefixed id such as `rezmac/goal:x` (`src/ilk2/rsi.py:292`) keeps its real source this way, where reading the id's prefix would get it wrong.
- **Requests** are outside every cap, as today (`src/ilk2/rsi.py:498-503`).
- **Goals** get at most 1 batch per rolling 24 h.
- **Everything else (defects)** gets at most `DAILY_CAP` = 3, counted separately from goals.
- Both counts exclude cancelled batches, as `today_count` does (`:428-432`). Batches submitted before this change carry no `rsi_source`, and count as defects.
- **Selection** takes the highest-scored candidate whose own allowance is not used up (today it stops at a capped top candidate, `:495-503`). `REVIEW_CAP` still holds everything while 2 branches wait for review.

Judgment call: 1 goal batch a day. A landed round needs about a day of samples before it is judged anyway. Wrong if Chad sets several goals and wants them worked in parallel.

## 8.8 The first goals (G7)

Targets are fixed after stage B is built and `ilk2 rsi goals` measures the windowed values. Starting values are from the first review (cut by batch creation date):

| id | project · metric | window | `min_samples` | 7-day now | proposed target |
|---|---|---|---|---|---|
| `rsi-inputs-readable` | ilk2 · `rsi_input_failures_last_tick` | last tick | 1 | 1 (issues) | max 0 |
| `gh-resolve-step-minutes` | gh-resolve · `minutes_per_accepted_step` | 7 d | 20 | 30.79 | max 25 |
| `gh-resolve-hand-steering` | gh-resolve · `human_transitions.per_batch` | 7 d | 5 | 1.33 | max 1.0 |

Judgment calls:
- **Targets 20-25% under the measured value.** Wrong if the first 2 judged rounds on a goal are both `improved` but each closes less than a tenth of the gap.
- **`min_samples` 20 steps and 5 batches.** About a day of gh-resolve's traffic at this week's rate.

The "working RSI" check is in two parts. Stage A's alarm catches ticks that stop or fail. Stage B's `rsi-inputs-readable` goal catches an input RSI cannot read.

## 8.9 Review log

All findings below were checked against the code before being accepted.

**Round 1** (revision 1 → rework), 8 findings: a 7-day count as the health goal; an unbounded lifecycle; the window cut; goals crowding out defects; the touched-file list; silent invalid goals; time-based cooldown and missing falsifiers; wording. All addressed in revision 2.

**Round 2** (revision 2 → rework), 8 findings: no reconciliation of met or deleted goals; unreachable review exits; metric paths with no window; "toward target" undefined; planning failures and rollbacks missing; tick pairing and clearing; the "1 of 3" allowance; a wrong line cited. Revision 3 changed approach: no stored goal state.

**Round 3** (revision 3 → rework). Round-2 items 3 and 8 were resolved; the rest were partly resolved. New findings and where each is addressed:
1. Planner results and an interrupted planner had no durable per-round record. → durable results; the 2 × `PLAN_CAP_S` close; `meta.rsi_goal_round_id`.
2. `ack` treated as abandonment, and a merge by hand with no landing row matched no row. → `ack` closes nothing; `held`, resolved by a merge or a reset; `landed-by-hand` whether or not a landing row exists.
3. A hash cannot reconstruct the goal. → `goal_json` and `registry_version` per round.
4. `stopped` beat `met`, and validity could hide an open round. → rounds close before state is derived; `met` before `stopped`; `open_round` shown.
5. Portfolio credit, not cause. → stated, as a choice with its falsifier.
6. Clearing was not selective. → `consumer_ref` argument.
7. Host-prefixed ids were misclassified. → `meta.rsi_source`.
8. Indexes. → 8.5.
9. Too broad for one session. → two stages; public `report --since` cut; review inferred from `ack` cut; remote goals deferred.

**Round 4** (revision 4 → stage A "accept with changes", stage B "rework"). Round-3 items 4, 5, 7 and 9 were resolved; 1, 2, 3, 6 and 8 partly. New findings:
1. An unattended merge mid-verification matched `landed-by-hand`. → a `merging` landing row is written before `main` changes; `landed-by-hand` requires no row or `review`.
2. A reset closed the round but left the branch in the review queue and able to land. → an `abandoned` landing disposition, excluded by `pending()`.
3. The orphan timeout could race a live submit. → `tick_pid` liveness, plus an open-round check inside the submit transaction.
4. `registry_version` selected nothing. → a versioned `metric()`, `tolerance` per round, `unjudgeable`, and the hash/closure index.
5. (A) A false stale alarm at rollout. → `rsi_health_since`.
6. (A) An acked row caused duplicates. → `open` and `acked` both count as active.
7. (A) Per-source results needed a finder refactor. → the `(candidates, error)` contract.
8. (A) Stage A's indexes were listed under stage B. → moved.

**Round 5** (revision 5 → stage A's changes "confirmed", stage B "rework"). Round-4 items 5-8 resolved; 2-4 partly; 1 not. Findings:
1. A crashed unattended merge became `review` and then `landed-by-hand`, crediting an unverified change. → a `merge-interrupted` hold, excluded from `landed-by-hand`.
2. `merging` was not a lock, so a second `land()` could merge another batch during verification; the process fields were missing. → `merge_pid`/`merge_proc_start`, and a `BEGIN IMMEDIATE` acquire that re-checks `merging`/`merged`/`released`.
3. `tick_proc_start` was missing. → added.
4. The late-cancel cleanup contract. → insert as `cancelled` in the same transaction, finalize outside it, rely on the supervisor's retry and the worktree GC.
5. `unjudgeable` had no state. → a state row; version 1 is kept permanently.
6. A hand merge of an abandoned branch was unspecified. → it is not credited; an audit event records it.
7. Round 4 item 1 said "mid-canary". → the rowless interval is mid-verification.

Stage B is split into B1 (landing intent and lock, which today's `rsi_land` also lacks) and B2 (the goal ledger), as round 5 suggested.

## 8.10 Build and verification

**Stage A** (G1, G5, G6): one session.
1. Tests, each failing before its change:
   - `issues()` still runs `gh` with a PATH that lacks it;
   - per-source results: a failed `issues`, a malformed `requests.json` and a malformed `goals.json` each record that source as failed, and the other sources still produce candidates;
   - tick events for a completed no-op tick, completed and failed ticks alternating, 3 failures in a row, and a start with no ending;
   - the `rsi-health` row opening after 90 min and clearing on recovery;
   - no alarm in the first 90 min after rollout, or after `rsi_auto` is turned back on;
   - open → ack → continued failure creates no second row, and recovery clears the acked one;
   - clearing by `consumer_ref` leaving other rows open;
   - allowances: a capped goal yields to a defect and back, a `rezmac/goal:x` batch counts as a goal, and a batch without `rsi_source` counts as a defect.
2. The full suite once, then commit and release on chad-mbp.
3. Live check: the next tick's `rsi.tick-completed` shows `issues: ok`.

**Stage B1** (landing intent and lock), after its review: one session.
- Tests:
  - acquiring the `merging` lock in one transaction;
  - a second `land()` returning while a live `merging` row exists;
  - a kill after `git merge` but before verification leaving `merge-interrupted` across repeated ticks;
  - PID reuse not treated as alive;
  - today's review paths (conflict, kernel touch, removed tests, red suite) still recorded once.
- The full suite once, then commit and release.

**Stage B2** (G2, G3, G4, G7), after B1:
1. Tests for every row of the close table, including:
   - kill-before-result and kill-after-submit;
   - a submit blocked inside `batches.submit()` while a concurrent tick runs: the round is not closed;
   - a late submit to a closed round inserted as cancelled, including a crash between commit and finalization;
   - a reused PID for `tick_pid`;
   - `landed-by-hand` with and without a landing row;
   - no `landed-by-hand` during `merging`, `merge-interrupted`, `merged` or `released`;
   - `held` resolved by a merge and by a reset;
   - two reset-abandoned rounds not filling `REVIEW_CAP`, and their branches never landing;
   - an unsupported `registry_version` giving the `unjudgeable` state, which plans nothing;
   - a hand merge of an abandoned branch staying `abandoned`, with its audit event.
2. Tests for every derived state, including a deleted goal with a round still judging (judged from its own `goal_json`), `met` before `stopped`, and drift back to `eligible`.
3. Tests for each 8.5 clock just inside and just outside the range, and for each G4 rejection.
4. The full suite once, then commit and release.
5. Measure with `ilk2 rsi goals`, fix the 8.8 targets, then write `docs/rsi/goals.json`.
6. Update docs/02 §2.11a.

## 8.11 Alternatives not taken, and deferred

- **A goal on "days since the last RSI landing".** Rejected: when there is genuinely nothing to fix, it pushes RSI to invent work. The stage A alarm watches completed ticks instead.
- **A stored goal state machine** (revision 2). Rejected after review.
- **A new finder source for slow steps.** Not now: a goal on minutes per step makes the planner look at that evidence.
- **Reopening every `done` defect that recurs.** A separate decision.
- **Deferred:**
  - remote goals (`host`), which need a remote range query;
  - a public `ilk2 report --since`;
  - an explicit `ilk2 rsi review --approve|--abandon` command, if merging by hand or resetting the goal proves clumsy.
- **Seeding `ilk2 improvement` by hand** for 18a's levers. Cheap, and it can be done alongside.
