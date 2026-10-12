# 2. Architecture

ilk2 is one supervisor process per host. It starts one driver process per run, and each driver starts one worker at a time. All state goes through a single SQLite store. The worker edits code in a worktree; everything else is the driver's job.

## 2.1 Invariants

These are the rules every component must keep. Each one exists because V1 broke it; the V1 root causes are numbered as in [document 1](01-why-v2.md).

| # | Invariant | Removes V1 root cause |
|---|---|---|
| I1 | **Only the driver changes batch state** (step pointer, plan status, gate results, commits). The worker has no write path to state. | 1, 2 |
| I2 | **All state changes are SQLite transactions**, each with an actor, a reason and the previous version. Markdown is never written back. | 2 |
| I3 | **Identity is declared once, at submission:** project id, batch id, run id, execution root. Nothing is derived from paths afterwards. | 3 |
| I4 | **The supervisor owns only processes it started:** each driver is a child in its own process group, with PID and start time in the store. A restarted supervisor never adopts survivors. It terminates them (verified by PID and start time) and requeues their runs (§2.10). No pgrep or argv matching anywhere. | 4 |
| I5 | **A gate result is `pass`, `fail` or `unmeasured`.** `unmeasured` (harness error, no tests found, runner unknown) is never treated as pass or fail. | 5 |
| I6 | **A failure counts against a step only if it is new.** Base failures are measured per versioned gate specification and identified by test ID plus failure fingerprint, so a base-red test that starts failing differently counts as new (§2.5). | 5 |
| I7 | **Code runs only from immutable releases.** A run records its release; upgrades happen at step boundaries. ilk2 never edits its own release. | 6 |
| I8 | **One language (Python 3.12), one platform (macOS).** | 7 |
| I9 | **Every terminal state has a named reason.** No state is "stuck" without a reason a human can read in `ilk2 status`. | Oct 12 #8309 deadlock and "timeout" mislabel |
| I10 | **Every refusal to dispatch queued work is a recorded row.** The row names the reason, the action that clears it, and who takes that action (`supervisor`, `driver` or `human`). Refused hours are reported. A refusal whose clearing action is `supervisor` or `driver` must clear without a human. No guard exists only to re-check what the driver already wrote. | V1 refused queued work for about 0.8 h per hour worked ([evidence](evidence/2026-10-12-v1-stuck-and-slow.md) §1) |
| I11 | **Every non-terminal state has a time bound and an action when the bound passes,** including the supervisor itself, which is watched from outside (§2.11). The action is automatic, or it creates a `needs_human` row that stays open until acknowledged. Every retry loop has a counted limit. | V1's stalls: a hung runner, escalation loops, relaunch storms, a blocked dependency re-dispatched all night (§2.11) |
| I12 | **Git and the store are never assumed atomic together.** Accepting a step is an idempotent protocol in three phases: intent, commit, finalize. Recovery finishes or undoes it (§2.4.1). | Review of Oct 13: a crash between the commit and the state change duplicates or loses work |

## 2.2 Components

```mermaid
flowchart TB
  subgraph Host["One Mac (chad-mbp or rezmac)"]
    LD[launchd KeepAlive] --> SUP
    HLT["launchd StartInterval 60 s<br/>ilk2 health"] -. tick age stale → kickstart -k .-> SUP
    subgraph REL["~/.ilk2/releases/vX (immutable, bundled Python 3.12)"]
      SUP["Supervisor<br/>queue · admission · process ownership · upgrades"]
      DRV1["Driver (run A)<br/>step lifecycle · gates · commits"]
      DRV2["Driver (run B)"]
    end
    DB[("~/.ilk2/data/state.db<br/>SQLite WAL")]
    SUP -- spawns, waits on --> DRV1
    SUP -- spawns, waits on --> DRV2
    DRV1 -- spawns per attempt --> W1["Worker<br/>Claude Code + MiMo<br/>sandboxed to worktree"]
    DRV1 -- runs --> G1["Gate commands<br/>in worktree"]
    SUP <-- transactions --> DB
    DRV1 <-- transactions --> DB
    DRV2 <-- transactions --> DB
    HLT -- reads tick age --> DB
    W1 -- edits only --> WT1["Execution worktree<br/>(declared root)"]
  end
  CLI["ilk2 CLI<br/>submit · status --json · amend · park · ack"] <--> DB
  CLI -. signal .-> SUP
  CONS["Consumers<br/>gh-resolve · /ilk2-plan · humans"] --> CLI
```

| Component | Responsibility | Never does |
|---|---|---|
| **Supervisor** (`ilk2 supervise`) | Admits submitted batches. Decides what runs by priority, then submission order, and keeps concurrency limits per host and per project. Records every refusal with its clearing action (I10). Spawns drivers and waits on them. Writes a tick timestamp every 30 s. On start-up, terminates surviving drivers and requeues their runs. Performs upgrades and drains. | Run gates, touch worktrees, call the model, adopt a process it did not start, or add a pre-dispatch audit of driver-written state |
| **Health job** (`ilk2 health`) | A separate launchd job every 60 s. If the supervisor's last tick is older than the bound, or its process is gone, it restarts the supervisor with `launchctl kickstart -k`. It also re-sends unacknowledged `needs_human` notices that are due | Touch runs or batches |
| **Driver** (`ilk2 drive <run_id>`) | Owns one run: prepares the worktree, freezes inputs, dispatches the worker, checks the diff's scope, runs gates, accepts the step (§2.4.1), retries with red evidence, and records the terminal reason | Choose which batch runs; edit another run's state |
| **Worker** | A Claude Code session with MiMo, under a private config dir, reached through the dispatch adapter (§2.6). Edits files in the worktree to complete one step | Write the store, plans, or anything outside the worktree; run `git commit` or `push`; decide step progress |
| **Store** | One SQLite file holding projects, batches, plans, steps, runs, attempts, gate results, transitions, events and `needs_human` rows | Hold secrets |
| **CLI** | Submission, status (human and `--json`), amend, park, resume, cancel, ack, lease acquire/release, export, doctor, upgrade | Bypass the transition API |

## 2.3 State model

```mermaid
erDiagram
  PROJECT ||--o{ BATCH : has
  BATCH ||--o{ PLAN : contains
  PLAN ||--o{ STEP : contains
  BATCH ||--o{ RUN : "executed by"
  RUN ||--o{ ATTEMPT : makes
  ATTEMPT ||--o{ GATE_RESULT : records
  STEP ||--o{ ATTEMPT : "targeted by"
  BATCH ||--o{ TRANSITION : journals
  BATCH ||--o{ NEEDS_HUMAN : raises
```

