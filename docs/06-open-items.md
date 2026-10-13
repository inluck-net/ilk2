# 6. Open items after design close

The design was closed for P1 on Oct 13, 2026 (tag `design-accepted-p1`). Review rounds stopped there. Each item below is resolved at the phase that needs it, against real code, and reviewed at that phase boundary, not in further rounds on paper. Open questions that need Chad's decision stay in [document 5](05-open-questions.md).

## Review checkpoints from here

| When | Reviewed against | Reviewers |
|---|---|---|
| After P1 | The spike write-up in `docs/spikes/` and its code | Chad, Codex |
| At M1 | The core, the stall suite S1-S27, and the P2-1 evidence below | Chad, Codex |
| Before P4 | Attribution (§2.5.7) and suite A1-A10 | Codex, gh-resolve |
| Before P5 | The P5 addendum below, against gh-resolve's code at that time | gh-resolve, Codex |

## During P2, decided at M1

### P2-1: plan steps as the batch runs (Q17)

**Origin.** Chad, Oct 13, after P1 step 1: "is it better to plan beforehand or to plan as we go?" The design plans beforehand. A batch is submitted with its full step list. Amendments are version-checked, limited to steps that have not started, and recorded in an append-only journal (Q3). Nothing proposes the next step from what the last one found.

**Why it may matter.** Later steps are written against assumptions that earlier steps test. P1 had a case in its first step. Step 1 found that Claude Code's Bash tool runs commands in their own process group, so a kill of the worker's group orphans them (`spikes/p1/findings.md`, finding 1). That revealed a descendant-process hazard that step 3's sandbox or the supervisor's cleanup must cover. Whether step 3 itself has to change is not yet shown; the plan, written beforehand, did not name the hazard.

**Direction (not designed yet).** Fix the destination and its verification before the batch starts, and plan the steps as it runs:

| Sealed at submission: an autonomous amendment may strengthen these, never weaken them | May change as the batch runs |
|---|---|
| The batch goal | The concrete next 1-2 steps |
| The **verification obligations**: which gates exist, their minimum coverage, and a final verification plan. That plan is sealed: weakening, replacing, dropping or reordering it, or editing the verification assets it protects (its tests and gate configuration), needs a human amendment | The order of later pending steps |
| Bounds (§2.11), plus a **step budget** and a **planning budget** (below) | Replacing, splitting or dropping pending steps, by stable step id |

Gate *specification versions* are not frozen. They change legitimately when a step edits tests, runner configuration or `package.json` (§2.5.1). What is sealed is the obligation, not the hash.

**Who writes what (I1).** A planning pass writes nothing authoritative. It emits a **proposal**: the amendment, the plan version it was made against, and the evidence it cites (attempt ids, commits, gate output). Only the driver applies a proposal. It does so in one transaction, after checking the plan version, the sealed envelope, both budgets, and that every touched step is still pending. The journal is append-only. Each amendment may replace, reorder or drop pending steps. Acceptance stays with the driver's gates (§2.4-2.4.1).

**Budgets, so the loop is bounded (I11):**
- **The step budget is monotonic.** Every created step consumes a slot permanently, split replacements included, and a dropped step never refunds one.
- **There is a separate limit on planning passes and on applied amendments,** so a planner cannot churn the plan without adding steps.
- **A planning pass is a bounded phase with a retry limit.** Exhausting either budget ends in `blocked (planning-budget)` with a `needs_human` row.

**State it would need if adopted** (§2.3 has none of it today):
- batch fields for the goal, the sealed verification obligations, and each budget's limit and consumed count;
- an amendment record: actor and session, plan version before and after, evidence references, and the reason.

**Risks the guards answer:**

| Risk | Guard |
|---|---|
| Goal drift, where each step looks reasonable next to the one before | Sealed goal and verification |
| No natural end, because there is always one more step | Monotonic step budget |
| Churn | Planning budget |
| The planner grading its own work | Driver-only application and acceptance; protected verification assets |

