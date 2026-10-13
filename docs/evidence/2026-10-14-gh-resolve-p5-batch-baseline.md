# Baseline: the gh-resolve P5 adapter batch on ilk2 (Oct 13-14, 2026)

Reference numbers for later batches. Batch `b-ffcd5f4a03e9` (`v1:MASTER-2026-10-13a-execution-plan`), gh-resolve on chad-mbp, worker model `mimo-v2.6-pro`. It was imported with 18 steps; 5 V1 "Red-first pins" steps of plans not yet started were folded into their next step (`amend merge_ceremony`), giving 13. Times are worker minutes summed over every attempt of a step, interrupted ones included (`attempts.ended_at - created_at`).

## Per step

| # | Plan · step | Attempts | Red | Interrupted | Minutes | Note |
|---|---|---|---|---|---|---|
| 1 | the-engine-is-recorded-per-run · Red-first pins | 1 | 0 | 0 | 27.5 | |
| 2 | the-engine-is-recorded-per-run · Implement; remove every xfail | 7 | 4 | 2 | 155.2 | scope enforced (V1 never did), stale fingerprints, work lost on restart |
| 3 | the-engine-is-recorded-per-run · Neighbour gate | 3 | 1 | 1 | 57.2 | hex ids in fingerprints |
| 4 | ilk2-is-called-through-one-client · 0 (merged) | 2 | 0 | 1 | 24.4 | 9.2 min lost to an unpassable gate left by the merge |
| 5 | ilk2-is-called-through-one-client · 1 | 1 | 0 | 0 | 6.2 | continued step 4's session |
| 6 | a-dispatch-is-shadowed-in-ilk2 · 0 (merged) | 1 | 0 | 0 | 32.8 | largest step (17k-character instructions) |
| 7 | a-dispatch-is-shadowed-in-ilk2 · 1 | 1 | 0 | 0 | 18.8 | continued |
| 8 | the-shadow-outcome-is-recorded · 0 (merged) | 1 | 0 | 0 | 31.7 | |
| 9 | the-shadow-outcome-is-recorded · 1 | 1 | 0 | 0 | 9.3 | continued |
| 10 | base-refresh-is-per-engine · 0 (merged) | 2 | 1 | 0 | 27.7 | gate PATH lacked /usr/sbin |
| 11 | a-superseded-run-cancels-its-shadow · 0 (merged) | 1 | 0 | 0 | 28.0 | |
| 12-13 | p5-shadow-batch-verify | | | | | running when this was written |

## Summary

| Measure | Value |
|---|---|
| Steps 1-3 (17:05-21:11, before the fixes landed) | 240 min for 3 steps |
| Steps 4-11 (21:11-00:12, after) | 179 min for 8 steps: 22.3 min per step, 7 of 8 first-attempt |
| Fresh-session merged steps (pins + implementation) | 24-33 min |
| Steps that continue the previous step's session | 6-19 min |
| Worker time split (sampled attempts) | 93-99% model, 1-7% tools; gates seconds |

`ilk2 report --project gh-resolve --v1-baseline` at Oct 14 00:13 (2 batches: P4 batch 1 plus this one's 11 implementation steps):

| Row (§4.5) | ilk2 | V1 baseline (Oct 12, 14 close records) |
|---|---|---|
| Minutes per accepted step | 33.79 (13 steps, 23 attempts) | 74.4 (9 steps advanced) |
| Share of attempts producing nothing | 0.0 | — |
| Median minutes to first edit | 7.89 | — |
| Silent stuck states | 0 | — |
| Runs past bound | 0 | — |
| Human transitions | 18 in this batch | — |
| Open human waits | 0 | — |

The 18 human transitions are the build session's own parks, resumes and amends while it fixed ilk2 during the run, not requests from the batch.

## What changed during the batch

Fixed in ilk2 while it ran (each with a test that fails without it; see `docs/06-open-items.md`, "Batch speed"): scope advisory for V1-planned projects; a sandbox that hides the engine's data; plan documents copied into each attempt; fingerprints ignoring ilk2 paths, the measurement root and hex ids; a new run keeping the step's own work; preload putting the named `file:line` ranges first; `amend merge_ceremony`; the merge dropping gates that need the folded pins commit; red-first covering pins from an earlier step; a gate PATH that always ends with the system dirs.

A later batch compares against the **steps 4-11** rows, the ones measured after these fixes.