- **PROJECT:** id, repo root, default branch, gate adapters, concurrency limit, setup hook (§2.5), and notification policy (`owner` or `consumer`; §2.11.3). Registered explicitly with `ilk2 project add`.
- **BATCH:** id, project, base commit, priority, consumer reference (for example a gh-resolve issue number, echoed in every event), status (`queued → active → shipped | blocked | parked | cancelled`), an immutable normalized plan snapshot (Q3), and the original imported documents.
- **ROOT LEASE:** the execution root's holder (`driver`, `consumer` or `released`), owner, generation, expiry, expected head and accepted head. It changes only by compare-and-set on the generation. Only the holder writes to the root's branch (§2.4.2).
- **PLAN** (V1 "sub-plan"): order, dependencies, status, `verification: true|false`, and for a repair plan `repair_of_step_id` (§2.5.7). A batch ships only when every plan, repair plans included, has shipped.
- **STEP:** index, instructions, declared gates with their specification version, expected reds, status (`pending → accepting → accepted`, or `blocked`).
- **RUN:** one driver process lifetime. Holds PID, PGID, start time, host boot time, release version, execution root and terminal reason.
- **ATTEMPT:** one worker dispatch for one step. Records session id, timings (model, thinking, tools, gates), diff hash, expected tree hash and outcome.
- **TRANSITION:** append-only journal: entity, from, to, actor, reason, previous version. The only way state changes.
- **NEEDS_HUMAN:** state, reason, created, last notified, acknowledged by and when. Stays open until `ilk2 ack` or the state it describes clears.

Concurrency control: every transition checks the row's version, so it is a compare-and-set. A run holds a lease on its batch, and only the lease holder may move that batch's steps. The store opens with a busy timeout, and transactions are short (no I/O inside them other than SQLite).

## 2.4 How a step runs

```mermaid
stateDiagram-v2
  [*] --> Prepare
  Prepare: Prepare worktree at step base, freeze inputs
  Prepare --> Dispatch
  Dispatch: Worker edits code (one session)
  Dispatch --> ScopeCheck
  ScopeCheck: Diff only inside declared scope?
  ScopeCheck --> Gates: yes
  ScopeCheck --> Retry: no (evidence: out-of-scope paths)
  Gates: Run declared gates on the worktree
  Gates --> Accept: no new failures (from P4: or only attributed ones)
  Gates --> Retry: new failure
  Gates --> Blocked: unmeasured (permanent)
  Retry: Resume same session with evidence
  Retry --> Dispatch: attempts left
  Retry --> Blocked: attempts exhausted
  Accept: Intent → commit → finalize (§2.4.1)
  Accept --> Prepare: more steps
  Accept --> PlanShipped: last step of plan
  PlanShipped --> [*]
  Blocked --> [*]
```

What this removes from V1:
- **Plans ship themselves.** When the driver finalizes a plan's last step, the plan is shipped in that same store transaction. No separate worker ship, `gate_first` marker or ship script is needed, so the Oct 12 #8309 deadlock cannot occur.
- **Batch verification is a plan like any other.** Its steps run gates; it has no special dispatch rules.
- **Timeouts are real timeouts.** An attempt that dispatched nothing cannot exist: the driver either dispatches or ends the run with a named reason (I9).

### 2.4.1 Accepting a step (I12)

Git and SQLite cannot share a transaction, so acceptance has three phases. Each one can be retried safely.

1. **Intent (store transaction):** the step moves `pending → accepting`, recording the attempt id, the parent commit and the expected tree hash (the gated worktree's tree).
2. **Commit (git):** the driver creates the commit with an `ilk2-attempt: <attempt id>` trailer. It does not commit if a commit with that trailer already exists on the branch with the expected tree and parent.
3. **Finalize (store transaction):** the step moves `accepting → accepted` with the commit SHA. If it is the plan's last step, the plan ships in the same transaction.

**Recovery**, run by the next driver before it prepares anything, for any step left `accepting`:

| Found in git | Action |
|---|---|
| A commit with the attempt's trailer, expected tree and expected parent | Finalize with that SHA. No work is redone |
| No such commit; branch head still at the recorded parent | Undo: the step goes back to `pending`, and the next attempt starts from the parent |
| A commit with the trailer but a different tree or parent, or the branch moved elsewhere | `blocked (acceptance-mismatch)`, with both trees recorded. A human decides. This should be impossible; it means something outside ilk2 wrote to the branch |

No step is ever `accepted` without its commit, and no commit with an `ilk2-attempt` trailer is ever left without a finalized step or a recorded mismatch.

### 2.4.2 Who may write the branch

A consumer may also push from the execution root. gh-resolve's reap and land push the issue branch from the same worktree (gh-resolve `push.py`). Two writers on one branch would turn every consumer push into an `acceptance-mismatch` (§2.4.1). An advisory "released" flag is not enough: a consumer could read it, `resume` could take the lease back, and the consumer would then push into a live run. So the execution root has a **lease acquired by compare-and-set**, with a generation number:

```
driver(gen n) --release--> released(gen n+1) --consumer acquire(gen n+1, expected_head)--> consumer(owner, gen n+2, expected_head)
consumer(gen n+2) --consumer release(gen n+2, new_head)--> released(gen n+3) --resume--> driver(gen n+4)
```

- **The driver holds the lease while the batch is `queued` or `active`.** It releases the lease when the batch reaches `shipped`, `blocked`, `parked` or `cancelled`, after any running attempt has been stopped and recovered.
- **A consumer acquires the lease before writing:** `ilk2 lease acquire --root <root> --generation <g> --expected-head <sha> --owner <name>`. This succeeds only if the lease is `released` at generation `g` and the root's branch head equals `expected-head`. It returns the new generation.
- **The consumer's push is conditional on the head it recorded.** It pushes only if the remote branch still points at the expected remote head, which gh-resolve's non-force push already enforces. When done it calls `ilk2 lease release --generation <g> --new-head <sha>`, recording the local and remote heads it left.
- **`ilk2 resume` fails while a consumer holds the lease.** Resume acquires the lease by the same compare-and-set from `released`. If the local branch or the recorded remote head moved since the last driver release, resume records the consumer's commits as the new step base (a `consumer-commits` transition), so they are never mistaken for a mismatch.
- **A consumer lease has a time limit and can be renewed.** A consumer doing a long operation calls `ilk2 lease renew --generation <g>` before the limit (§2.11.1). Expiry does **not** release the lease: it moves to `consumer-expired`, which still fences the driver. `resume` fails in that state, and a consumer that is still working can finish, push and release normally. Writer authority returns to `released` only by the consumer's own `ilk2 lease release`, or by `ilk2 lease recover --generation <g>` after an operator or the consumer confirms no consumer process can still write. Expiry creates a `needs_human` row under the project's notification policy (Codex review 3, finding 2).
- **`ilk2 status --json` reports** `root_lease` (holder, owner, generation, expiry) and `accepted_head`.

## 2.5 Gates

### 2.5.1 What a gate is

- **Declared gates are commands** with an explicit adapter (`pytest` or `vitest` for test gates; `shell` for checks that are not tests), a working directory and a time cap.
- **Package scripts are resolved.** A gate written as a package script (`bun run test:non-ui:convex`, `npm test`) is read from `package.json` and resolved to the runner command it calls, with that runner's adapter. gh-resolve's consumer gates are of this kind, with `-c tests/convex-tests/vitest.config.ts`.
- **`shell` cannot be a test gate.** A shell gate reports no test IDs, so base exclusion and fingerprints cannot apply to it, and a shell gate that is red at the base could never pass. `ilk2 submit` rejects a step whose test gate does not resolve to a test adapter. `shell` is kept for checks with plain exit-code meaning (lint, type check, build), which must be green at the base.
- **Each gate has a specification version:** a hash of the resolved command, adapter, working directory, pinned environment, and every configuration file that decides what runs. That includes `package.json` for a resolved script, the runner config, and any workspace or projects config.
- **The vitest adapter** finds the owning config by evaluating each config's `include` and `exclude` against the file, and resolves workspace and projects configs. If a file matches several configs, every matching config is run. If it matches none, it is recorded as `uncovered`. Added checks (V1's "mention gate") come only from this resolution, never from a bare `vitest run`.
- **The pytest adapter** uses the project's rootdir and its configured plugins.

