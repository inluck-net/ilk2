# Retrospective: an RSI goal stopped by planning timeouts (Oct 19)

**What Chad saw:** the SwiftBar panel's "needs you (1)": `rsi-goal: goal gh-resolve-step-minutes is stopped: rounds ['gr-d7b1ae…`. Its clear line read `edit docs/rsi/goals.json (or bump the goal's reset)`, and "copy the clear command" copied that sentence.

**Sources:** `rsi_goal_rounds` and the `rsi.*` events in `~/.ilk2/data/state.db`, read Oct 19, 19:40-23:40 CST, and the planner logs under `~/.ilk2/data/rsi/goal-gh-resolve-step-minutes-*/attempt/logs/`.

## What happened

The goal (B2, `891877c`) asks for gh-resolve's minutes per accepted step to be at most 24 over 7 days. It measured 29.72 (192 samples) at its first round.

| Round | Planning | Tool calls | Ended |
|---|---|---|---|
| `gr-d7b1ae-1792384644437` | 12:37-13:07 | 46 | `plan-failed`: no `.rsi-spec.json` (session timeout) |
| `gr-d7b1ae-1792397307601` | 16:08-16:42 | 43 | the same |
| `gr-d7b1ae-1792406610028` | 18:43-19:13 | 61 | the same |

After 3 `retry` rounds the goal is `stopped`, as docs/08 §8.4 specifies. Two more RSI planning sessions the same day also hit the cap: `improvement:e90b6ca6cdba` (16:43-17:13) and `needs-human:621adae77a05` (19:13-19:43). That makes **5 sessions, 150 minutes, and 0 plans**. The last of them was RSI planning against the goal's own `rsi-goal` row.

## Why the rounds timed out

None of the 3 sessions was stuck, and none ran a suite:
- **They were close to done.** At about 29 min, round 1 wrote "I have the design's own answer now. Let me pin the exact line references", and round 3 wrote "I have enough to decide… then write the spec."
- **They started cold.** Each round spent about 25 min reading the same docs and code (`docs/08`, `docs/09`, `report.py`, `goals.py`, `driver.py`). Rounds 1 and 3 reached the same lever, the preload question in docs/02 §2.6 (Q10). Nothing carried a round's findings to the next one.
- **They had no time budget.** The brief (`src/ilk2/rsi.py:320-355` before the fix) never mentioned the cap. In round 3, the 9 gaps longer than 60 s between tool calls add up to 987 s of the 1797 s.
- **The goal is broad.** "Make gh-resolve's steps faster" leaves the whole engine in scope.

## What made it worse

1. **The `rsi-goal` row became a defect candidate.** `NOT_DEFECTS_NH` held `attempts` and `rsi-review`, but not B2's new `rsi-goal`. At 19:13 RSI spent a full planning session on its own alert, which only a `goals.json` edit can clear. Fixed in `d33c201`.
2. **The alert gave the wrong cause.** A retry stop used the miss stop's text, "rounds … did not move it: change the target or reset". No round had planned anything, and a reset alone would repeat 3 × 30 min.
3. **The clear "command" was prose,** so the panel's copy action copied a sentence.

## Fixes

- `d33c201`:
  - `rsi-goal` rows are not defects;
  - TMPPREFIX for sandboxed zsh heredocs;
  - plan-issue's drafts dir is open to the worker hook. The same day's `plan-issue` probe lost about 400 s to the hook; the RSI rounds were not affected, since `.rsi-spec.json` is written inside the worktree.
- This change:
  - **The stop names its cause.** A retry stop reads "its last 3 rounds failed to plan (last: …): fix planning, then reset the goal".
  - **`ilk2 rsi reset-goal <id>` is the clear command.** It bumps `reset` and commits `docs/rsi/goals.json` on a clean `main`, and refuses otherwise.
  - **A planner that writes no spec leaves its last notes** in the round's detail (`rsi.last_notes`). The goal's evidence carries the last 3 closed rounds' details to the next planner.
  - **The brief states the time budget:** the cap, and the spec by two thirds of it. It asks the planner to build on earlier rounds' notes and to keep scratch files under `$TMPDIR`.

## Not changed, and why

- **The 30-min cap.** Chad's rule, relayed by gh-resolve-65 (Oct 7): "never raise a timeout; fix a wait by removing work". The fixes above remove the cold start.
- **`STOP_RETRIES = 3`.** The stop worked: it ended a loop of 30-min failures. Its falsifier (docs/08 §8.4) is a restart whose first round plans. The restart after these fixes tests it.

## Checks

- **Does the next round plan?** After the release and the reset, the first round should write a spec within the cap. If it times out again, the cold start was not the main cost; then the goal itself is too broad for one step, and Chad narrows it.
- **Are notes carried?** Read the next round's planner prompt (`stub` in tests; the `start.jsonl` init in production). Its evidence should have `earlier_rounds`. The three old rounds have no notes: they closed before this change.
