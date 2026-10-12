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
| 11 | M1 run: throwaway consumer, 3-step batch, unattended, supervisor kill mid-batch | — | — |

## Known gaps

- `store.migrate()` creates missing tables only. A column added to an existing table needs a real migration step from here on, because chad-mbp now has a live `~/.ilk2/data/state.db` (installed Oct 13).
- vitest owning-config resolution across workspace projects; the Q12 off-host channel (a configured command until Chad picks one).

## Decisions taken while building (judgment calls)

- Q1: CLI dispatch, no SDK, for M1 (P1 step 2; `spikes/p1/findings.md`).
- Tests use a stub worker (a fake `claude` that emits stream-json), so the stall suite runs without MiMo. Live MiMo is used only for the M1 run.
