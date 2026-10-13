# Evidence: what the full suite costs, and whether splitting repos would help

Measured Oct 13, 2026 by Claude Code session ilk2-design-discussion, on chad-mbp and rezmac. It answers Chad's question: "split a big design into several small component repos, so each build only runs its own tests?" The short answer is no, as the main fix. In all three repos the cost comes from suites that are **noisy**, run **in the wrong place**, and run **more than once**. Suite size is not the main cost, and a split would not remove any of these three.

Nothing was run in any of the three repos, and nothing was written to them, to `~/.ilk-data` or to rezmac. All reads were read-only.

## Sources and method

| Source | What was read | Who read it |
|---|---|---|
| V1 per-iteration worker streams: `~/.ilk-data/projects/<key>/logs/runs/*/iter-*.log.jsonl` | Root-session Bash tool calls that invoke a test runner, timed from the `tool_use` event to its `tool_result` | This session, script [`worker_tests.py`](2026-10-13-full-suite-cost/worker_tests.py) |
| V1 batch verification records: `~/.ilk-data/projects/<key>/logs/verification/*-batch.md` | `suite_scope`, `suite_total`, `suite_duration_sec`, `suite_failed`, the at-base rerun section | This session |
| ilk-skills `a00e02c3`, gh-resolve `7e986226`, kira-cloudflare (checkout on `fix/7985-credit-usage-retry`), plus `gh run list` and Claude memory notes | Suite shape, CI, hooks, coupling | Three read-only agents. Their `file:line` citations are theirs; they were not re-opened in this session |

**How a worker call is classified:**
- **files:** names a test file, a `::` node id, or `-k`;
- **dir:** names only directories;
- **whole:** names no path, or is a `bun|npm|pnpm run test…` script with no test file.

This is a heuristic. It counts only foreground calls; background calls are excluded, and so are calls longer than 4 h. It cannot see `bun run <script>` calls whose name does not contain `test`.

**Caveats.**
- Timed driver gate records (`check_observations` in `.ilk-loop.log`) exist only since code-only mode, about Oct 12. There are 14 for ilk-skills and 17 for gh-resolve, against about 900 iterations each. That is too few to measure driver gate time, so it is not reported.
- Stream wall time is the first-to-last event time of the worker session. It excludes the driver's phases.

## 1. Worker-run tests, by host and project

| Project (host) | Iteration streams | Stream wall | Worker test time | files | dir | **whole** |
|---|---:|---:|---:|---|---|---|
| ilk-skills (chad-mbp) | 860 | 179.5 h | 19.3 h (10.8%) | 4,310 calls, 7.2%, mean 11 s | 292 calls, 0.8%, mean 18 s | 359 calls, **2.8%**, mean 50 s |
| gh-resolve (chad-mbp) | 1,099 | 298.4 h | 52.4 h (17.6%) | 6,961 calls, 7.7%, mean 12 s | 435 calls, 2.0%, mean 48 s | 561 calls, **7.9%**, mean 151 s, max 1,159 s |
| kira-cloudflare (chad-mbp) | 249 | 45.9 h | 2.9 h (6.3%) | 829 calls, 3.1% | 50 calls, 0.3% | 89 calls, **2.9%**, mean 53 s |
| kira-cloudflare resolver (rezmac, 253 issue projects) | 616 | 118.7 h | 32.2 h (27.2%) | 838 calls, 3.1%, mean 16 s | 51 calls, 0.0% | 724 calls, **24.1%**, mean 142 s |

ilk-skills on rezmac has 0 iteration streams.

**Readings:**
- The rezmac resolver spends about a quarter of the worker's wall time running the whole consumer suite itself. That is about 2.9 whole-suite runs per issue (724 over 253 issue projects).
- The slowest whole-suite calls on rezmac are `bun run test:non-ui:convex`, cut off at the 600 s Bash tool cap. On ilk-skills they are `pytest` with no paths, also cut off at 600 s. A run cut off at the cap gives the worker no result at all.
- E1 (Oct 12, 24 iterations) measured worker-run tests at 7% of wall time. Over the whole history the share is 10.8-27.2% depending on the project, so E1's sample understated it.

## 2. Batch verification (the full suite at the end of a batch)

| Project (host) | Records | Timed | Median / max | Tests (median) | Records with failures |
|---|---:|---:|---|---:|---|
| ilk-skills (chad-mbp) | 114 | 86 | 334 s / 1,281 s; full-scope runs median 546 s | 4,116 | **72 of 114** (65 of the 72 list failures at the base) |
| gh-resolve (chad-mbp) | 76 | 46 | 394 s / 592 s | 6,691 | **37 of 76** (35 of the 37 list base failures) |
| kira-cloudflare (chad-mbp) | 10 | 3 | 826 s / 828 s | 18,642 | 0 of 10 |
| kira-cloudflare resolver (rezmac) | 48 | 42 | 710 s / 868 s; 8.2 h in total | 24,787 | 3 of 48 |

"Lists base failures" is a text match on the record's at-base section. It is not a verdict that every red was a base red.