**Open, for M1:**
- who plans: a separate model session, the consumer that submitted the batch, or a human through `/ilk2-plan`;
- what triggers a planning pass: every accepted step, or only a step whose evidence contradicts a later step's instructions;
- which stall cases the stall suite gains (a planner that adds steps forever, churns the plan, or proposes against a stale plan version).

**What P1-P2 do.** M1 is built with fixed step lists, as designed; nothing above is needed to build it. Every batch and phase records a manual evidence table.

*For every batch:* total steps and amendment opportunities, counting the steps where the original plan stayed valid. That is the denominator.

*For every step that had to change after an earlier one ran:*
- the original instructions and the assumption that failed;
- the evidence, with attempt ids and commits;
- whether the fact was knowable when the plan was written;
- the amendment, before and after;
- whether the goal, scope or verification obligations changed;
- the attempts and time wasted, or that would have been wasted, without the amendment;
- the time a human amendment took;
- the final gate outcome;
- what a planning pass would have proposed, and whether that would have helped.

**Decision rule, declared now:** adopt only if discoveries that were not knowable at planning time recur, cost material time, and would have been handled inside the sealed envelope. No numeric threshold is set. Chad decides Q17 at the M1 review from this table.

## Found while building M1 (P2)

| # | Finding | What M1 does |
|---|---|---|
| B1 | A test that fails at the batch base and still fails after a step is not a new failure (§2.5.4), so a step whose job is to make such a test pass could not say so. The first driver test "passed" with no change at all. | Steps may declare `must_pass: [test ids]`, judged as the inverse of `expect_red`: those ids must pass after the step whatever the base says. V1 import maps "remove the xfail" steps to it by hand for now. Review at M1 |
| B2 | A descendant that inherits the worker's stdout kept the driver's reader blocked until the attempt cap | stdout is read on a thread; the main thread watches the worker process and owns the cap |
| B3 | Python reuses a stale `.pyc` when a file is rewritten with the same size within one second, so a gate could test old code | Every gate run gets a fresh `PYTHONPYCACHEPREFIX` |
| B4 | S20's text says a config edit "triggers a new base measurement", while §2.5.4 (Codex review 3, later) says a changed gate specification has no comparable base | M1 follows §2.5.4: no comparable base, every failure counts; S20's test asserts that. Reword S20 at the M1 review |
| B5 | Three vacuous passes or refusals came from the same shape, an absent signal read as a pass: a base-red test that stays red (B1), a `must_pass` test in a file that fails to collect, and a parser that dropped a path's first letter so the first live M1 run refused its own step | `must_pass` requires the id among passed tests; changed paths read raw git output; each has a test that fails without the fix. The M1 review should look for this shape elsewhere |
| B6 | Observed on the M1 run (Oct 13 04:21): SIGKILL of the launchd-managed supervisor also ended its driver within a second, although the driver runs in its own session. Its worker was gone too: the only process left in the worktree was the next run's. Mechanism not verified (launchd job teardown is the likely one). S11 assumed drivers outlive their supervisor | Recovery is the same either way: reconcile finds no live driver, marks the run `lost` and requeues. S11 still covers the case where a driver survives (supervisor run outside launchd). Lost runs now close their in-flight attempt as `interrupted` |

## M1 code review (Codex, Oct 13): findings and fixes

Codex reviewed the core at `fcacd95` (prompt: `~/Documents/handoffs/ilk2-m1-code-review-prompt.md`). The builder checked each finding against the code before fixing it; all 7 held. Each fix has a test that fails without it.

