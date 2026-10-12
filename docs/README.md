# ilk2 design documents

Draft for Chad's review, Oct 12, 2026. Author: Claude Code session ilk-skills-aa.

| # | Document | Question it answers |
|---|---|---|
| 1 | [Why V2](01-why-v2.md) | Why build a new core instead of patching V1? |
| 2 | [Architecture](02-architecture.md) | What are the parts, and how does a batch run end to end? |
| 3 | [V1 vs V2](03-v1-vs-v2.md) | What changes, what improves, and what V2 will *not* fix |
| 4 | [Relation to V1 and migration](04-v1-relation-and-migration.md) | How V2 sits beside V1, how a project moves over, and when we stop |
| 5 | [Open questions](05-open-questions.md) | What still needs a decision before code |
| E1 | [Evidence: where V1's time goes](evidence/2026-10-12-v1-stuck-and-slow.md) | How many hours are lost to refusals (stuck) and to worker time (slow)? |

**Revision, Oct 12 (later the same day):** two changes from Chad's review question on stuck loops and slowness:
- invariant I10, refusal accounting;
- worker time moved into scope ([document 3](03-v1-vs-v2.md) §3.4, Q10).

Evidence in E1.

**Revision, Oct 13:** a progress guarantee, from Chad's question on how a batch is kept from getting stuck:
- invariant I11 (every non-terminal state is bounded);
- [document 2](02-architecture.md) §2.11: bounds table, checks at submission, notification, and the stall suite required for M1;
- Q11-Q12.

**Revision, Oct 13 (Codex review):** all 11 findings accepted after checking. The two V1 facts were checked against `d17ffdcd`: the code-only default at `run_ilk_loop_claude.sh:5895-5899`, and the 5,573-line seed total. Changes:
- I4 no adoption; I6 fingerprints and gate specification versions; I11 covers the supervisor; new I12 with the acceptance protocol (§2.4.1);
- a health job, more bounds, sleep handling, and a durable `needs_human` row with acknowledgement;
- stall suite S1-S18;
- rollback only between batches or via export;
- size as a diagnostic, with checkpoints replacing the line and hour kill criteria;
- the Q10 comparison counterbalanced over several steps;
- V1 status corrected in [document 1](01-why-v2.md), including new §1.4 (V1 minus its legacy paths).

**Revision, Oct 13 (gh-resolve review):** reviewed by gh-resolve-e1 as the main consumer. All findings accepted after checking: ADR 0032's amendment and gh-resolve's `push.py` and `reap.py` were read, and rezmac's per-run switch was confirmed. Changes:
- per-project notification policy: resolver projects never page;
- the step that introduced a failure is found and gets the repair;
- package scripts resolved to their runner; `shell` not allowed as a test gate;
- a root lease plus `accepted_head` for consumer pushes;
- `cancel`, priorities and a setup hook;
- a defined pinned environment, timeout-class failures and pytest edge cases;
- no full suites inside the worker;
- a host budget of 2 for ilk2;
- shadow mode for P5 and a vitest proof before it;
- a §4.8 list of gh-resolve's blockers;
- stall suite S1-S26.

Decisions Q14-Q16 were recorded as accepted by Chad.

## Decisions already made (Chad, Oct 12, 2026)

- **Mac only.** chad-mbp and rezmac; no Windows or PowerShell.
- **Worker model: keep MiMo** (`mimo-v2.6-pro`) through Claude Code.
- **Built by hand.** A Claude Code session writes it and Chad reviews; the ilk loop never edits this repo.
- **Separate repo** (this one), separate data home `~/.ilk2/`, separate command namespace (`/ilk2-…`, `ilk2 …`).
- **Python 3.12, managed by uv** and bundled into each release; SQLite state; CI on a macOS GitHub runner.
- **V1's D2 code-only trial finishes first.** Its result is evidence for this design, not a prerequisite for writing it.

## Ground rules for this design

- **Every V1 pain point cited here has a source:** a V1 retro, a Claude memory note, a backlog row or a measured log. V1 references are to `inluck-net/ilk-skills` at `d17ffdcd` (Oct 12, 2026) unless another commit is named.
- **Size is watched, not fixed in advance:** the core stays small, but its size is estimated after P1 from real code (the first draft's 4.7k budget did not survive review). Growth past the estimate triggers a review of coupling ([document 4](04-v1-relation-and-migration.md) §4.6).
- **Speed claims follow the V1 measurement method.** Measured means all attempts counted, per accepted step. Nothing here claims a speedup that has not been measured.
