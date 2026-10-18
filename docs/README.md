# ilk2 design documents

**Status, Oct 14, 2026:** ilk2 **0.2.0** runs on both hosts. It owns gh-triage, gh-resolve's and ilk-skills' development, and kira-cloudflare's shadow and issue-sweep batches; V1's scheduler is off on chad-mbp. rezmac's resolver is moving next: P5/P6 in [document 4](04-v1-relation-and-migration.md) §4.4, "As it happened". Diagrams of the system as built: admission §2.2.1, step lifecycle §2.4, lanes §2.4.2a, upgrades §2.7 and a consumer's batch §2.8 in [document 2](02-architecture.md); a project's owner over time in [document 4](04-v1-relation-and-migration.md) §4.3.

The design closed for P1 on Oct 13, 2026 (tag `design-accepted-p1`), after five review rounds (Codex ×3, gh-resolve ×2). Findings since then are in [document 6](06-open-items.md), by phase.

First draft Oct 12, 2026 by Claude Code session ilk-skills-aa; revised Oct 13 by ilk-skills-4a.

| # | Document | Question it answers |
|---|---|---|
| 1 | [Why V2](01-why-v2.md) | Why build a new core instead of patching V1? |
| 2 | [Architecture](02-architecture.md) | What are the parts, and how does a batch run end to end? |
| 3 | [V1 vs V2](03-v1-vs-v2.md) | What changes, what improves, and what V2 will *not* fix |
| 4 | [Relation to V1 and migration](04-v1-relation-and-migration.md) | How V2 sits beside V1, how a project moves over, and when we stop |
| 5 | [Open questions](05-open-questions.md) | What still needs a decision before code |
| 6 | [Open items after design close](06-open-items.md) | What is still open, and at which phase it gets resolved |
| 7 | [Retiring V1 completely](07-retiring-v1.md) (accepted, Oct 17) | How V1's planner, feedback and run skills become ilk2-native or go, and when V1's data is archived |
| E1 | [Evidence: where V1's time goes](evidence/2026-10-12-v1-stuck-and-slow.md) | How many hours are lost to refusals (stuck) and to worker time (slow)? |
| E2 | [Evidence: what the full suite costs](evidence/2026-10-13-full-suite-cost.md) | Where do full-suite runs cost time in V1's repos, and would splitting into component repos help? |
| E3 | [Evidence: the gh-resolve P5 batch baseline](evidence/2026-10-14-gh-resolve-p5-batch-baseline.md) | How long does each step of a real 15-step batch take on ilk2, and what did the night's fixes change? |
| E4 | [Evidence: V2 against V1 speed](evidence/2026-10-16-v2-vs-v1-speed.md) | Minutes per step on the projects with a V1 baseline |
| E5 | [Evidence: where batch 17d's time went](evidence/2026-10-17-batch-17d-where-the-time-went.md) | Why a 7-step gh-resolve batch took about 6 h, and what ilk2 controls in it |
| E6 | [Evidence: batch 18a's retrospective](evidence/2026-10-18-batch-18a-retrospective.md) | Why a 20-step batch took 13 h, and the ilk2 levers ranked by evidence |

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

**Revision, Oct 13 (Codex review 2):** 8 findings accepted:
- repairs become batch-level repair plans (`repair_of_step_id`), with an explicit `pass-with-attributed` rule;
- probes run in diagnostic worktrees keyed by commit and gate version, and are `unattributable` if the gate is not portable across the boundary;
- the root lease is a compare-and-set with a generation and expected head, and has a time limit;
- shadow mode uses a separate non-owning clone;
- attribution is an edge search with confirmation, a budget and a cache;
- fingerprints include a message digest and the top three project frames;
- host budgets are an experiment guarded by load admission.

Chad deferred attribution to before P4 (§2.5.7). M1 judges new failures against the current step and marks `possibly-inherited` ones. The stall suite is now S1-S27.

**Revision, Oct 13 (Codex review 3):** 8 remaining findings fixed:
- the host-load timeout signal decays over a 30-minute window, so it can't latch;
- lease expiry moves to `consumer-expired`, which still fences the driver, and leases are renewable;
- attribution walks backwards to the most recent edge, never binary search (A9);
- a changed gate specification has no comparable base, so its failures are charged to the current step;
- the probe budget is 12, with cached probes not counted;
- diagnostic worktrees are part of M1, and only attribution is deferred;
- fingerprints keep assertion operands;
- repair plans are deduplicated batch barriers (A10).

**Revision, Oct 13 (gh-resolve re-review, design close):** fixes for M1: setup re-run on transient failure; named `ILK_*` variables in the allow-list; base comparison for non-zero exit with 0 failures; notification wording made consistent; the false remote-head claim removed; A1 aligned with A10. The resolver-integration items are parked as the P5 addendum in [document 6](06-open-items.md).

**Addition, Oct 13 (during P1, not a review round):** Q17 and [document 6](06-open-items.md) P2-1, from Chad's question "is it better to plan beforehand or to plan as we go?". M1 keeps fixed step lists. P1-P2 keep an evidence table, with denominators, of steps that had to change after an earlier one ran, and Chad decides at M1 whether a planning pass may propose amendments to pending steps. The driver alone would apply them, inside a sealed goal and set of verification obligations, with monotonic step and planning budgets. Codex reviewed the item (9 findings, all applied).

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
