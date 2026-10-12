# P1 findings, raw (input to `docs/spikes/p1.md` at step 5)

## Step 1: CLI dispatch adapter (Oct 13, 2026, chad-mbp, Claude Code 2.1.296)

Code: `cli_adapter.py`, `run_step1.py`, `probe_sigkill.py`. Run outputs (stream logs, summary.json) under `~/.ilk2/spike-p1/`.

**Result: pass.** Final run `step1-20261013-023018`, on the final code:

| Attempt | Session id | init model | Turns | Duration | First assistant event | First edit | Gate after |
|---|---|---|---|---|---|---|---|
| start | e60f4ed2… | mimo-v2.6-pro | 5 | 37.4 s | 15.9 s | 23.4 s | red, 2 of 5 cases |
| resume with gate output | e60f4ed2… (same) | mimo-v2.6-pro | 3 | 103.4 s | 78.9 s | 81.3 s | green, 5 of 5 |
| cancel (Bash `sleep 600` in flight) | e0014ad4… (new session) | mimo-v2.6-pro | — | killed in 1.1 s | — | — | 0 survivors; sleep gone |

Earlier runs of the same script: `step1-20261013-022428` passed on the pre-fix cancel; the first run crashed in cancel (finding 2).

### Findings

1. **The Bash tool runs commands in a new process group.** `zsh` and its `sleep` had pgid ≠ the worker's pgid in all 3 cancel runs. `killpg` on the worker group alone works only because Claude Code cleans up its children on SIGTERM. Under SIGKILL of the worker group, both survived (probe `sigkill-20261013-*`, first run), reparented to launchd. **Adapter consequence:** `cancel` snapshots the descendant process groups before signalling and signals all of them. With `term_first=False` the probe then passes (groups `[59143, 59792]`, 0 survivors). A descendant that starts after the snapshot is not covered. Step 3's sandbox, or a final sweep by the supervisor, has to catch those. This matters for I4 (kill and requeue) and for a worker that crashes.
2. **On macOS, `killpg` on a group whose only member is a zombie returns `EPERM`**, not `ESRCH`. Reproduced with `true` in a new session: `PermissionError` before `wait()`, then `ProcessLookupError` after it. The cancel path must reap the leader and must treat zombies as gone.
3. **Resume keeps the session id**, without `--fork-session`. The init event on resume reports the same id and the same model.
4. **Resume is slow to its first event:** 68-106 s to the first assistant event in 3 runs, against 16-31 s for a start. Thinking and re-processing the session's context are the likely causes; this hasn't been measured. Relevant to Q6 (resume vs a fresh session) and Q10.
5. **The init event arrives about 0.2 s after spawn** and gives the model from config, before any API call (consistent with memory `probe-model-by-init-event-not-self-report`).
6. `duration_api_ms` can exceed `duration_ms` on resume (222.8 s vs 166.2 s in run 2). This was seen, not explained. Use wall time from the driver, not the CLI's API time.

### Choices made in the spike (judgment calls, not findings)

- `--permission-mode bypassPermissions` plus `--strict-mcp-config`: the worktree is a throwaway, and the sandbox arrives in step 3. Wrong if step 3 shows that bypass mode skips hooks.
- The prompt goes over stdin, so it never appears in process arguments.
- Inherited `ANTHROPIC_*`, `CLAUDE_CODE_*`, `CLAUDECODE` and `CLAUDE_CONFIG_DIR` are removed from the worker's environment, so only the template's provider settings apply.
- 5 s grace between SIGTERM and SIGKILL. Measured cleanup takes 1.1 s; wrong if a real worker needs longer to flush.

## Step 2: Agent SDK adapter (claude-agent-sdk 0.2.165)

Code: `sdk_adapter.py`, `sdk_launch.sh` + `sdk_launch.py`, `run_step2.py`. **Result: pass, all 6 adoption criteria** (docs/02 §2.6), run `step2-20261013-031059`, every attempt under the step 3 sandbox and file hook:

| Criterion | Evidence |
|---|---|
| MiMo compatibility | init model `mimo-v2.6-pro`, `model_usage` = [mimo-v2.6-pro] |
| Session identity on resume | same session id; gate red, then green |
| Hook parity | the settings-file hook and an SDK callback hook both fired on `git commit`; HEAD unchanged |
| Cancel by process group | worker pgid ≠ driver pgid; groups [worker, Bash tool]; 0 survivors; driver alive |
| Streaming timing metrics | init, first assistant event, first tool use, first edit, result all timestamped |
| Same sandbox behaviour | Bash write outside the worktree refused; file absent |

**But only with a wrapper**, so the SDK is no simpler than the CLI here:
1. **The SDK spawns the CLI in the driver's own process group** (`anyio.open_process` without `start_new_session`, `claude_agent_sdk/_internal/transport/subprocess_cli.py:873-881`). Without a wrapper, killpg on the worker would kill the driver. `sdk_launch.py` calls `setsid` and writes its pid (= pgid) to a file.
2. **The SDK passes the whole inherited environment** (`:819-825`); `options.env` can add keys but not remove them. The wrapper scrubs `ANTHROPIC_*`.
3. **It bundles its own CLI**: Claude Code 2.1.294 in a 212 MB wheel, against 2.1.296 installed. ilk2 would pin two CLI versions, or pass `cli_path`.
4. **It makes an extra model call per session** to generate a title (`query_source: generate_session_title` on stderr), which the CLI path does not.
5. **Settings are not loaded by default**: `setting_sources=["user"]` is needed for the private config dir's provider env and file hooks.

