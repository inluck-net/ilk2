# 5. Open questions

These need a decision before or during P1. Each has a proposed default; Chad decides.

| # | Question | Proposed default | Why it matters |
|---|---|---|---|
| Q1 | Dispatch layer: Claude Agent SDK or the Claude Code CLI? | Spike both in P1. Prefer the SDK if it works with MiMo | Decides how sessions, resume and hooks are controlled, and removes stream-log parsing |
| Q2 | Plan format: keep V1 Markdown, or define an ilk2 JSON/YAML format? | Import V1 Markdown unchanged; define a JSON form for machine submitters (gh-resolve) | Keeps `/ilk-plan` working without changes |
| Q3 | Where do plans live once submitted? | In the store, plus a read-only rendered copy for humans. Edits go through `ilk2 amend` | V1's plan files with several writers are root cause 2 |
| Q4 | Consumer interface: CLI with JSON, or a local socket? | CLI with JSON and `api_version` | gh-resolve already shells out; a socket adds lifecycle burden |
| Q5 | First real consumer for P3? | `gh-triage` or `todo-triage` (small repos with tests) | Low risk, but must have real gates |
| Q6 | Retry policy per step (attempts, resume vs fresh session)? | 3 attempts; resume the same session with red evidence; a fresh session on the 3rd | Needs a judgment-call label and data from P2 |
| Q7 | Attempt time cap? | 60 min (V1's current), measured and revisited after M1 | V1 timeouts were often caused by gates that could not pass, not by slow work |
| Q8 | Does ilk2 run its own CI full suite? | Yes, on every push (macOS runner) while the suite stays under about 5 min | Keeps V1's no-full-suite workarounds out of V2 |
| Q9 | ilk-skills repo visibility vs ilk2 | ilk2 private; ilk-skills is public today | Decide whether ilk2 should match |