### 2.5.2 The pinned environment

Gates run outside the worker sandbox but in a pinned environment, defined so that a test cannot see the host's ilk state:
- `HOME` is a private directory per run, created empty, with only the files the project declares copied in.
- All `ILK_*` and `ILK2_*` variables are removed. The paths `~/.ilk-data`, `~/.ilk` and `~/.ilk2` are therefore not reachable through `HOME`.
- `PATH` is the project's declared `PATH` prelude plus the release's bundled tools, and nothing else.
- Every other variable comes from an explicit per-project allow-list. Anything not listed is absent.

On Oct 12, 36 V1 loop-gate reds did not reproduce in an isolated copy, because tests leaked through `ILK_SKILL_HOME`, `ILK_CODE_ROOT` and `~/.ilk-data` (gh-resolve review, finding F7).

### 2.5.3 Setup hook

A project may declare a setup command, for example `bun install --frozen-lockfile`. It runs in the execution root under the pinned environment, before the base measurement and after any step that changes the lockfile. It has its own time cap (§2.11.1). If it fails, the batch is `blocked (setup)` with its output; it never becomes `base-unmeasured`.

### 2.5.4 Base measurement and new failures

- **The base:** the driver runs each gate at the batch base and stores the failing tests, each as a test ID plus a **failure fingerprint**. Fingerprints are adapter-specific. Each is a hash of:
  - the exception or failure type;
  - a digest of the assertion or error message, with only **recognized volatile values** replaced by placeholders: ISO and epoch timestamps, temporary paths, memory addresses, UUIDs and generated ids, process ids and ports. Assertion operands such as "expected 1, got 2" are kept, so different assertions stay different;
  - the top three normalized frames inside the project.

  So two different assertion failures at the same line get different fingerprints, while line-number drift and temporary paths do not change them.
- **A failure is new** when its test ID was not failing at the base, when it was failing at the base but its fingerprint changed, or when the test did not exist at the base and is not declared as an expected red.
- **When a step changes a gate's specification** (a new test file, a changed runner config, the gate's own `package.json` script entry), the new specification is generally not portable to the base (§2.5.7): there is **no comparable base** for it. That gate's failures at that step are all charged to the current step, and attribution is unavailable for them. The one exception is a pure addition: new test files with the runner, config and environment unchanged. Then the base is measured in a diagnostic worktree at the base commit, the new tests are absent there ("0 tests"), and every collected new test counts as new. To limit spurious changes, the specification hashes only the gate's own `package.json` script entry, not the whole file. Plan authors should put gate-specification changes in a step of their own (Codex review 3, finding 4).
- **In M1, and until attribution lands before P4, every new failure is judged against the current step,** as in V1. The driver also records, for each new failure, whether it was already present at the current step's parent (one probe of the failing IDs in a diagnostic worktree at the parent commit; diagnostic worktrees are part of M1, §2.5.7). If it was, the failure is marked `possibly-inherited` in the attempt record and in the worker's red evidence, so the worker is told it may be repairing earlier damage. This is evidence only; it changes neither the verdict nor who repairs.
- **Before P4, attribution replaces that rule** (§2.5.7). P4 is where gh-resolve's own batches first run on ilk2, and cross-step damage (gh-resolve review F8) is where it cost the most.
- Every measurement is fresh, at the base, a parent or a probe commit, and never reuse of an earlier gate result. V1's false-green history (07m R1/R2) stays excluded.

### 2.5.5 Outcomes

A gate's result is `pass`, `fail` (with the new failures), or `unmeasured` (with the reason).

