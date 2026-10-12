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
- [document 2](02-architecture.md) §2.11: bounds table, checks at submission, notification, and the stall suite S1-S12 required for M1;
- Q11-Q12.

## Decisions already made (Chad, Oct 12, 2026)

- **Mac only.** chad-mbp and rezmac; no Windows or PowerShell.
- **Worker model: keep MiMo** (`mimo-v2.6-pro`) through Claude Code.
- **Built by hand.** A Claude Code session writes it and Chad reviews; the ilk loop never edits this repo.
- **Separate repo** (this one), separate data home `~/.ilk2/`, separate command namespace (`/ilk2-…`, `ilk2 …`).
- **Python 3.12, managed by uv** and bundled into each release; SQLite state; CI on a macOS GitHub runner.
- **V1's D2 code-only trial finishes first.** Its result is evidence for this design, not a prerequisite for writing it.

## Ground rules for this design

- **Every V1 pain point cited here has a source:** a V1 retro, a Claude memory note, a backlog row or a measured log. V1 references are to `inluck-net/ilk-skills` at `d17ffdcd` (Oct 12, 2026) unless another commit is named.
- **Size target:** core under about 5k lines of Python, excluding tests. If it grows past that, this design is being repeated as V1.
- **Speed claims follow the V1 measurement method.** Measured means all attempts counted, per accepted step. Nothing here claims a speedup that has not been measured.
