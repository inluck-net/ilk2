# ilk2: rules for agents working in this repo

ilk2 is the second version of the ilk loop engine (V1 is `inluck-net/ilk-skills`). The design is in `docs/` and was closed for P1 on Oct 13, 2026, at tag `design-accepted-p1`. Read `docs/README.md` first, then the phase you are working on.

## Standing rules (Chad, Oct 12-13, 2026)

- **Built by hand, and by RSI** (Chad, Oct 14: "Reviewed RSI first"; Oct 15: "RSI branches may merge and release without your review, as long as we can track back with some kind of audit logs"). A Claude Code session writes ilk2, and ilk2 improves itself: it finds, plans and builds batches on its own repo (ilk2 project `ilk2`), and lands them unattended (`src/ilk2/rsi_land.py`):
  - merge into `main` in this checkout, only when it is clean;
  - verify the full suite on the merged commit;
  - release when idle;
  - canary on chad-mbp, with automatic rollback;
  - push to rezmac after 24 h.

  Every landing is audited: git trailers `ilk2-rsi-batch`/`-candidate`, the `rsi_landings` table (`ilk2 rsi log`), and `rsi.*` events. A batch that touches the protected kernel, removes a test or an assert, fails to merge, or is red after merging is left for review (a needs_human `rsi-review` row). The kernel has two parts (Chad, Oct 16). Governance (this file, `CLAUDE.md`, `docs/rsi/**`, `.github/**`) is never editable by RSI. RSI's own machinery (the RSI and release code and their tests) may be fixed by RSI batches, which then always land by review. Chad's inputs are `docs/rsi/requests.json`, GitHub issues labelled `rsi:req` by chad007, and goals in `docs/rsi/goals.json`. The brake is the project setting `rsi_land`. The ilk loop (V1) still never edits this repo: no `/ilk-plan`, no V1 loop batches and no V1 self-improve against it. The auto-plan routing rule in host instructions does not apply here.
- **Mac only.** chad-mbp and rezmac. Python 3.12 managed by uv (`uv sync`, `uv run pytest`; a full local run is `uv run pytest -n 4`, about 2 min instead of 6.5-9). No shell orchestration in the core.
- **Credentials.**
  - Never print worker settings, tokens, full plists, environment dumps or full process arguments.
  - The ilk2 worker template lives in `~/.ilk2/worker-template/` and is ilk2's own copy.
  - ilk2 code never reads V1 worker homes (`~/.claude-worker*`, `~/.claude-manager`) at runtime.
- **Phases and reviews.**
  - Work follows the phases in `docs/04` §4.4.
  - There are no more design review rounds on paper. Reviews happen at phase boundaries against code: after P1, at M1, before P4 and before P5.
  - New findings for later phases go into `docs/06-open-items.md`, tagged with the phase that needs them.
- **Spike vs core.** P1 code goes in `spikes/p1/`. It is evidence, not the core. Code that graduates to `src/ilk2/` is rewritten there with tests.
- **Every change to code logic** states its acceptance criterion before editing. It is verified by a test that fails without it, or by a live run whose output is reported.
- **Commits and pushes** to this repo are made when Chad asks, or when a phase step's stated exit criterion requires CI.
- **V1 is retired** (P7, both hosts, Oct 15): its scheduler is disabled on chad-mbp and rezmac (`ilk2 v1 status` is empty; rollback `ilk2 v1 on --apply` only on Chad's go). Do not touch `~/.ilk`, `~/.ilk-data` (beyond the handover's reversible renames) or V1's launchd plists: they are history and the rollback. Concurrency (Chad, Oct 14): at least 3 kira-cloudflare batches at once on each host. `~/.ilk2/host.json` sets `host_budget` (chad-mbp and rezmac 6 since Oct 16, Chad: "more than 5"; re-read every tick), and `kira-shadow` has `concurrency_limit` 3; the load check still holds new runs back on a busy host.
- **ilk-skills' own development runs on ilk2** (Chad, Oct 14: "go ahead with the ilk-skills takeover"). ilk2 batches edit the clone at `/Users/chad/Projects/github/inluck-net/ilk-skills` through their own worktrees and `ilk2/b-*` branches; live V1 keeps running from its release in `~/.ilk/current`, not from the clone. A build session still does not edit the clone by hand.

## Where things are

- Design: `docs/01`-`06`, plus `docs/evidence/`.
- HTML summary for Chad: `~/Documents/keyreply/chad/ilk-skills/20261012-ilk2-v2-design-vs-v1.html`. Update it when the design changes.
- Handoffs: `~/Documents/handoffs/ilk2-*-handoff-*.md`.
