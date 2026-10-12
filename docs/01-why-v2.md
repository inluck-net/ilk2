# 1. Why build V2

V1's recurring failures come from a handful of structural choices, not from individual bugs. Patching has stopped converging, and V1's own redesign (Oct 11-12) could not make its core the default. A new core, small and built around the accepted designs from day one, is cheaper than continuing to retrofit them into V1.

## 1.1 The evidence

| Signal | Measurement | Source |
|---|---|---|
| Backlog not converging | 378 rows: 320 open, 40 shipped | `improvement_backlog.py list --json`, Oct 12 |
| Recurring incidents | 29 retros; 116 operational memory notes | `docs/retros/` in V1; Claude memory dir |
| Size | About 110.7k lines of non-test code (Python, Bash, PowerShell); `ilk-loop` alone 62k; runner `run_ilk_loop_claude.sh` 8,695 lines plus a 2,929-line PowerShell twin | `wc -l` at `d17ffdcd` |
| Redesign could not land as default | Oct 11-12 owner build: +26.7k lines in two days. The driver-owned "code-only" path stayed opt-in: the scheduler never sets `ILK_WORKER_MODE` | Owner-build audit, Oct 12 |
| Speed | 84-93 min per accepted step, all attempts counted; 1 in 5 to 3 in 8 iterations produced nothing | V1 `.ilk-loop.log` close records, Oct 12 |
| Stuck: queued work refused | Sep 15 to Oct 12 on chad-mbp: the scheduler refused queued work for 123 h (ilk-skills) and 124 h (gh-resolve), against 159 h and 161 h spent inside iterations. 69-74% of the refused hours come from three guards added Oct 5-8: release train, batch audit and permits | [Evidence, Oct 12](evidence/2026-10-12-v1-stuck-and-slow.md) §1 |
| Slow: worker time dominates | Oct 12, 24 iterations, 998 min: model thinking ran for 56% of wall time, and 42% passed before the first edit. Worker-run tests took 7%, and the driver's audit took 1.9% | [Evidence, Oct 12](evidence/2026-10-12-v1-stuck-and-slow.md) §2 |
| Fixes expose the next defect | "within one day, three consecutive point fixes each exposed the next defect in the same code" | Memory `design-review-pause-2026-09-28` |
| Same day, same class | Two #8309 defects on Oct 12: a mention gate that can never pass, and a finished batch-verification sub-plan that can never ship | Backlog rows `aa9501b25925c890`, `c5a10331421dcbb8` |

## 1.2 The seven root causes

Each root cause below is structural: removing it needs a different shape, not a fix in place.

1. **The worker owns state.** The model edits plan frontmatter, bumps steps and commits. This led to shipping by frontmatter edit, a forged and re-signed verification record, and a whole batch run in one turn. The worker's step bump became load-bearing, so it cannot simply be removed. Sources: memory `ship-refusal-bypassed-by-frontmatter-edit`, `worker-forged-verification-record`, `worker-step-bump-is-load-bearing`; retro `a-worker-ran-the-whole-batch-in-one-turn`.
2. **State lives in shell globals and in files with many writers.** This causes phase-order bugs, lost updates (`open-w-truncates-before-the-read`, `park_dead_master` rewrites after CAS) and several definitions of "this iteration's work". Sources: `design-review-pause-2026-09-28`; retro `a-subplan-with-two-names`.
3. **Identity is derived, not declared.** Project key, plans dir and execution root are recomputed from paths, which produced worktree-vs-clone keys, empty plans dirs and resolver concurrency stuck at 1 on rezmac. Sources: `plans-dir-keying-has-no-read-side-seam`, `rezmac-host-compat-switch`.
4. **Liveness is guessed.** pgrep, argv patterns, PID files and wrapper PIDs: kills miss process groups, the scheduler races the postmortem, and a guard cannot tell a fixture from a real loop. Sources: `agent-kill-misses-gtimeout-pgid`, `runner-launched-pid-is-a-wrapper`, `scheduler-races-the-postmortem-writer`.
5. **Gates are fragile.** Some gates pass vacuously; some can never pass because of reds already at base. Some added checks guess at the test runner, and "could not measure" gets read as red or green. Sources: `step-heading-colon-makes-gates-vacuous`, `verify-step1-pytest-gate-cannot-pass-a-base-red`; retro `a-gate-that-could-not-pass-read-as-a-timeout`; backlog `a4293ee802516895`.
6. **The deploy is a live checkout, and the toolkit modifies itself.** Moving the clone moves every loop, hand commits break batch audits, and self-mod batches strand commits. Sources: `moving-the-clone-moves-every-loop`, `no-hand-commits-inside-a-live-batch`; retro `the-selfmod-batch-could-not-land`.
7. **The orchestrator is Bash, with a PowerShell twin.** This causes BSD/GNU tool differences, an unquoted heredoc that executed code, and parity drift (a failed brief is ignored in Bash but throws in PowerShell). Sources: `declared-gate-timeouts-ignored-on-macos-awk`, `unquoted-heredoc-executes-backticks`.

### What the root causes cost in hours

The two problems felt most are **stuck loops** and **slow steps**, and they have different sources:

- **Stuck time is mostly self-inflicted.** V1 can't trust state the worker wrote (root cause 1) or its gates (root cause 5), so it added guards before dispatch: a release train, batch audits, permits, a blacklist and a no-progress bound. On chad-mbp, from Sep 15 to Oct 12, these refused queued work for almost as many hours as the loop worked: about 0.8 h refused per hour worked. A core that cannot be lied to doesn't need most of these guards. See invariant I10 in [document 2](02-architecture.md).
- **Slow steps are mostly worker time.** The engine's own overhead within a run is small. Thinking and finding its way around the repo before the first edit make up most of a step. The engine can't make the model faster, but it decides how much the worker must discover for itself. [Document 3](03-v1-vs-v2.md) §3.4 brings this into scope.

## 1.3 Why not keep patching V1

- **Each root cause is spread across many files.** For example, liveness is checked by the scheduler, watchdog, stop script, status, release, permits and preflight separately (D1 audit, Oct 12). Moving every reader means touching the busiest, least-tested code under live batches.
- **V1 must keep serving while it changes.** That forces every change through side refs, batch boundaries and two-host releases, and makes each fix slow.
- **The owner build showed the cost of retrofitting.** The new mechanisms were written and tested (`iteration_context.py`, `plan_state.py`, `run_registry.py`, `run_inputs.py`, `driver_step.py`), but wiring them in as the default meant migrating every legacy path at once. That is where it stalled.

## 1.4 Why not a rewrite of everything

V2 is deliberately **a core, not a full replacement.**

- **V1's planning and feedback tools work and stay.** That covers `/ilk-plan`, `ilk-spec`, `ilk-feedback`, the backlog, and inbox and Lark tickets. V2 replaces only the engine that executes batches. See [document 4](04-v1-relation-and-migration.md).
- **Some pain is outside the engine:** the worker model's thinking time, slow consumer test suites, and the gh-resolve contract. A new engine does not fix those. [Document 3](03-v1-vs-v2.md) lists them as non-goals.
- **V2 reuses V1's best work.** The five owner-build modules are copied in (with provenance) and cut down, not rewritten.
