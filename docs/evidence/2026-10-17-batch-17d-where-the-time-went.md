# Evidence: where batch 17d's time went (Oct 17)

Batch `b-3bbee3537471` (`v1:MASTER-2026-10-17d-execution-plan`), gh-resolve on chad-mbp, worker model `mimo-v2.6-pro`. 6 work plans and a verification plan, 7 steps, one step per plan. At 20:53, 4 of 7 steps were accepted after 4 h, and the batch was heading for about 6 h. Sources: `ilk2 report --batch`, `ilk2 events --batch --json`, the ilk2 store, and the worker transcripts under `~/.ilk2/data/runs/<run>/<attempt>/config/projects/`.

## Per step

| # | Plan · step | Minutes | First edit (min) | Turns | s/turn | Inserted lines (tests / src / docs) |
|---|---|---|---|---|---|---|
| 1 | the-sweep-judges-the-backlog-oldest-first · 0 | 39.5 | 7.6 | 117 | 13.4 | 3,720 (1,326 / 2,394 / 0) |
| 2 | the-sweep-judges-the-backlog-oldest-first · 1 (same session) | 29.0 | 1.4 | 137 | 8.2 | 1,723 (998 / 276 / 449) |
| 3 | the-manual-sweeps-verdicts-are-imported-and-excluded · 0 | 40.8 | 14.3 | 93 | 18.6 | 2,894 (1,585 / 1,089 / 220) |
| 4 | a-likely-done-issue-is-labelled-with-its-evidence · 0 | 54.8 | 22.5 | 91 | 25.1 | 3,553 (1,868 / 1,497 / 188) |
| 5 | a-cheap-swept-issue-reaches-ingest · 0, attempt 1 | 60.0 (cap) | 12.4 | — | — | 1,566 uncommitted at the cap |

Step 5's first attempt ended at the 3600 s attempt cap (`attempt.end` outcome `red`, "The worker's session did not finish (timeout)"). Its work was kept, and attempt 2 resumed the same session.

Phases over the batch (min): dispatch 217.9, base 7.9, merge 1.6, gates 1.4.

## Inside an attempt

Read from the transcripts. A request is one model call. A long turn is a request of more than 60 s.

| Session | Total | Model | Long turns (count, min) | Tools | Requests (median s, by context band) |
|---|---|---|---|---|---|
| steps 1-2 | 68 min | 65 min | 7, 14 min | 3 min | 254 (6-11 s) |
| step 3 | 40 min | 39 min | 7, 21 min | 1 min | 93 (7-15 s) |
| step 4 | 54 min | 51 min | 7, 28 min | 3 min | 91 (7-14 s) |
| step 5, attempt 1 | 62 min | 60 min | 9, 26 min | 2 min | 153 (4-17 s, 30 s for the first) |

- **Median request latency does not grow with context** from 50k to 350k tokens. The rising mean s/turn comes from the long turns.
- **Long turns** are thinking followed by one large tool call. The largest wrote 18-23k output tokens: whole test files, or in step 4 a single 46,638-character edit.
- **Minutes to first edit is only partly discovery.** Before the first edit, each worker made 35-46 tool calls (reads, greps, `sed -n`).
  - Step 1 read steadily for 7.2 min. That is discovery.
  - In steps 3-5 the reading came in bursts: 0.3-3.3 min, 0.3-1.8 and 9.8-14.6 min, 0.3-3.5 and 7.6-8.9 min. Single model requests of 3-8 min filled the gaps between bursts and the time up to the first edit.
  - In step 4 the first edit *was* the implementation: a 7.9 min request that wrote 46,638 characters.
  - The metric is stamped when that request ends.
- **Every attempt opens with a brief of 171-185k characters** (66-74k tokens on the first request). It holds the step's sub-plan and windows of 8-14 files. The worker still re-read files the brief contained: the sub-plan's "Before you start" section asks it to (for example "Read these. Line numbers are at `ac23c541`; re-read every symbol").

## Against the other gh-resolve batches

24 gh-resolve batches in the store, accepted work steps only (verification steps excluded). Inserted lines from `git show --shortstat` of each accepted commit.

| | 17d | Median of the other 23 | Range of the other 23 |
|---|---|---|---|
| Inserted lines per step | 2,972 | 981 | 340-2,261 |
| Inserted lines per worker-minute | 72.5 | 36.9 | 6.9-76.2 (b-f695a3aa9495) |
| Minutes per accepted step | 41.0 | 29.7 | 10.2-128.6 |

## The plan's shape

- **Strictly serial.** Each sub-plan's `depends_on` names every earlier sub-plan (MASTER line 394: "6 depends on 1-5"; line 445: "serial sub-plans"). gh-resolve has `max_lanes` 2, and no two plans could run at once. ilk2 copies `depends_on` from the frontmatter (`src/ilk2/plans.py:257`).
- **Written by V1's `/ilk-plan`** in the gh-resolve session, by helper agents following `commands/ilk-plan.md`. It was queued in `~/.ilk-data/projects/<key>/plans/` and claimed by ilk2's v1 import (`*.ilk2-claimed`). Confirmed by the gh-resolve session (gh-resolve-e1).
- **It exceeded V1's own sizing rules.** `ilk-loop/references/decomposition-principles.md:84-96` asks for a sub-plan body of about 200 lines or less, typically 5 files or fewer. `plan_lint.py:1754` (`lint_step_window_fit`) asks a step's edit surface to fit in half the iteration window. 17d's steps touched 7-10 files and inserted 1,700-3,700 lines.
- **Test volume came from review.** The external design review's 6 blockers each required failing tests first, and the planner packed one step per sub-plan with all of them (gh-resolve-e1).

## Conclusion

ilk2's orchestration cost about 11 minutes of the batch. The time is the worker writing very large steps, one after another. Its throughput was the second highest of 24 batches. The levers are plan shape (step size and real dependencies) and the worker's long thinking turns (docs/06 "Speed levers… never built"). Neither is a defect in ilk2's execution path. The first-edit metric is a reporting defect.
