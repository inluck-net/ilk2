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

## Q17 evidence table (docs/06 P2-1)

Denominator for P1: 5 planned steps (1-5). Steps whose original plan line stayed valid are counted as they finish.

| Step | Plan line still valid? | Evidence | Knowable when planned? | Change | Goal/scope/verification changed? | Cost without amendment | Human amend time | Gate outcome | What a planning pass would have proposed |
|---|---|---|---|---|---|---|---|---|---|
| 1 | yes | — | — | — | — | — | — | pass (step1-20261013-023018) | — |
| 3 (from 1) | the line is valid but incomplete | finding 1: Bash-tool commands run in their own process group; SIGKILL of the worker group orphans them (probe sigkill-20261013-*) | partly: documented Claude Code behaviour could have been read beforehand; not checked | step 3 also checks that the sandbox covers processes spawned after the cancel snapshot | verification strengthened only | an orphaned process per hard kill, unnoticed until something leaked | ~0 (the builder session added it) | pending | add the check to step 3, as done here |
