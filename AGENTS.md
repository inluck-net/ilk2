# ilk2: rules for agents working in this repo

ilk2 is the second version of the ilk loop engine (V1 is `inluck-net/ilk-skills`). The design is in `docs/` and was closed for P1 on Oct 13, 2026, at tag `design-accepted-p1`. Read `docs/README.md` first, then the phase you are working on.

## Standing rules (Chad, Oct 12-13, 2026)

- **Built by hand.** A Claude Code session writes ilk2 and Chad reviews it. The ilk loop (V1) never edits this repo: no `/ilk-plan`, no loop batches and no self-improve against it. The auto-plan routing rule in host instructions does not apply here.
- **Mac only.** chad-mbp and rezmac. Python 3.12 managed by uv (`uv sync`, `uv run pytest`). No shell orchestration in the core.
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
- **V1 is live.** V1 loops run on both hosts from `~/.ilk/current`. Do not touch `~/.ilk`, `~/.ilk-data` (beyond the handover's reversible renames) or V1's launchd jobs (`ilk2 v1 off` only on Chad's go). Concurrency (Chad, Oct 14): at least 3 kira-cloudflare batches at once on each host. `~/.ilk2/host.json` sets `host_budget` (chad-mbp 4, rezmac 3; re-read every tick), and `kira-shadow` has `concurrency_limit` 3; the load check still holds new runs back on a busy host.
- **ilk-skills' own development runs on ilk2** (Chad, Oct 14: "go ahead with the ilk-skills takeover"). ilk2 batches edit the clone at `/Users/chad/Projects/github/inluck-net/ilk-skills` through their own worktrees and `ilk2/b-*` branches; live V1 keeps running from its release in `~/.ilk/current`, not from the clone. A build session still does not edit the clone by hand.

## Where things are

- Design: `docs/01`-`06`, plus `docs/evidence/`.
- HTML summary for Chad: `~/Documents/keyreply/chad/ilk-skills/20261012-ilk2-v2-design-vs-v1.html`. Update it when the design changes.
- Handoffs: `~/Documents/handoffs/ilk2-*-handoff-*.md`.
