# ilk2 design documents

Draft for Chad's review, Oct 12, 2026. Author: Claude Code session ilk-skills-aa.

| # | Document | Question it answers |
|---|---|---|
| 1 | [Why V2](01-why-v2.md) | Why build a new core instead of patching V1? |
| 2 | [Architecture](02-architecture.md) | What are the parts, and how does a batch run end to end? |
| 3 | [V1 vs V2](03-v1-vs-v2.md) | What changes, what improves, and what V2 will *not* fix |
| 4 | [Relation to V1 and migration](04-v1-relation-and-migration.md) | How V2 sits beside V1, how a project moves over, and when we stop |
| 5 | [Open questions](05-open-questions.md) | What still needs a decision before code |

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
