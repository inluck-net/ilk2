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

## Before P4

| # | Item |
|---|---|
| A | Attribution (§2.5.7) and suite A1-A10 |
| B | P4 is pytest-only, so the vitest/bun gate path must also be proven before P5 ([document 4](04-v1-relation-and-migration.md) §4.4) |

## Decided during review, recorded for traceability

All findings from five review rounds were applied: Codex ×3, gh-resolve ×2. The [README](README.md) revision notes list each round, and the commits carry the details.
