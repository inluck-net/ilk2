# M1 build progress (P2)

Working log for building the core toward M1 ([document 4](04-v1-relation-and-migration.md) §4.4). It is updated at each commit, and a resuming session starts here. The design is in documents 2-6; this file only tracks what exists and what is next.

Started Oct 13, 2026, while P1 step 4 ran. Chad authorized building unattended, with the builder's recommendations taken as decisions. Every judgment call is labelled in code or commit text.

## Order of work

| # | Module | Status | Tests |
|---|---|---|---|
| 1 | `store.py`: schema, CAS transitions, events, `needs_human`, refusals | done (leases in 6) | tests/test_store.py (8) |
| 2 | `plans.py`: V1 Markdown and JSON import, submit checks | done | tests/test_plans.py (7) |
| 3 | `gates/`: pytest adapter (report plugin), spec versions, pinned env, fingerprints, outcomes, judge | done for pytest; vitest pending (S20, S21) | tests/test_gates.py (13) |
| 4 | `worker.py`, `worker_hook.py`, `procs.py`: CLI dispatch, sandbox, hook (Q16 full-suite refusal), brief, group termination | done | tests/test_worker.py (20), stub `tests/stub_claude.py` |
| 5 | `driver.py`, `gitops.py`, `lease.py`, `batches.py`, `bounds.py`, `faults.py`: step lifecycle, Q6 retries, API backoff, scope, gates with parent probe, acceptance + recovery | done | tests/test_driver.py (10: S5, S7, S8, S10, S14, S19 in-process) |
| 6 | `supervisor.py`: queue, admission, spawn, stall checks, reconcile | — | — |
| 7 | `health.py`, `notify.py` | — | — |
| 8 | `cli.py`: project add, submit, status, events, park/resume/amend/ack/cancel, lease | — | — |
| 9 | `release.py`: build, install, launchd | — | — |
| 10 | Stall suite S1-S27, with a stub worker | — | — |
| 11 | M1 run: throwaway consumer, 3-step batch, unattended, supervisor kill mid-batch | — | — |

## Decisions taken while building (judgment calls)

- Q1: CLI dispatch, no SDK, for M1 (P1 step 2; `spikes/p1/findings.md`).
- Tests use a stub worker (a fake `claude` that emits stream-json), so the stall suite runs without MiMo. Live MiMo is used only for the M1 run.
