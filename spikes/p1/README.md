# P1 spike

Exit criterion: [docs/04](../../docs/04-v1-relation-and-migration.md) §4.4. The write-up goes to `docs/spikes/p1.md`.

Spike code lives here, not in `src/ilk2/`. It is evidence, not the core. Anything that graduates into the core is rewritten there, with tests.

| # | Experiment | Passes when |
|---|---|---|
| 1 | CLI dispatch adapter: Claude Code + MiMo under a private config dir, stream-json, session id, timings, resume with red evidence, cancel by process group | One session dispatched, resumed with evidence, killed with 0 surviving processes |
| 2 | Agent SDK adapter, against the same checks (docs/02 §2.6): MiMo compatibility, session identity on resume, hooks, cancellation, timing metrics, sandbox | Each check passes or fails with evidence |
| 3 | `sandbox-exec` profile plus hook | A write outside the worktree, `git commit` and `git push` from the worker are all refused |
| 4 | Brief comparison (Q10): 4 real V1 steps × {discovery, preloaded}, fresh worktrees and sessions, order counterbalanced | Total minutes per accepted step, acceptance and retries recorded for all 8 runs |
| 5 | Write-up and size estimate from this code | Chad reviews at the after-P1 checkpoint |

Rules: never print worker settings, credentials or full process arguments. The ilk2 worker template is its own copy; at runtime it never reads V1 worker homes.