| # | Finding | Fix | Test |
|---|---|---|---|
| C1 | Cancel ended the batch and run only: plans, steps and the attempt in flight stayed non-terminal (the live store's `b-1b3841db9323` showed it) | `Driver.finish_cancel`: recover `accepting`, close running attempts, plans and steps → `cancelled`, then release the root. Run from the driver's own `cancelled` stop, from `ilk2 cancel` when nothing runs, and by the supervisor for any cancelled batch with something left to end | S23 (both) assert the whole hierarchy; `test_cancel_of_a_queued_batch_ends_its_hierarchy` |
| C2 | A worker that crashed, errored or timed out was judged like a finished one, so green gates accepted it; an attempt with no change made an empty commit | Only `done` can be accepted; any other outcome is a counted red with the gate output as evidence. An unchanged tree is red | `test_worker_that_did_not_finish_is_never_accepted`, `test_attempt_that_changes_nothing_is_red` |
| C3 | A driver that ended its own run with `error` was never counted, so the supervisor respawned it every tick forever (I11) | `reap` counts any self-ended run whose reason is a failure (`driver.counts_as_restart`); the third reaches `blocked (crash-loop)`. Attempts under an ended run are closed every tick | `test_driver_error_counts_toward_the_crash_loop`, `test_attempt_left_running_by_an_ended_run_is_closed` |
| C4 | Recovery "undo" reset only the store, so a cancel could release a root with the uncommitted accepted tree in it | Undo runs `reset_hard(parent)` first | S23 cancel-while-accepting asserts a clean worktree |
| C5 | Survivors of SIGKILL opened a row but the run was still requeued, and a cancel could still release the root | Survivors fence the batch: `blocked (unkillable)` with the lease kept; a cancelled batch keeps its lease while any process is inside the root (`procs.strays`); `ilk2 resume` refuses until the root is empty | `test_unkillable_worker_fences_the_batch_and_keeps_the_lease`, `test_supervised_kill_with_survivors_fences_instead_of_requeueing`, `test_cancel_keeps_the_root_while_a_process_is_in_it` |
| C6 | A whole-file `must_pass` passed with a test still skipped or xfailed | Reports keep skipped and xfailed ids; a whole file needs none of them | `test_must_pass_file_entry_rejects_skipped_and_xfailed` |
| C7 | A `shell` gate passed submission but had no runner, so it always blocked as `base-unmeasured` | A shell runner per §2.5.1: exit code only, one pseudo-test `<gate>::<exit>`, and red at the base blocks the batch | `test_shell_gate_is_exit_code_only`, `test_shell_gate_runs_green_at_base`, `test_shell_gate_red_at_base_blocks_the_batch` |
| C8 | Found while fixing C7, not by Codex: `must_pass` and expected reds were judged per gate, so any step with two gates failed on ids the other gate runs | Both are step-level (`gates.unmet_obligations`): each id must be met by one of the step's gates | `test_step_obligations_span_gates`, `test_shell_gate_runs_green_at_base` |

Judgment calls in these fixes:
- **C2:** a timed-out worker whose partial work passes the gates is still not accepted; the next attempt resumes with that evidence. Wrong if P3's `ilk2 report` shows timeout reds followed by an immediate accept with no further change.
- **C2:** no opt-out for steps that change nothing. Wrong if P3 needs verify-only steps; then add a step flag (a schema migration).
- **C3:** a `lease` stop (the root not held by the driver) counts as a restart, so it ends in `crash-loop` after three instead of looping. Wrong once R4's lease waiting lands; that wait should be a refusal row instead.
- **C1:** `cancelled` is a new terminal state for plans and steps, not a reuse of `blocked`. No column changes, so no migration.
- **C7:** a shell gate red at the base uses the existing `base-unmeasured` reason, with the detail "red at the base".

### Stall suite strength review (Codex, Oct 13, at `275ae76`)

Codex rated S1-S27: 11 STRONG, 16 WEAK, 0 MISSING (prompt: `~/Documents/handoffs/ilk2-stall-suite-table-prompt.md`). The builder checked each claim against the tests:
- **CI never ran the vitest cases.** CI had no Node, so `tests/test_vitest.py` skipped (CI: 104 passed, 3 skipped). S20 and S21-vitest had only run on chad-mbp. CI now installs Node 20 and vitest 3.2.4 from `tests/fixtures/vitest-cache/package.json`, and with `ILK2_REQUIRE_VITEST=1` a missing cache fails the run instead of skipping it.
- **B5-shaped tests, confirmed and fixed:** S18 asserted a state set before the fault, S16 had no attempt, S22 raced sequentially and swallowed the loser's reason, S27 used batch status as proof nothing ran, S15 never checked its base. Also S14, which accepted the wrong recovery mode.
- **Refuted:** "S23 can cancel before an attempt exists" (the attempt row is inserted before the dispatch phase). S23 was weak for a different reason Codex missed: the test helper `until()` killed every driver on return, so S23 never cancelled a live run. `until(..., keep=True)` now keeps them.
- **Strengthened:** S3 (two real starts, no survivors), S5 (15 scaled minutes of API errors), S9 (through `submit`, no rows), S13 (kickstart within the start-up bound), S17 (KILL only after the TERM grace), S19 (the inherited tag reaches the worker's prompt), S21 (through the driver: one attempt), S23 (live worker gone, its attempt closed), S24 (base rows), S26 (the channel, not only the count).
- **Mutation-checked:** for S3, S5, S14, S15, S16, S17, S18, S19, S22, S23, S26, S27, a mutant with the defect Codex described passes the old test and fails the new one. S9, S13, S21 and S24 were strengthened without a mutant.


## P5 addendum: resolver integration (from gh-resolve's re-review at 5e0e3ca)

These items only matter once gh-resolve's resolver runs on ilk2. They stay open until a P5 design addendum, reviewed with gh-resolve before P5 starts.

| # | Item | Direction recorded so far |
|---|---|---|
| R1 | No unattended way out of `consumer-expired` if reap crashes holding the lease ([document 2](02-architecture.md) §2.4.2) | gh-resolve may run `ilk2 lease recover` once its own reap lock is free. ilk2 accepts it only if no process has its working directory inside the root |
| R2 | The 30 min lease limit is too short: gh-resolve's pre-push hook took about 871 s on rezmac, and one `push_branch` call can exceed 30 min. `lease renew` and `lease recover` are missing from the CLI lists in §2.2 and §2.8 | Each project declares its maximum lease length (gh-resolve about 60 min, a judgment call); `acquire` takes a length up to that maximum. Add both verbs to the lists |
| R3 | gh-resolve's base refresh (`reap.py:3443`) acts on queued runs, which the driver holds, so it can never acquire the lease | Proposed: `ilk2 advance-base <batch> --onto <sha>`, applied by the driver at its next step boundary. Alternative: gh-resolve turns base refresh off under ilk2. Chad to choose at P5 |
| R4 | A second batch on the same root (rework reuses `resolver-<issue>`, gh-resolve `pipeline.py:471-478`) is undefined | The lease belongs to the root. A new batch's driver acquires it from `released`; while a consumer holds it, the batch waits behind a refusal row that clears on release |
| R5 | How chad-mbp's 2 ilk2 runs are split between gh-resolve dev batches and the resolver | Proposed: per-project limits plus priority, with 1 run always reserved for the resolver (judgment call). Chad to confirm at P5 |
| R6 | [Document 4](04-v1-relation-and-migration.md) §4.8 should list "gh-resolve adopts the lease in reap, land and refresh" as a blocker | Add with the addendum |
| R7 | gh-resolve's pre-push hook time is outside ilk2's timings | ilk2 counts its own setup time; the hook time comes from gh-resolve's own records in the P5 comparison |
| R8 | Found Oct 13 (kira-cloudflare #8463): gh-resolve refuses issues whose write target is on its denylist (`convex/migrations/` here). ilk2 has no denylist, so a batch submitted to ilk2 would bypass that control | gh-resolve keeps enforcing its denylist before it submits; ilk2 adds a per-project `deny_paths` to the scope check (an out-of-scope red, never a pass), so the control holds whoever submits. Before P5 |

## Before P4

| # | Item |
|---|---|
| A | Attribution (§2.5.7) and suite A1-A10: **built Oct 13** (`src/ilk2/attribution.py`, `tests/test_attribution.py`, 10 tests, each property mutation-checked). Additions to §2.5.7, all judgment calls: a result `current` when the parent fails with a different fingerprint (the current step changed the failure, so it is judged there), and `foreign` when the failing side is not an ilk2 step (a consumer commit); a probe that cannot measure counts as `flaky`; each failure is probed alone (one test id per run), simpler and more expensive than batching ids. Fixed on the way: parent probes on vitest gates used pytest-style ids, and parent-probe diagnostic worktrees skipped the setup hook (A5 requires it in every diagnostic worktree). Wrong if P4's probes are too slow one id at a time; then batch the ids per commit |
| B | P4 is pytest-only, so the vitest/bun gate path must also be proven before P5 ([document 4](04-v1-relation-and-migration.md) §4.4). Read against kira-cloudflare's real gate on Oct 13 (`test:non-ui:convex` = `TZ=UTC vitest run -c tests/convex-tests/vitest.config.ts`): the config is explicit and has no `projects`, so per-file workspace resolution does not block this gate. Found and fixed: the script's leading `TZ=UTC` was dropped; gates now carry the script's assignments (`Gate.env`), in every run, probe and spec version. Still open: the spec version hashes the config file but not the files it imports (`setupFiles: ./lib/convex-test-env.ts`, the reporter), so editing the setup file does not change the spec version |

## From the full-suite cost evidence (E2, Oct 13)

Chad asked whether splitting projects into component repos would keep full suites cheap. [E2](evidence/2026-10-13-full-suite-cost.md) measured V1's repos on both hosts, and the answer is no, as the main fix. The cost comes from suites that are never green, run in the wrong place, and run more than once. These items follow from it.

| # | Item | Phase |
|---|---|---|
| F3 | **Hook gap, confirmed by experiment** against `worker_hook.py:44-64` at `bfc7976`. A command that names the gate's whole collection as a directory is allowed: `pytest tests/`, `vitest run -c <config> convex/__tests__`. The resolved gate command names no paths to compare against. A package script whose name lacks `test` (`bun run check`) is also allowed. In V1, directory-wide worker runs cost gh-resolve 5.8 h over 435 calls (E2 §1). Fix: treat a path as covering the whole suite when it contains the gate's collection root (pytest rootdir or testpaths; the vitest config's `include` roots). Resolve package scripts by their entry, not by their name | P3 (fix now) |
| F4 | **Change-based selection for step gates.** A project may declare a `selector` command: changed files in, test files out. The fallback to the full suite is recorded with its reason. Both large consumers already have one (ilk-skills `gate_scope.py`, kira-cloudflare `scripts/select-pr-tests.ts`). A batch then runs at most one full suite, in its verification plan. Judgment call: selection is advisory to the driver's verification plan, never a replacement for it; wrong if selected sets miss regressions the verification plan then catches often | P3 |
| F5 | **Cache base measurements per project.** `base_measurements` is keyed by `(batch_id, gate, spec_version, commit_sha)`, so batches on the same base re-measure it. On rezmac a base suite costs a median of 710 s (E2 §2). Key on (commit, spec version) across a project's batches. Judgment call: wrong if a base result depends on state outside the commit and spec version, which the pinned environment is meant to rule out | P3, before P5 at the latest |
| F6 | **ilk2's own suite stays green and bounded.** CI gates main with no tolerated red baseline. No test reaches the real HOME or host state. A fast tier gets a time budget (judgment call: 60 s; wrong if it fails on normal growth rather than on slow tests). CI took 131 s at `273c0e0` and 323 s at `96ae7cb` | now |

Outside ilk2: kira-cloudflare's pre-push could reuse its own selector, and gh-resolve's reap re-runs the consumer's full pre-push after the engine's gate has passed (P5 addendum).

## Decided during review, recorded for traceability

All findings from five review rounds were applied: Codex ×3, gh-resolve ×2. The [README](README.md) revision notes list each round, and the commits carry the details.
