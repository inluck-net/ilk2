# Evidence: where batch 17d's time went (Oct 17)

Batch `b-3bbee3537471` (`v1:MASTER-2026-10-17d-execution-plan`), gh-resolve on chad-mbp, worker model `mimo-v2.6-pro`. **5 work plans and 1 verification plan, 7 steps:** the first work plan has 2 steps, every other plan has 1. Snapshot after the batch shipped, Oct 17 22:30 CST.

The tables below are the output of `uv run python tools/batch_time.py b-3bbee3537471`. It reads the ilk2 store read-only, `git show --numstat` of each accepted commit, and the worker transcripts under `~/.ilk2/data/runs/<run>/<attempt>/config/projects/`, including each session's `subagents/*.jsonl`. Its method is in its docstring. Codex reproduced the first four steps' durations, first-edit times, turns, insertions and splits independently (review of `32cbb91`).

**Submitted to shipped: 5.23 h.**

## Accepted work steps

| Plan · step | Minutes (all attempts) | Files | Inserted (tests / src / docs) | Instruction chars |
|---|---|---|---|---|
| the-sweep-judges-the-backlog-oldest-first · 0 | 39.5 | 7 | 3,720 (1,326 / 2,394 / 0) | 31,433 |
| the-sweep-judges-the-backlog-oldest-first · 1 | 29.0 | 9 | 1,723 (998 / 276 / 449) | 31,248 |
| the-manual-sweeps-verdicts-are-imported-and-excluded · 0 | 40.8 | 6 | 2,894 (1,585 / 1,089 / 220) | 17,921 |
| a-likely-done-issue-is-labelled-with-its-evidence · 0 | 54.8 | 10 | 3,553 (1,868 / 1,497 / 188) | 20,666 |
| a-cheap-swept-issue-reaches-ingest · 0 | 89.1 | 11 | 3,927 (2,012 / 1,574 / 341) | 26,529 |
| a-sweep-stops-from-a-file-and-reports-its-residue · 0 | 34.2 | 8 | 1,930 (1,231 / 517 / 182) | 11,158 |

The verification step is excluded, as in every comparison here.

## Inside each attempt

A request is one model message. A long request is one of more than 60 s. "First edit" is the request that made the first `Edit`/`Write`: when it started and when it ended, measured from the attempt's start.

| Step · attempt | Outcome | Wall | Model | Long (n, min) | Tools | Requests (+subagent) | Median request | Calls before first edit | First edit start → end (min), chars |
|---|---|---|---|---|---|---|---|---|---|
| oldest-first · 0 #1 | accepted | 39.5 | 37.9 | 6, 13.4 | 1.3 | 117 | 11 s | 46 | 7.2 → 7.6, 4,327 |
| oldest-first · 1 #1 (same session) | accepted | 29.0 | 27.1 | 1, 1.0 | 1.5 | 137 | 8 s | 2 | 1.2 → 1.4, 1,441 |
| manual-sweeps · 0 #1 | accepted | 40.8 | 38.8 | 7, 20.6 | 1.3 | 93 | 11 s | 35 | 8.0 → 14.3, 2,054 |
| likely-done · 0 #1 | accepted | 54.8 | 51.3 | 7, 27.6 | 3.1 | 91 | 14 s | 41 | 14.6 → 22.5, 46,638 |
| cheap-swept · 0 #1 | red (`timings.outcome` timeout, at the 3600 s cap) | 60.5 | 58.2 | 8, 25.0 | 1.8 | 150 (+13) | 12 s | 35 | 8.9 → 12.4, 620 |
| cheap-swept · 0 #2 (resumed) | accepted | 28.7 | 22.9 | 1, 1.2 | 5.1 | 79 | 14 s | 0 | 0.0 → 1.3, 143 |
| sweep-stops · 0 #1 | accepted | 34.2 | 31.5 | 1, 3.0 | 2.2 | 112 | 13 s | 62 | 7.3 → 8.1, 13,810 |

