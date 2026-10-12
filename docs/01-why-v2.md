# 1. Why build V2

V1's recurring failures come from a handful of structural choices, not from individual bugs, and patching has stopped converging. V1's Oct 11-12 redesign did land one of the accepted designs as the default: since v0.9.197, managed Mac runs use code-only mode, where the driver owns gates, commits and the step advance. It took +26.7k lines in two days, layered on the legacy paths rather than replacing them, and the rest of the design (state through CAS, liveness, canonical context) is still open backlog. A new core, small and built around the accepted designs from day one, is cheaper than continuing to retrofit them into V1. §1.4 tests that claim against the obvious alternative: V1 with its legacy paths deleted.

## 1.1 The evidence

| Signal | Measurement | Source |
|---|---|---|
| Backlog not converging | 378 rows: 320 open, 40 shipped (Oct 12); 380 rows, 322 open (Oct 13) | `improvement_backlog.py list --json` |
| Recurring incidents | 29 retros; 116 operational memory notes | `docs/retros/` in V1; Claude memory dir |
| Size | About 110.7k lines of non-test code (Python, Bash, PowerShell); `ilk-loop` alone 62k; runner `run_ilk_loop_claude.sh` 8,695 lines plus a 2,929-line PowerShell twin | `wc -l` at `d17ffdcd` |
| Redesign landed partly, by layering | Oct 11-12 owner build: +26.7k lines in two days. Code-only became the managed-Mac default in v0.9.197: the runner sets `ILK_WORKER_MODE=code-only` when on Darwin with frozen run inputs, and `legacy` otherwise (`run_ilk_loop_claude.sh:5895-5899` at `d17ffdcd`). Legacy paths remain for direct, unpinned and non-Mac callers. Open D3 rows: default worker state through CAS (`0f13f9b73fe854ce`), remaining legacy liveness readers (`c42945c349bea1d9`), canonical context migration (`53e0f8975ceee8b6`) | V1 CHANGELOG v0.9.197; backlog, Oct 13 |
| Speed | 84-93 min per accepted step, all attempts counted; 1 in 5 to 3 in 8 iterations produced nothing | V1 `.ilk-loop.log` close records, Oct 12 |
| Stuck: queued work refused | Sep 15 to Oct 12 on chad-mbp: the scheduler refused queued work for 123 h (ilk-skills) and 124 h (gh-resolve), against 159 h and 161 h spent inside iterations. 69-74% of the refused hours come from three guards added Oct 5-8: release train, batch audit and permits. Whether each refusal was justified was not measured | [Evidence, Oct 12](evidence/2026-10-12-v1-stuck-and-slow.md) §1 |
| Slow: worker time dominates | Oct 12, 24 iterations, 998 min: model thinking ran for 56% of wall time, and 42% passed before the first edit. These overlap. Worker-run tests took 7%, and the driver's audit took 1.9% | [Evidence, Oct 12](evidence/2026-10-12-v1-stuck-and-slow.md) §2 |
| Fixes expose the next defect | "within one day, three consecutive point fixes each exposed the next defect in the same code" | Memory `design-review-pause-2026-09-28` |
| Same day, same class | Two #8309 defects on Oct 12: a mention gate that can never pass, and a finished batch-verification sub-plan that can never ship | Backlog rows `aa9501b25925c890`, `c5a10331421dcbb8` |

## 1.2 The seven root causes

Each root cause below is structural: removing it needs a different shape, not a fix in place. **Status at `d17ffdcd`** says how much of each is still present on V1's managed-Mac default path, as distinct from its legacy paths and history.

1. **The worker owns state.** The model edited plan frontmatter, bumped steps and committed. This led to shipping by frontmatter edit, a forged and re-signed verification record, and a whole batch run in one turn. The worker's step bump became load-bearing. Sources: memory `ship-refusal-bypassed-by-frontmatter-edit`, `worker-forged-verification-record`, `worker-step-bump-is-load-bearing`; retro `a-worker-ran-the-whole-batch-in-one-turn`.
   *Status:* largely addressed on the managed-Mac default since v0.9.197 (code-only: the driver owns gates, commits and the advance). Still present on legacy paths, and default worker state does not yet go through CAS (backlog `0f13f9b73fe854ce`, open). The guards built against this cause (train, audit, permits) are still in place.
2. **State lives in shell globals and in files with many writers.** This causes phase-order bugs, lost updates (`open-w-truncates-before-the-read`, `park_dead_master` rewrites after CAS) and several definitions of "this iteration's work". Sources: `design-review-pause-2026-09-28`; retro `a-subplan-with-two-names`.
   *Status:* present. Frozen per-run inputs and a journaled CAS for some writes landed in v0.9.194; the canonical context migration is open (`53e0f8975ceee8b6`).
3. **Identity is derived, not declared.** Project key, plans dir and execution root are recomputed from paths, which produced worktree-vs-clone keys, empty plans dirs and resolver concurrency stuck at 1 on rezmac. Sources: `plans-dir-keying-has-no-read-side-seam`, `rezmac-host-compat-switch`.
   *Status:* present. rezmac still runs with `host-compat.json`.
