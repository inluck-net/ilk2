# 5. Open questions

These need a decision before or during P1. Each has a proposed default; Chad decides. Record decisions in the Status column.

| # | Question | Proposed default | Why it matters | Status |
|---|---|---|---|---|
| Q1 | Dispatch layer: Claude Agent SDK or the Claude Code CLI? | Spike both in P1. Prefer the SDK if it works with MiMo | Decides how sessions, resume and hooks are controlled, and removes stream-log parsing | open |
| Q2 | Plan format: keep V1 Markdown, or define an ilk2 JSON/YAML format? | Import V1 Markdown unchanged; define a JSON form for machine submitters (gh-resolve) | Keeps `/ilk-plan` working without changes | open |
| Q3 | Where do plans live once submitted? | In the store, plus a read-only rendered copy for humans. Edits go through `ilk2 amend` | V1's plan files with several writers are root cause 2 | open |
| Q4 | Consumer interface: CLI with JSON, or a local socket? | CLI with JSON and `api_version` | gh-resolve already shells out; a socket adds lifecycle burden | open |
| Q5 | First real consumer for P3? | `gh-triage` or `todo-triage` (small repos with tests) | Low risk, but must have real gates | open |
| Q6 | Retry policy per step (attempts, resume vs fresh session)? | 3 attempts; resume the same session with red evidence; a fresh session on the 3rd | Needs a judgment-call label and data from P2 | open |
| Q7 | Attempt time cap? | 60 min (V1's current), measured and revisited after M1 | V1 timeouts were often caused by gates that could not pass, not by slow work | open |
| Q8 | Does ilk2 run its own CI full suite? | Yes, on every push (macOS runner) while the suite stays under about 5 min | Keeps V1's no-full-suite workarounds out of V2 | open |
| Q9 | ilk-skills repo visibility vs ilk2 | ilk2 private; ilk-skills is public today | Decide whether ilk2 should match | open |
| Q10 | What does the brief preload, and which worker settings are set per project? | The P1 spike runs one real V1 step twice: once with a discovery brief (V1 style) and once with a preloaded brief (named files and symbols, their tests, red output, earlier-step diff). Adopt whichever has fewer minutes to first edit without a lower acceptance rate. Record effort, thinking settings and the model per attempt; the Oct 14 flash trial is the first model input | 42% of V1 iteration time passed before the first edit and 56% was thinking ([evidence](evidence/2026-10-12-v1-stuck-and-slow.md) §2). This is the largest share of a step that the engine can influence | open (added Oct 12) |