- **Model requests are 80-96% of every attempt.** Tool time is 1.0-5.1 min per attempt. Gates over the whole batch took 1.4 min, and ilk2's work between steps about 11 min (`ilk2 report --batch`: phases base 7.9, merge 1.6, gates 1.4).
- **Long requests dominate the fresh, large steps.** In 4 of 7 attempts, 6-8 long requests took 13-28 min, a third to a half of the attempt. The resumed and small attempts had 1 each.
- **Median request time stays at 8-14 s across attempts,** and in the transcripts it does not grow with context from 50k to 350k tokens. The rising mean seconds per turn in `ilk2 report` comes from the long requests.
- **The first-edit figure mixes discovery and writing.**
  - In the fresh attempts, the first editing request *started* 7.2-14.6 min in, after 35-62 calls. That part is discovery and thinking.
  - It *ended* 0.4-7.9 min later. In step 4, that request wrote the whole 46,638-character implementation.
  - `ilk2 report` stamps the end, so it counts that writing time as discovery.
- **Every attempt opens with a brief of 171-185k characters** (66-74k tokens on the first request). The brief holds the step, its sub-plan, and windows of 8-14 files. Workers still re-read files the brief contained, because the sub-plans' "Before you start" sections told them to. For example: "Read these. Line numbers are at `ac23c541`; re-read every symbol".

## Against gh-resolve's other batches

There are 23 other gh-resolve batches in the store, with 69 accepted work steps. Two aggregations, both from the script:

| Measure | 17d (per-step mean) | Median of the 23 batch means | Pooled median of the 69 steps |
|---|---|---|---|
| Minutes per step | 47.9 | 29.7 | 28.0 |
| Inserted lines per step | 2,958 | 981 | 1,012 |
| Instruction characters per step | 23,159 | 14,896 | 13,923 |

17d's line throughput (inserted lines per worker-minute) was 2nd of 24 batches, measured over its first four steps. That makes it fast per line and slow per step. The steps were large.

## The plan's shape

- **Strictly serial.** Each sub-plan's `depends_on` names every earlier sub-plan (MASTER line 394: "6 depends on 1-5"; line 445: "serial sub-plans"). gh-resolve has `max_lanes` 2, and no two plans could run at once. ilk2 takes `depends_on` from the frontmatter (`src/ilk2/plans.py:257`).
- **Written by V1's `/ilk-plan`** in the gh-resolve session, by helper agents following `commands/ilk-plan.md`. It was queued in `~/.ilk-data/projects/<key>/plans/` and claimed by ilk2's v1 import (`*.ilk2-claimed`), as confirmed by gh-resolve-e1. All 15 of 15 gh-resolve batches created since Oct 15 were claimed this way. The other 10 of the 25 ilk2 batches in that period were submitted directly: 3 ilk2 RSI, 5 kira-dev, 2 kira-live.
- **V1's sizing guidance is prose.** `ilk-loop/references/decomposition-principles.md:84-96` asks for a sub-plan body of about 200 lines or less, typically touching about 5 files or fewer. The linter's `lint_step_window_fit` (`plan_lint.py:1754`) counts scope paths and step headings against half a window measured in minutes, which are different units. Run over all six 17d sub-plans, it returned 0 findings (Codex review). One work sub-plan exceeded about 200 body lines. Every step touched 6-11 files.
- **The test volume came from review.** The external design review's 6 blockers each required failing tests first. The planner packed one step per sub-plan with all of them (gh-resolve-e1).

## Conclusion

ilk2's orchestration was about 11 min of a 5.23 h batch. The time is the worker writing large steps, one after another. The levers are the shape of the plan (step size and real dependencies) and the worker's long thinking requests (docs/06 "Speed levers… never built"). Neither is a defect in ilk2's execution path. The first-edit metric is a reporting defect.