From the agents' reports:
- **ilk-skills' full suite has never been green.** 0 of 99 full runs passed, with a median of 10 failing ids per run. Memory `full-suite-reds-need-per-id-attribution` says 19 of 23 reds passed when run alone. The cause was HOME and `ILK_DATA_HOME` leaking between tests.
- **ilk-skills widens most verifies to the full suite.** 71% of its verify runs went full, because `compute_suite_scope` widens on conftest, release-path or unmappable changes.
- **gh-resolve:** 33 of 37 full results from its run logs (Sep 20-Oct 12) had at least one failure.

## 3. Full-suite runs outside the worker (agents' reports)

| Project | Where | Cost |
|---|---|---|
| ilk-skills | CI does not run the Python suite (`ci.yml:52-56`); 60 of 60 recent CI runs failed, on lint and hygiene | None in CI; everything falls on the loop |
| gh-resolve | Main has no CI workflow. Per resolved consumer issue: the consumer's pre-push (median 15.9 min) plus consumer CI; the consumer batch suite has been off since v0.106.0 | 2 full suites per issue, down from 3 |
| kira-cloudflare | `lefthook` pre-push runs the **whole** Convex suite on any `convex/*` change and the whole frontend suite on any `src/*` change, with no selection. CI uses an import-graph selector (`scripts/select-pr-tests.ts`), but only for PRs into dev | One pre-push took 897 s. The resolver gate hit its 300 s bound in 104 of 140 runs (Sep 12-21) |

## 4. Would splitting repos cut what each change runs?

| Repo | What blocks a split, or makes it not pay | Source |
|---|---|---|
| ilk-skills | `skills/ilk-loop` holds 3,753 of about 6,100 test functions (62%), and ilk-watchdog, ilk-ship, ilk-feedback and ilk-launcher import its scripts. The small skills already scope down to about 10 s | agent |
| gh-resolve | `config` (26 dependents), `gh` (22) and `ledger` (21) sit under everything. 200 of 585 test files import `cli` (53 modules) or `pipeline`. The D-69, D-70 and D-156 guards live in one `conftest.py` | agent |
| kira-cloudflare | One `schema.ts` and one deployment (`convex.config.ts`, 66 components). 1,255 of 1,807 Convex test files go through `seedTenant`, which loads the whole module tree; about 40% of shard time is import cost | agent |

**Reading.** In each repo a change to the shared core would still run nearly everything, and a split would add cross-repo version pins and a second release path. It would not fix any of the three measured costs:
- the never-green baseline;
- the worker running the whole suite;
- hooks without selection.

Both selectors that already exist (ilk-skills `gate_scope.py`, kira's `select-pr-tests.ts`) go unused exactly where the cost falls.

**When a split is right:** when the part has its own runtime and deployment, and talks through a versioned contract. ilk2 beside V1, behind the CLI JSON interface, is that kind. Code that shares one schema or one core module is not.

## 5. What this means for ilk2

| # | Item | Status in ilk2 |
|---|---|---|
| F1 | **Exclude base reds by fingerprint (I6).** It is what makes a never-green suite usable: "nothing new failed" is a real answer even when the base is red. It ranks above attribution | Built (§2.5.4) |
| F2 | **Refuse full suites in the worker (Q16).** Section 1's 24% on rezmac is the strongest evidence for it so far | Built, with a gap: see F3 |
| F3 | **A hook gap, found by experiment on Oct 13 against `src/ilk2/worker_hook.py:44-64` at `bfc7976`.** The hook refuses `bun run test:non-ui:convex` and `vitest run -c <config>` with no paths. It **allows** `pytest tests/` and `vitest run -c <config> convex/__tests__`, which is the gate's whole collection named as a directory, because a resolved gate command names no paths to compare against. It also allows `bun run check`, a script whose name lacks `test` (whether that runs tests depends on the project). Directory-wide calls cost gh-resolve 5.8 h over 435 calls (§1) | Open: for the builder session |
| F4 | **Change-based selection for step gates, as a project setting.** A project declares a `selector` command: changed files in, test files out, with a recorded fallback to the full suite. The plan author no longer picks tests by hand. A batch then runs at most one full suite, in its final verification plan | Proposed, for P3 |
| F5 | **Cache base measurements per project, not per batch.** `base_measurements` has the primary key `(batch_id, gate, spec_version, commit_sha)` (`src/ilk2/store.py`, schema). Resolver batches on the same base each pay for the base suite again: 710 s median on rezmac (§2). Keying on (commit, spec version) shares it. Judgment call: wrong if a project's base measurement depends on state outside the commit and the spec version, which the pinned environment is meant to rule out | Proposed, for P3 (before P5 at the latest) |
| F6 | **ilk2's own repo stays green.** Main is gated by CI with no tolerated red baseline (gh-resolve's main has no CI, and ilk-skills' CI failed 60 of 60). No test may reach the real HOME or host state. A fast tier gets a time budget. Today the suite is 114 test functions and CI took 323 s at `96ae7cb` (131 s at `273c0e0`, 6 hours earlier) | Proposed, now |

Outside ilk2, for their owners:
- kira-cloudflare's pre-push could reuse `select-pr-tests.ts`.
- gh-resolve's reap re-runs the consumer's full pre-push after the engine's gate has passed. That belongs in the P5 addendum.