| Situation | Classified as |
|---|---|
| Harness crashed or killed by a signal; gate hit its own cap while the host was under load | `unmeasured`, transient. Re-run once on the same tree, with no new worker attempt |
| A single test hit its own runner timeout (vitest `testTimeout`, pytest `--timeout`), or an xdist worker crashed | A **timeout-class failure**: its fingerprint is the class, not the first frame, which is unstable for these. That test is re-run once alone, after the gate finishes. It counts as new only if it fails again on that re-run (gh-resolve review F6: 2 of 3 consumer pre-push runs failed this way, each on a different test) |
| Runner unknown, command not found, second run also unmeasured | `unmeasured`, permanent. The step is blocked with the reason |
| Collection error | Recorded **per file**. A file that errors at the base too is a base failure, not a batch blocker. A file that errors only after the step is a new failure |
| pytest exits non-zero with 0 failing tests (for example gh-resolve's D-69 unexpected-skip guard) | `fail`, with reason `nonzero-exit-no-failures` and the exit code and summary line as evidence, so the worker can see it. It is not `unmeasured`, and not a pass |
| No tests collected | `fail` only if the step declared tests for this gate; otherwise the gate is recorded as `uncovered` |

### 2.5.6 Expected reds

A red-first step declares the test IDs it expects to fail (`expect: red`), and optionally their fingerprints. The gate then passes only if exactly those IDs fail and nothing else is new. A later step that turns them green clears the expectation. (V1 retro `a-red-first-step-gated-on-green`: correct work was failed by its own gate, and the project was blacklisted for an hour.)

### 2.5.7 Diagnostic worktrees (M1) and attribution (before P4)

The feature is split in two (Codex review 3, finding 6).

**Diagnostic worktrees: required for M1.** Every base, re-base and parent measurement runs in an isolated diagnostic worktree, never in the execution root, so the current attempt's uncommitted changes are untouched.
- Each diagnostic worktree is keyed by (commit, gate specification version).
- The setup hook runs in it from that commit's own lockfile, so old source is never tested against later dependencies.
- Diagnostic worktrees are cached for the batch, and removed when it ends.

**Attribution: deferred to before P4** (Chad, Oct 13). Multi-commit attribution, repair plans and `pass-with-attributed` are not in M1. Their entry condition is P4's ([document 4](04-v1-relation-and-migration.md) §4.4), with the suite A1-A10 (§2.11.4).

**When attribution is possible.** A gate specification is portable across a commit boundary only if its resolved command, adapter, pinned environment and every configuration file it hashes are identical at both commits, and the failing test files exist at both. If not, the result is `unattributable (spec-changed)`. The tool never overlays new tests or config onto old source.

**The attribution phase.** When an attempt's gates show new failures that were already present at the step's parent, the driver enters an `attribute` phase. For each failure:
1. **Walk backwards.** Starting from the step's parent (known failing), probe the failing test IDs at each earlier accepted commit of the batch, newest first, until the first commit where the failure is absent. That commit and its child form the edge (last clean commit, first failing child). The child's step introduced the failure. Walking backwards, never binary search, means the edge found is always the **most recent** clean-to-failing transition, even if the history went clean, failing, fixed and failing again (Codex review 3, finding 3).
2. **Confirm it.** Re-probe both sides of the edge twice more. The failure must be absent on the clean side and present on the failing side every time. If any probe disagrees, the result is `unattributable (flaky)`.
3. **If the walk reaches the batch base and the failure is still there,** it is a base failure whose fingerprint changed, and it is judged against the current step.
4. **Budget.** Every probe run counts, confirmation probes included. Results already in the cache, keyed by (commit, specification version, test ID), are reused and do not count. The limit is 12 probes per failure and 20 minutes of wall time for the whole phase. The cache persists across requeues. When the budget is spent, the result is `unattributable (budget)`.

**What each result does:**

| Result | Current step | Repair |
|---|---|---|
| Attributed to step *k* of plan P | Passes if its only failures are attributed ones (`pass-with-attributed`, recorded with the failing IDs) | The driver adds a **repair plan** to the batch (amend, actor `driver`, reason `regression-attributed`), with `repair_of_step_id = k`, the failing IDs and the edge as its brief. Repair plans are deduplicated by (test ID, fingerprint, edge): a second attribution of the same failure to the same edge joins the existing repair plan. A repair plan is a **batch barrier**: it runs at the highest priority, and no other plan in the batch starts a new step until it ships. All plans share one branch, and no impact graph exists to prove a plan unaffected. P stays `shipped`; nothing is reopened. The batch cannot ship until every repair plan has shipped |
| `unattributable (spec-changed / flaky / budget)` | Judged on all new failures, as in M1 | None. The reason is recorded and reported in `ilk2 report` |

## 2.6 Worker isolation

- **Private Claude config dir per run,** copied from a pinned template: model `mimo-v2.6-pro`, `CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`, the hooks, and an MCP allow-list.
- **macOS `sandbox-exec` profile:** writes allowed only to the worktree and a per-attempt temp dir. The store, plans, `~/.ilk2/releases` and `.git` internals are read-only to the worker.
- **A hook refuses `git commit`, `git push` and edits outside the worktree**, as a second layer behind the sandbox.
- **The worker runs targeted tests only.** The brief tells it to run only the tests for the files it changed. A hook refuses any test command that resolves to the whole suite of a declared gate, with a message saying the driver runs that gate after the attempt. (gh-resolve review M1: the worker's own full-suite run plus the driver's gate cost about 13 minutes per iteration on rezmac. Chad approved the hook on Oct 13.)
- **The brief** contains the step instructions, the declared gates, the previous attempt's red evidence, and paths the worker may read. It contains no plan files the worker could edit.
- **The brief can preload context.** In V1, 42% of iteration wall time passed before the first edit ([evidence](evidence/2026-10-12-v1-stuck-and-slow.md) §2). That span includes model thinking, start-up, prompt processing and tool latency, not only discovery, so preloading's effect is unknown. The driver can assemble, at no model cost, the files and symbols the step names, their tests, the failing output, and the earlier steps' diff. Whether that lowers total time per accepted step is open question Q10, tested in P1 with a counterbalanced comparison over several steps.
- **Dispatch adapter (Q1):** one interface (`start`, `resume`, `cancel`, `stream events`) with the Claude Code CLI as the baseline implementation, since it already works with MiMo. A Claude Agent SDK implementation is adopted only if P1 proves, with MiMo: compatibility, stable session identity on resume, hook parity, cancellation by process group, streaming timing metrics, and the same sandbox behaviour.

## 2.7 Releases and upgrades

- **A release** is `~/.ilk2/releases/<version>/`: the code, a uv-built virtualenv and a manifest with hashes. `~/.ilk2/current` points at one release. launchd starts the supervisor and the health job from `current`.
- **Upgrades drain at step boundaries:** `ilk2 upgrade <version>` tells the supervisor to stop admitting new steps. Each driver finishes its current step and exits with reason `drained`. The drain is bounded (§2.11.1): a driver still running when the bound passes is terminated and its run requeued, exactly as in a crash. Then the supervisor re-executes itself from the new release and requeues the drained runs. Runs record which release made each commit.
- **Both hosts** install the same release with `ilk2 upgrade`. `ilk2 doctor --hosts` compares versions over ssh.

## 2.8 Consumer interface

- **The CLI with JSON output is the contract:** `ilk2 submit <batch.md|json> [--priority N] [--consumer-ref REF]`, `ilk2 status [--batch ID] --json`, `ilk2 runs --json`, `ilk2 events --since <cursor> --json`, `ilk2 park|resume|amend|ack`, `ilk2 cancel <batch> [--superseded-by <batch>]`, `ilk2 lease acquire|release` (§2.4.2), `ilk2 export --v1`.
- **Versioning.** Every JSON output carries `api_version`. A consumer may pass `--api-version N` to get an older shape. The CLI keeps every version that a registered consumer has pinned, and at least the two previous ones; `ilk2 doctor --hosts` lists each host's pinned versions. (rezmac runs a pinned gh-resolve tag, so one previous version was not enough.)
- **`status --json` schema,** per batch: status, priority, consumer reference, terminal reason (from a fixed enum), the failing gate with its new failures and fingerprints, `root_lease`, `accepted_head`, and per accepted step its commit SHA, gate results, attempt count and release version. The per-step record is the publication evidence a consumer can cite. It replaces V1's ship-proof provenance fields.
- **Events** carry a monotonic cursor and are kept for 30 days. A cursor older than that returns `cursor-expired`, and the consumer re-reads `status`. Every event carries the batch's consumer reference.
- **Cancel** is terminal: it stops any running attempt as a crash would (§2.10), recovers an `accepting` step, releases the root lease, and records the reason (`issue-closed`, `claim-released`, `stand-down`, `superseded`). `--superseded-by` links the replacement batch. This replaces V1 consumers parking and unparking other masters.
- **gh-resolve** submits one batch per issue, with that issue's worktree as the declared execution root (I3) and the issue number as consumer reference. The concurrency limit is per project, so several issues run in parallel. V1 reached that on rezmac only on Oct 12, through a per-host flag and moving `host-compat.json` aside.
- **Amend** is append-only and version-checked, and only changes steps that have not started (Q3). From P4 the driver also amends, to add an attributed repair plan (§2.5.7).
- **Base-measurement cost is counted.** A consumer suite of about 14.5 minutes is measured at the base once per batch, plus parent probes and, from P4, attribution probes. All appear in each batch's timings and in minutes per accepted step.

## 2.9 Observability

- **Every attempt records** wall time, time to first edit, model API time, thinking time (when the stream reports it), tool time, gate time, outcome and reason.
- **`ilk2 report`** prints five comparison metrics:
  - minutes per accepted step (all attempts);
  - share of attempts that produce nothing;
  - human transitions per batch;
  - **refused hours:** queued but not dispatched, by reason and clearing actor (I10);
  - **minutes to first edit** per attempt, reported next to thinking time, because the two overlap.

  The first three come from the V1 assessment. The last two come from the Oct 12 [evidence](evidence/2026-10-12-v1-stuck-and-slow.md).

## 2.10 Crash, restart and sleep behaviour

| Event | Behaviour |
|---|---|
| Supervisor restarts (crash, health-job kickstart, upgrade) | Reads runs in non-terminal state. For each driver still alive (PID plus start time match), it sends SIGTERM to the driver's process group, waits 30 s, then sends SIGKILL. Each run is marked `lost` and requeued from its last accepted step; a step left `accepting` is recovered first (§2.4.1). It never adopts (I4). The cost is the attempt in flight, up to one attempt cap. |
| Driver crashes mid-attempt | The supervisor sees the child exit. The attempt is recorded `interrupted`. The worktree is kept for diagnosis, and the next run starts from the last accepted commit. |
| Worker hangs | The driver kills the worker's process group at the attempt time cap. The outcome is `timeout`, and only then. |
| Process group does not exit after SIGKILL (for example, stuck in uninterruptible I/O) | After 60 s the run is `blocked (unkillable)`, with the PIDs, and a `needs_human` row. Its execution root is not reused until the PIDs are gone. |
| Host sleeps | All bounds are measured with `time.monotonic()`. On macOS that is `mach_absolute_time()` (checked on chad-mbp), which Apple documents as not advancing during sleep, so sleep does not use up a bound. When the gap between monotonic time and wall time grows by more than 60 s between two ticks, the supervisor records `host-slept`. |
| Host reboot | launchd starts the supervisor, and the same reconciliation runs. A run lost because `kern.boottime` changed counts as `lost (reboot)` and does not count toward the step's restart limit. |

## 2.11 Progress guarantee

**The promise:** a submitted batch either moves forward, or stops in a named state with an open `needs_human` row. No state waits without a bound, and the supervisor that enforces the bounds is itself watched from outside (I9-I11).

### 2.11.1 Bounds

| State or phase | Bound | When the bound passes | Who acts |
|---|---|---|---|
| Supervisor alive but not ticking | Last tick older than 3 min | The health job runs `launchctl kickstart -k` on the supervisor, which then restarts as in §2.10. Three kickstarts within an hour create a `needs_human` row | Health job |
| Supervisor start-up and reconciliation | 5 min to the first tick | Same as above | Health job |
| Driver spawned, waiting for its first phase event | 2 min | Kill the driver's group; run `stalled (spawn)`; requeue | Supervisor |
| Any run: no progress event at all | Attempt cap (Q7) + 15 min | Kill the driver's group; run `stalled (run)`; requeue. This outer bound also covers the gap between one phase ending and the next starting | Supervisor |
| Driver phase: prepare, scope check, accept | 10 min each | Kill the driver's group; run `stalled (<phase>)`; requeue from the last accepted step (a step left `accepting` is recovered first) | Supervisor |
| Dispatch (worker running) | Attempt cap (Q7) + 5 min | The driver kills the worker's group at the cap: outcome `timeout`. If the driver misses the grace period, the supervisor treats it as a stalled phase | Driver, then supervisor |
| Gate running | The gate's declared cap + 5 min | The driver kills the gate's group, which counts as a transient `unmeasured`. The gate is re-run once (§2.5). The supervisor is the backstop | Driver, then supervisor |
| Process-group termination | SIGTERM, 30 s, SIGKILL, 60 s | `blocked (unkillable)` with a `needs_human` row (§2.10) | Human |
| SQLite busy or locked | Busy timeout of 10 s per transaction; 3 retries | The actor's operation fails with `store-busy`. A driver treats it as a stalled phase (requeue); the CLI reports it and exits non-zero | Driver or supervisor |
| Setup hook (§2.5.3) | Declared cap, default 15 min | `blocked (setup)` with its output | Human, or the consumer (§2.11.3) |
| Attribution phase (§2.5.7, from P4) | 12 probes per failure (cached results do not count); 20 min wall time per phase; cache kept across requeues | `unattributable (budget)`; the current step is judged on all new failures | Driver |
| Consumer root lease (§2.4.2) | 30 min, renewable | Lease moves to `consumer-expired`, which still fences the driver (it does not release). `needs_human` row under the project's policy. Cleared by the consumer's release or `ilk2 lease recover` | Consumer, or human |
| Host under load (admission) | Checked every tick before starting a run | No new ilk2 run starts while the 5-minute load average exceeds the host's core count, free memory is under 2 GB, or 3 or more of ilk2's gate runs **in the last 30 minutes** timed out. The timeout signal decays with time, so it clears within 30 minutes of the last timeout without any new gate running. A `host-load` refusal row (I10), cleared by the supervisor | Supervisor |
| Cancel | Same as a crash: SIGTERM, 30 s, SIGKILL | `cancelled` with its reason; root lease released | Supervisor |
| Upgrade drain | Attempt cap + 15 min | Remaining drivers are terminated and requeued (§2.7) | Supervisor |
| Model API error (429, 5xx, overloaded) | Backoff of 1, 2, 4 … 30 min; 2 h per run | Not counted as an attempt. After 2 h the batch gets a `waiting (api)` refusal row (I10), cleared by the supervisor. It probes every 30 min and creates a `needs_human` row at 6 h | Supervisor, then human |
| Quota exhausted | Until the reported reset, or hourly probes if none is reported; 24 h at most | A `quota` refusal row, cleared by the supervisor. At 24 h it becomes `blocked (quota)` with a `needs_human` row | Supervisor, then human |
| Attempts on one step | 3 (Q6) | Step `blocked (attempts)`, with the red evidence of each attempt | Human |
| Restarts of one step (run lost, interrupted or stalled; reboot losses excluded) | 3 | Step `blocked (crash-loop)`, with all three reasons | Human |
| Plan whose dependency is blocked | Immediate | `blocked (dependency: <plan>)`. Independent plans keep running | Clears when the blocking plan clears |
| Batch with nothing runnable | Immediate | Batch `blocked`, listing each blocked plan and its reason | Human |
| Park, resume, amend | One store transaction each (SQLite busy row above) | Applied at the next step boundary. A running attempt is not interrupted except by `park --now`, which terminates it like a crash | Human, through the CLI |
| `needs_human` row unacknowledged | Re-notify after 4 h, then every 12 h | Re-send on every configured channel (§2.11.3) | Health job |
| Queued but refused (I10) | Re-evaluated every tick | Rows cleared by the supervisor re-check themselves. Rows cleared by a human create a `needs_human` row | Per row |
| Parked | None: explicit human state | Shown at the top of `ilk2 status` | Human |

**How a stall is detected:** the driver writes an event to the store at the start and end of every phase. Every 30 s the supervisor compares the age of the current phase, and of the run's latest event, with their bounds, and writes its own tick. The heartbeat is the phase event itself, not a timer thread. A timer thread keeps beating while the main loop is hung, so it would hide the stall; V1's retro `the-ledger-spawn-held-the-runner` is a runner held by a child process it had started in the background. The supervisor's own tick is checked by the health job, a separate launchd job, so a supervisor that is alive but hung is caught. KeepAlive alone only catches one that exits.

### 2.11.2 Checks at submission and activation

`ilk2 submit` rejects a batch before it is queued, with every reason listed, when:
- a step declares no gate (vacuous; V1 memory `step-heading-colon-makes-gates-vacuous`);
- a declared gate resolves to no adapter, or its executable is not on the pinned `PATH` in the execution root;
- a plan dependency names a missing plan or forms a cycle;
- the execution root is missing, or is not a git worktree of the project.

When the batch becomes active, the driver runs the base measurement in the execution root before any worker starts. If the base is permanently `unmeasured`, the batch is `blocked (base-unmeasured)` and no model time has been spent. (V1 retro `the-gate-cannot-pass-from-the-worktree`: a batch parked four times in four hours because its gate could not pass from where the loop ran it.) A gate whose tests a later step creates is measured as "0 tests" at the base, not as unmeasured (§2.5).

### 2.11.3 Telling a human, or the consumer

Emitting a notification does not prove anyone saw it, so the record of record is a durable **`needs_human` row**:
- It is created once per entry into a state that ilk2 cannot clear itself. It records a reason from a fixed enum (`attempts`, `crash-loop`, `dependency`, `setup`, `base-unmeasured`, `acceptance-mismatch`, `unkillable`, `quota`, `api`, `regression-attributed`, `consumer-lease-expired`), the command that clears it, and the batch's consumer reference.
- It stays open until someone runs `ilk2 ack <id>`, or the state clears (for example `ilk2 resume` or `ilk2 cancel`).
- A `needs_human` event appears in `ilk2 events --json` and at the top of `ilk2 status` in every case.
- A state meant for a human is never retried automatically.

What happens next depends on the project's **notification policy**:

| Policy | Who handles the row | Delivery | Re-sent |
|---|---|---|---|
| `owner` (default; projects Chad runs directly) | Chad | macOS notification on the host plus one channel off the host (Q12) | After 4 h, then every 12 h, by the health job |
| `consumer` (resolver projects) | The consumer, for example gh-resolve | The event only. No macOS notification and no off-host page | Never. If the row is still unacknowledged after 24 h, the health job sends one off-host notice that the consumer is not handling it |

**Why resolver projects never page Chad:** gh-resolve's ADR 0032, amended Sep 17, makes the merge the only human gate in the resolver pipeline. gh-resolve maps the event to its terminal `escalated` state (its `reap.py` releases the claim with `state="escalated"`), comments on the issue, writes its own ledger row, then calls `ilk2 ack`. Chad approved this policy on Oct 13. (V1 retro `a-designed-escalation-loops`: a stop that the code called a human escalation cycled every 35 minutes until someone parked it.)

### 2.11.4 The stall suite

Each case injects one fault into a throwaway batch. M1 requires all of them to pass ([document 4](04-v1-relation-and-migration.md) §4.4).

| # | Fault injected | Expected result | Source |
|---|---|---|---|
| S1 | Driver's main loop hangs in prepare | `stalled (prepare)`, requeued, the step then ships | retro `the-ledger-spawn-held-the-runner` |
| S2 | Worker hangs | `timeout` at the cap, killed by process group, named as a timeout and nothing else | retro `a-hard-stop-disguised-as-a-timeout` |
| S3 | Gate ignores its cap | Killed, transient `unmeasured`, re-run once | retro `a-verify-that-cannot-fit-its-own-gate` |
| S4 | Gate runner missing | `unmeasured` (permanent), blocked at once, 0 further attempts | retro `a-gate-that-could-not-pass-read-as-a-timeout` |
| S5 | API returns 429 and 5xx for 10 min | No attempt counted; the step continues afterwards | memory `exit-state-vocabulary-is-under-declared` (quota) |
| S6 | Driver killed at every start | `blocked (crash-loop)` after 3 restarts, one `needs_human` row | memory `relaunch-fixes-state-not-step-design` |
| S7 | One plan blocked, another depends on it, a third is independent | Dependent is `blocked (dependency)`, the independent plan ships, the batch ends `blocked`, one `needs_human` row | memory `blocked-dependent-is-not-runnable` |
| S8 | Red-first step whose test does not exist at the base | Passes with its declared expected reds; a later step turns them green and passes | retro `a-red-first-step-gated-on-green`; review finding 5 |
| S9 | Step with no gate; gate executable missing | Rejected at submit, no run created | memory `step-heading-colon-makes-gates-vacuous` |
| S10 | Base cannot be measured in the execution root | `blocked (base-unmeasured)` before any dispatch | retro `the-gate-cannot-pass-from-the-worktree` |
| S11 | Supervisor killed mid-batch | Surviving driver terminated, run requeued, the batch ships; no adoption | memory `runner-launched-pid-is-a-wrapper`; review finding 4 |
| S12 | A state cleared by a human, left unacknowledged | Exactly one `needs_human` row; re-sent at 4 h (time compressed in the test); 0 automatic retries | retro `a-designed-escalation-loops`; review finding 6 |
| S13 | Supervisor alive but its loop blocked | The health job kickstarts it within the bound; the batch ships | review finding 1 |
| S14 | Crash after intent, after commit, and before finalize (3 cases) | Each recovers per §2.4.1: undo, finalize, finalize. No duplicate commit, no accepted step without a commit | review finding 2 |
| S15 | Base-red test whose failure changes during a step | Counted as new; the step fails | review finding 5 |
| S16 | Host sleeps mid-attempt for longer than the attempt cap | No `timeout` and no `stalled`; `host-slept` recorded; the attempt continues | review finding 3 |
| S17 | Process group ignores SIGTERM | SIGKILL at 30 s; a simulated unkillable PID gives `blocked (unkillable)` | review finding 3 |
| S18 | Store held locked by another connection | `store-busy` after the busy timeout and retries; the run is requeued, not hung | review finding 3 |
| S19 | Step 1 breaks a test in a gate only step 3 declares (M1 behaviour) | Step 3 is judged on the failure, which is marked `possibly-inherited` with the parent-probe evidence in its brief; no attribution yet | gh-resolve review H2 (F8); Codex review 2, finding 1 |
| S20 | Gate written as `bun run <script>` that calls `vitest -c <config>`; then a step edits that config | Resolved to the vitest adapter with test IDs; the config edit changes the gate version and triggers a new base measurement. A `shell` test gate is rejected at submit | gh-resolve review H3 |
| S21 | One vitest test exceeds its own 15 s timeout under load | Timeout-class failure, re-run alone, passes; no attempt spent | gh-resolve review H6 (F6) |
| S22 | Lease races: (a) a consumer acquire and a `resume` at the same generation; (b) a consumer push after the remote head moved; (c) a legitimate consumer operation that crosses the 30 min limit without renewing; (d) one that renews | (a) exactly one succeeds; (b) the push is refused on the stale expected head; (c) the lease moves to `consumer-expired`, `resume` still fails, and the consumer's late push and release succeed with no driver commit in between; (d) no expiry | gh-resolve review H4; Codex reviews 2-3 |
| S23 | `ilk2 cancel` mid-attempt, and while a step is `accepting` | Attempt stopped, acceptance recovered, batch `cancelled`, lease released | gh-resolve review H5 |
| S24 | Execution root without `node_modules`, setup hook declared | Setup installs, the base measures; with a failing setup the batch is `blocked (setup)`, never `base-unmeasured` | gh-resolve review H5 |
| S25 | A test reads `$HOME/.ilk-data` or `ILK_SKILL_HOME` | It sees an empty private `HOME` and no `ILK_*` variables | gh-resolve review H6 (F7) |
| S26 | Resolver project (policy `consumer`) blocked | `needs_human` event with the consumer reference; no macOS or off-host page; after 24 h unacknowledged, one off-host notice | gh-resolve review M2; ADR 0032 |
| S27 | Host load above threshold with a run queued; then 3 gate timeouts in 30 min followed by recovered load | No run starts; the `host-load` refusal clears by itself, at the latest 30 min after the last timeout, with no gate having run; then the run starts | Codex reviews 2-3 |

**Attribution suite (required before P4, §2.5.7; diagnostic worktrees themselves are tested in M1 by S19):**

| # | Fault injected | Expected result |
|---|---|---|
| A1 | Step 1 of plan P breaks a test that only step 3 declares; P has already shipped | Edge found at step 1; step 3 is `pass-with-attributed`; a repair plan with `repair_of_step_id` is added; P's dependents pause; P stays `shipped`; the batch ships only after the repair |
| A2 | The gate's specification changed between the edge commits | `unattributable (spec-changed)`; step judged on all new failures; no overlay of new config onto old source |
| A3 | The current attempt has uncommitted changes when attribution runs | Probes run in diagnostic worktrees; the execution root and its changes are untouched |
| A4 | The failure is flaky at the boundary | Disagreeing re-probes give `unattributable (flaky)`, not a blamed step |
| A5 | A dependency version changed between the edge commits | Each diagnostic worktree installs from its own commit's lockfile; the result reflects that commit's dependencies |
| A6 | Probe budget exhausted (long batch, slow fixtures), then the run is requeued | `unattributable (budget)`; after requeue, cached probes are reused, not repeated, and not counted again |
| A7 | Base clean, step 1 red, step 2 red | Attributed to step 1 (the child of the last clean commit, found walking backwards), not to the commit where the walk stopped |
| A8 | Two different assertion failures at the same line ("expected 1, got 2" at base, "expected 3, got 4" after a step) | Different fingerprints; the second counts as new |
| A9 | History clean, failing (step 1), fixed (step 2), failing again (step 4) | Attributed to step 4, the most recent clean-to-failing edge, not step 1 |
| A10 | Two attributed failures with the same test ID, fingerprint and edge; and an independent plan queued | One repair plan, not two; the independent plan starts no new step until the repair ships |

### 2.11.5 Judgment calls in this section

Each needs data from P2 and is revisited at M1.
- **Phase bound of 10 min** for prepare, scope check and accept. Basis: these do git and store work only. Wrong if large repos make a normal prepare take longer; then derive the bound per project from measured phase times.
- **5 min grace** after the attempt and gate caps, and **attempt cap + 15 min** as the outer run bound and drain bound. Basis: process-group kill and cleanup take seconds; the outer bound only has to exceed the longest phase. Wrong if `stalled` fires on runs that were about to finish.
- **30 s supervisor tick; health job every 60 s; supervisor stale after 3 min.** Basis: cheap, and fine-grained compared with any bound; 3 min is 6 missed ticks. Wrong if store contention shows up in the timings, or if kickstarts happen on a healthy supervisor.
- **Kill escalation: SIGTERM, 30 s, SIGKILL, 60 s.** Basis: drivers and workers need seconds to flush. Wrong if `unkillable` fires on processes that exit shortly after.
- **SQLite busy timeout 10 s, 3 retries.** Basis: transactions do no I/O beyond SQLite, so they last milliseconds. Wrong if `store-busy` appears under normal load.
- **API backoff of 1-30 min with a 2 h budget per run; `needs_human` at 6 h; quota at most 24 h.** Basis: V1 outages were either minutes long or quota resets. Wrong if MiMo outages often last 2-6 h; then notify sooner.
- **3 restarts per step, reboot losses excluded.** Basis: matches the attempt limit (Q6). One crash is noise, three is a defect. Wrong if other host events still cause legitimate repeated losses.
- **One re-run for a transient `unmeasured`.** Basis: a second identical failure is not transient. Wrong if load-induced gate timeouts need a quieter re-run slot, in which case wait for the host's run count to drop before re-running.
- **Re-notify at 4 h, then every 12 h.** Basis: one working session, then twice a day. Wrong if Chad finds it noisy or too slow.
- **Events kept 30 days; the CLI keeps every pinned `api_version` and at least two previous.** Basis: consumers poll at least daily; rezmac can lag a release. Wrong if a consumer is offline longer, or if keeping old shapes costs real code.
- **Setup hook default cap of 15 min.** Basis: a cold `bun install` takes minutes. Wrong if first installs on rezmac exceed it.
- **Consumer-policy fallback: one off-host notice after 24 h unacknowledged.** Basis: a consumer that stops handling rows should not leave a batch silent forever, but resolver runs must not page by default. Wrong if gh-resolve's own escalation already covers a dead consumer.
- **Attribution budget: 12 probes per failure, 20 min per phase.** Basis: walking backwards costs one probe per accepted commit until the clean one. In typical batches the damaging step is a few steps back; with 4 confirmation probes that leaves about 8 for the walk. Wrong if real damage is usually further back than that; then raise the probe count, since the wall-time limit still bounds the cost.
- **Consumer lease 30 min, renewable; expiry fences instead of releasing.** Basis: reap and land push within minutes, and a stuck consumer must never let the driver write under it. Wrong if consumers often die holding a lease, which would leave batches waiting on `lease recover`.
- **Host-load admission: 5-minute load above core count, under 2 GB free, or 3 or more ilk2 gate timeouts in the last 30 minutes.** Basis: gate timeouts under load were the measured symptom (gh-resolve review F6); a time window cannot latch. Wrong if it refuses runs on a host whose gates are not slowing.
- **Kill and requeue on supervisor restart instead of adopting** (Chad, Oct 13: "go ahead"). Basis: I4 holds with no lease or IPC code; supervisor restarts should be rare. Cost: the attempt in flight. Wrong if supervisor restarts become frequent (more than one a day); then add a driver lease and a handshake.

## 2.12 Code layout and size

| Module | Purpose | Seed from V1 (lines at `d17ffdcd`) |
|---|---|---|
| `ilk2/store.py` | Schema, migrations, transitions (CAS), events, `needs_human` | `plan_state.py` (492) |
| `ilk2/supervisor.py` | Queue, admission, process ownership, reconcile, upgrade, stall checks | `run_registry.py` (503) |
| `ilk2/health.py` | Supervisor health, re-notification | none |
| `ilk2/driver.py` | Step lifecycle, retries, acceptance protocol | `driver_step.py` (624), `iteration_context.py` (392) |
| `ilk2/worker.py` | Brief, config dir, sandbox, dispatch adapter, stream timings | `run_inputs.py` (480), `worker_dispatch.py` (199) |
| `ilk2/gates/` | Adapters (pytest, vitest, shell), specification versions, base measurement, fingerprints | `run_local_checks.py` (2,744; parts only) |
| `ilk2/plans.py` | Import V1 MASTER/sub-plan Markdown and ilk2 JSON, snapshot, export to V1 | `dispatch_target.py` (139) |
| `ilk2/notify.py` | Channels: macOS, events, one off-host channel | none |
| `ilk2/cli.py` | Commands and JSON output | none |
| `ilk2/release.py` | Build, install, verify, doctor | none |

**Size is a diagnostic, not a budget yet.** The first draft budgeted about 4.7k lines. The eight seed modules alone total 5,573 lines at `d17ffdcd`, before the acceptance protocol, health job, notification channels, fingerprinting, packaging and fault injection. Cutting seeds down may be possible, but 4.7k was an aspiration, not an estimate. A size estimate is made after P1 from the spike's real code, and then tracked per module in CI. Growth past that estimate is a signal to review coupling, not a kill condition by itself ([document 4](04-v1-relation-and-migration.md) §4.6).