4. **Liveness is guessed.** pgrep, argv patterns, PID files and wrapper PIDs: kills miss process groups, the scheduler races the postmortem, and a guard cannot tell a fixture from a real loop. Sources: `agent-kill-misses-gtimeout-pgid`, `runner-launched-pid-is-a-wrapper`, `scheduler-races-the-postmortem-writer`.
   *Status:* partly addressed (run registry on most paths since Oct 12); remaining readers open (`c42945c349bea1d9`).
5. **Gates are fragile.** Some gates pass vacuously; some can never pass because of reds already at base. Some added checks guess at the test runner, and "could not measure" gets read as red or green. Sources: `step-heading-colon-makes-gates-vacuous`, `verify-step1-pytest-gate-cannot-pass-a-base-red`; retro `a-gate-that-could-not-pass-read-as-a-timeout`; backlog `a4293ee802516895`.
   *Status:* present. Both Oct 12 #8309 defects are this class.
6. **The deploy was a live checkout, and the toolkit modifies itself.** Moving the clone moved every loop, hand commits break batch audits, and self-mod batches strand commits. Sources: `moving-the-clone-moves-every-loop`, `no-hand-commits-inside-a-live-batch`; retro `the-selfmod-batch-could-not-land`.
   *Status:* the deploy half is addressed: both hosts run from release directories (`~/.ilk/current` → `releases/v0.9.198`, checked Oct 13). Self-modification and hand commits interacting with batch audits remain.
7. **The orchestrator is Bash, with a PowerShell twin.** This causes BSD/GNU tool differences, an unquoted heredoc that executed code, and parity drift (a failed brief is ignored in Bash but throws in PowerShell). Sources: `declared-gate-timeouts-ignored-on-macos-awk`, `unquoted-heredoc-executes-backticks`.
   *Status:* present. The runner is 8,695 lines of Bash at `d17ffdcd`.

### What the root causes cost in hours

The two problems felt most are **stuck loops** and **slow steps**, and they have different sources:

- **Most refused hours come from V1's own guards.** V1 added guards before dispatch because it could not trust state the worker wrote (root cause 1) or its gates (root cause 5): a release train, batch audits, permits, a blacklist and a no-progress bound. On chad-mbp, from Sep 15 to Oct 12, the scheduler refused queued work for almost as many hours as the loop worked, about 0.8 h per hour worked. The train, audit and permits account for 69-74% of those hours. Whether each refusal was necessary was not measured, so "the guards are unnecessary" is an inference: in a core where the driver ships by accepting the last step, they have nothing left to re-check. See invariant I10 in [document 2](02-architecture.md).
- **Slow steps are mostly worker time.** The engine's own overhead within a run is small. Model thinking ran for 56% of iteration wall time, and 42% passed before the first edit. Those spans overlap, and the time before the first edit also includes start-up, prompt processing and tool latency, so it is not all discovery. The engine can't make the model faster, but it decides what the worker is given. [Document 3](03-v1-vs-v2.md) §3.4 brings this into scope as a hypothesis to measure.

## 1.3 Why not keep patching V1

- **Each root cause is spread across many files.** For example, liveness is checked by the scheduler, watchdog, stop script, status, release, permits and preflight separately (D1 audit, Oct 12). Moving every reader means touching the busiest, least-tested code under live batches.
- **V1 must keep serving while it changes.** That forces every change through side refs, batch boundaries and two-host releases, and makes each fix slow.
- **The owner build showed the cost of retrofitting.** The new mechanisms were written and tested (`iteration_context.py`, `plan_state.py`, `run_registry.py`, `run_inputs.py`, `driver_step.py`). Making code-only the default took two days and +26.7k lines, and it sits beside the legacy paths rather than replacing them. The remaining migrations (three D3 rows above) touch the same busy code.

## 1.4 Is V1 with its legacy paths deleted effectively V2?

This is the strongest alternative, and since v0.9.197 it is a real one. Deleting V1's legacy worker paths would finish root cause 1 and shrink the runner. It would not address:
- **The refusal guards** (train, audit, permits), which are scheduler-level and still apply under code-only, and were 69-74% of refused hours.
- **Derived identity** (root cause 3), which still needs `host-compat.json` on rezmac and keeps the resolver serial.
- **Guessed liveness** (root cause 4) in the readers that remain.
- **Fragile gates** (root cause 5): base reds by test ID only, and an added check that guesses the runner.
- **A Bash orchestrator of about 8.7k lines** (root cause 7), with no bound or progress guarantee for its own phases.

Each could be retrofitted in turn, but every one of them touches the runner and scheduler under live batches, which is the cost §1.3 describes. V2's case rests on these five, not on root cause 1.

## 1.5 Why not a rewrite of everything

V2 is deliberately **a core, not a full replacement.**

- **V1's planning and feedback tools work and stay.** That covers `/ilk-plan`, `ilk-spec`, `ilk-feedback`, the backlog, and inbox and Lark tickets. V2 replaces only the engine that executes batches. See [document 4](04-v1-relation-and-migration.md).
- **Some pain is outside the engine:** the model's raw speed, slow consumer test suites, and the gh-resolve contract. A new engine does not fix those. [Document 3](03-v1-vs-v2.md) lists them as non-goals.
- **V2 reuses V1's best work.** The owner-build modules are copied in (with provenance) and cut down, not rewritten.
