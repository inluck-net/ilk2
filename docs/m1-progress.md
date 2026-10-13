# M1 build progress (P2)

Working log for building the core toward M1 ([document 4](04-v1-relation-and-migration.md) §4.4). It is updated at each commit, and a resuming session starts here. The design is in documents 2-6; this file only tracks what exists and what is next.

Started Oct 13, 2026, while P1 step 4 ran. Chad authorized building unattended, with the builder's recommendations taken as decisions. Every judgment call is labelled in code or commit text.

## Order of work

| # | Module | Status | Tests |
|---|---|---|---|
| 1 | `store.py`: schema, CAS transitions, events, `needs_human`, refusals | done (leases in 6) | tests/test_store.py (8) |
| 2 | `plans.py`: V1 Markdown and JSON import, submit checks | done | tests/test_plans.py (7) |
| 3 | `gates/`: pytest and vitest adapters (own reporters), package-script resolution, spec versions, pinned env, fingerprints, outcomes, judge | done; vitest owning-config resolution across workspace projects not done | tests/test_gates.py (13), tests/test_vitest.py (3) |
| 4 | `worker.py`, `worker_hook.py`, `procs.py`: CLI dispatch, sandbox, hook (Q16 full-suite refusal), brief, group termination | done | tests/test_worker.py (20), stub `tests/stub_claude.py` |
| 5 | `driver.py`, `gitops.py`, `lease.py`, `batches.py`, `bounds.py`, `faults.py`: step lifecycle, Q6 retries, API backoff, scope, gates with parent probe, acceptance + recovery | done | tests/test_driver.py (10: S5, S7, S8, S10, S14, S19 in-process) |
| 6 | `supervisor.py`: tick, reconcile without adoption, admission (budget, project limit, host load, refusal rows), spawn, stall checks, requeue + crash-loop, cancel / park --now | done | tests/test_stall.py |
| 7 | `health.py` (kickstart, 3/h -> needs_human), `notify.py` (owner / consumer policy, re-sends) | done; off-host channel is a configured command until Q12 is picked | tests/test_stall.py (S12, S13, S26) |
| 8 | `cli.py`: drive, supervise, health, project add, submit (JSON / V1), status, events, park, resume, cancel, amend (Q3 journal), ack, lease, report, release, doctor | done | test_release (CLI smoke), test_driver (amend) |
| 9 | `release.py`: build (uv venv + wheel + manifest), install (atomic `current`, launchd plists), upgrade drain; `doctor` | done | tests/test_release.py (drain x2, build+install, CLI smoke) |
| 10 | Stall suite S1-S27, with a stub worker | all 27 have a passing test | test_stall (16), test_vitest (S20, S21), test_driver, test_gates, test_worker, test_plans |
| 11 | M1 run: throwaway consumer, 3-step batch, unattended, supervisor kill mid-batch | **met** Oct 13 04:24 (see below) | `~/.ilk2/m1/verify.py` |

## M1 result (Oct 13, chad-mbp, release 0.1.0+g955f5eda under launchd, MiMo mimo-v2.6-pro)

Batch `b-26687da364b1` on the throwaway consumer `~/.ilk2/m1/consumer` (todo-lite, 3 steps with pinned tests). Checked independently by `~/.ilk2/m1/verify.py`:
- 3 of 3 steps accepted; 3 commits on the branch, all with `ilk2-attempt` trailers; the branch head is the last step's commit; the worktree is clean;
- 0 human transitions, 0 needs_human rows;
- the supervisor was killed with SIGKILL during step 2's dispatch (04:21:32). launchd restarted it, reconcile marked run `r-32f0460ee630` lost and requeued it, and run `r-36c98ec6fd2c` shipped the batch;
- the consumer's own suite passes at the final head (7 passed), run outside ilk2;
- the stall suite S1-S27 passes in CI (the vitest cases only since the stall-suite review: before it, CI skipped them; docs/06).

The first M1 attempt (`b-1b3841db9323`) found a bug (docs/06 B5: the scope check read `odo.py`) and was cancelled by hand. That is the one human transition of the day, outside the batch that met the criterion.

## After the M1 code review (Oct 13)

Codex's code review found 7 defects (C1-C7) and fixing C7 exposed an eighth (C8). All are fixed with tests; the table is in [document 6](06-open-items.md) "M1 code review". Suite: 106 passed, 1 skipped (release build while `src/` was dirty). The new supervisor also finishes the stale rows in the live store: the cancelled `b-1b3841db9323` hierarchy and the running attempt `a-20739d425ee1` left by the M1 supervisor kill, from before lost runs closed their attempts.

## Attribution (before P4), Oct 13

Built and tested (A1-A10, plus a blocked-repair barrier case); see [document 6](06-open-items.md) "Before P4" item A. Repair plans are added through `amend` (`add_plan`, actor `driver`, reason `regression-attributed`) and recorded in the `repairs` table; probe results persist in `probe_cache`, so a requeued run reuses them. Both are new tables, which `store.migrate()` creates on the live store; no existing table changed.

## Known gaps

- `store.migrate()` creates missing tables only. A column added to an existing table needs a real migration step from here on, because chad-mbp now has a live `~/.ilk2/data/state.db` (installed Oct 13).
- vitest owning-config resolution across workspace projects; the Q12 off-host channel (a configured command until Chad picks one).

## Decisions taken while building (judgment calls)

- Q1: CLI dispatch, no SDK, for M1 (P1 step 2; `spikes/p1/findings.md`).
- Tests use a stub worker (a fake `claude` that emits stream-json), so the stall suite runs without MiMo. Live MiMo is used only for the M1 run.

## Q17 evidence from P2 (docs/06 P2-1)

**The M1 batch:** 3 planned steps, 3 accepted as written, 0 amendments. Denominator 3, changes 0.

**Building M1 itself (by hand, against the 11-item order above):** 11 planned items, all still valid. Items added because an earlier item's result showed they were needed:

| Added item | Found by | Knowable when the order was written? | Cost had it been missed |
|---|---|---|---|
| `must_pass` on steps (B1) | the first driver test passed with no change | yes, by reading §2.5.4 against a "make it pass" step | every "fix the red test" step would pass vacuously |
| own vitest reporter | vitest 3.2's JSON reporter drops timeout messages | no, only by running it | S21 unrecognizable |
| per-host config (`host.json`) | CI failed S11 and S13 on a small runner | partly | flaky stall suite on CI |
| health start-up grace | the first real install kickstarted its own supervisor | yes, §2.11.1 names the start-up bound | a restart at every install |
| raw porcelain parsing (B5) | the first live M1 run refused its own step | no | any step touching a tracked file at the top of `git status` refused |
| closing a lost run's attempt | the M1 supervisor kill | partly | attempt rows left `running` forever |
| absent declared test path fails after a step | a self-review for the B5 shape | yes | a step skipping its new test file would pass |

Of 7 additions, 3 were knowable from the design text and 4 needed a run. All stayed inside the goal and the verification obligations; none changed the M1 exit criterion. A planning pass could have proposed the 4 run-found items only after the run that found them, which is what happened by hand.