**Recommendation for Q1 (judgment call): keep the CLI as the dispatch implementation; do not adopt the SDK for M1.** It passes the criteria, but only behind the same process wrapper the CLI needs anyway. It adds a second CLI version, an extra call per session, and a 212 MB dependency, and its gain (typed messages, in-process hook callbacks) is small next to file hooks that already work. Wrong if M1 needs in-process hooks or control requests (interrupt, permission prompts) that the CLI cannot give.

## Step 3: sandbox-exec profile plus hook

Code: `Isolation` in `cli_adapter.py`, `worker_hook.py`, `run_step3.py`. **Result: pass**, run `step3-20261013-031214`:

| Layers | Write tool outside | Bash write outside | git commit | git push |
|---|---|---|---|---|
| sandbox + hook | refused (hook) | refused (sandbox) | refused (hook) | refused (hook) |
| sandbox only | refused (EPERM) | refused | refused (exit 128) | refused (unpack error) |
| hook only | refused | **written** | refused | refused |

The sandbox alone is sufficient. The hook alone is not: it cannot see writes made through Bash. Keep the hook for clear messages to the worker, and rely on the sandbox.

Orphan sweep: a double-forked `(sleep 600 &)` survived a SIGKILL cancel, because it was in no descendant group by then. A cwd sweep (`lsof -d cwd`, matching cwd inside the worktree, never argv) caught and killed it. Finding 1's gap is closed by sweep-after-cancel.

### Findings (steps 2-3)

7. **A crashing hook fails open.** Claude Code treats any exit code other than 2 as a non-blocking hook error. `worker_hook.py` crashed under `/usr/bin/python3` 3.9 (`str | None`), and the `git commit` went through to the next layer (step2-20261013-025927). Hooks must turn every internal error into exit 2, and must be tested on the interpreter that runs them.
8. **The sandbox profile must allow Claude Code's own scratch:** `/private/tmp/claude-<uid>/<cwd key>` (per-cwd tool directory, created whatever `TMPDIR` says) and `/private/tmp/claude-<hex>-cwd` (a marker the Bash tool writes per call). Without them, every Bash call fails with EPERM, and "refused" means nothing.
9. **Paths in a profile are matched after symlink resolution** (`/tmp` is `/private/tmp`).
10. **Under sandbox-exec, setuid binaries cannot run** (`ps` fails with EPERM). A worker that runs a test using `ps` inside the sandbox sees a failure the driver's gate, outside the sandbox, does not. The brief, or the targeted-test hook, has to say so. V1's own tests hit this (step 4 preflight).
11. **The writable log directory must not contain anything the fence protects.** My first step 2 and 3 runs put the probe targets under the allowed log directory and "passed" vacuously (step3-20261013-030422). The model-free profile check in the commit message is the guard: inside 0, outside 1, commit 128, cwd marker 0, other /tmp 1.
12. **`uv run` leaks the spike's virtualenv into children** (`PATH`, `VIRTUAL_ENV`), so a worker's or gate's `python3` became ilk2's 3.12 instead of the host's. `host_env()` strips it. The core's driver must set the worker's and the gate's interpreter explicitly.

## Step 4: brief comparison (Q10)

**Result: done, 8 of 8 runs accepted.** Runs `~/.ilk2/spike-p1/step4/runs/20261013-033236`, analysis `~/.ilk2/spike-p1/step4/analysis.json`, verdict and table in `docs/spikes/p1.md`. P was faster on 4 of 4 steps (26.99 → 20.59 min per accepted step).

Findings from preparing it:
13. Gates cannot run under `sandbox-exec`: V1's tests exec setuid `ps`. They run in the pinned environment instead (HOME and ILK_DATA_* pinned; 0 ilk2-keyed projects appeared in `~/.ilk-data`).
14. V1 accepted `d996894e` with a pinned test still red (`launch.sh` wrote Python `true`); V1 fixed it in the next commit, `3d1823e7`. Evidence for the design's driver-owned gate.
15. `13919eff` could not be judged: a pinned test fails at its own commit in a clean environment.
16. The adapter's thinking measure undercounts: MiMo reports thinking through `thinking_tokens` system events, not thinking blocks. `step4_analyze.py` measures from those events.

## Q17 evidence table (docs/06 P2-1)

Denominator for P1: 5 planned steps. Steps 1, 2 and 5 kept their plan line; step 3 gained a check from step 1; step 4 swapped one V1 step after preflight (`13919eff` → `d996894e`), which a planning pass could not have foreseen without running the gate.

| Step | Plan line still valid? | Evidence | Knowable when planned? | Change | Goal/scope/verification changed? | Cost without amendment | Human amend time | Gate outcome | What a planning pass would have proposed |
|---|---|---|---|---|---|---|---|---|---|
| 1 | yes | — | — | — | — | — | — | pass (step1-20261013-023018) | — |
| 2 | yes, with a wrapper the line did not foresee | SDK spawns in the driver's process group (finding, step 2) | yes, by reading the SDK source first | added `sdk_launch` wrapper | no | — | 0 (builder) | pass (step2-20261013-031059) | read the transport before writing the adapter |
| 3 (from 1) | the line is valid but incomplete; pass (step3-20261013-031214) | finding 1: Bash-tool commands run in their own process group; SIGKILL of the worker group orphans them (probe sigkill-20261013-*) | partly: documented Claude Code behaviour could have been read beforehand; not checked | step 3 also checks that the sandbox covers processes spawned after the cancel snapshot | verification strengthened only | an orphaned process per hard kill, unnoticed until something leaked | ~0 (the builder session added it) | pending | add the check to step 3, as done here |
