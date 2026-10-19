# 9. What batch verification costs, and making it cheaper

**Status:** revision 2, Oct 19, 2026, after Codex round 7 ("rework"). Revision 1 proposed V3 and V4 as new, but both already exist (9.3). I did not search the driver before writing them; the review did. Waits for Chad's go.
**Asked by Chad, Oct 19:** "I think that's (the full suite) what we've been avoiding in batches to speed up", and: when gh-resolve handles a kira-cloudflare issue, the pre-push hook already runs the suite, so those plans should carry no verification step.

## 9.1 What the design says, and what happens

- **The design.** Step gates stay narrow, and a batch runs at most one full suite, in its verification plan (docs/06 F4; docs/07 §7.4.2). `/ilk2-plan` writes that plan only when the project has a `verify_command`, and only if the project's own instructions do not say to omit it.
- **Trial #3** (`b-da5e3354a7d1`, gh-resolve's own repo):
  - **Step gates:** 0 of 8 step gates run the suite; each names 4-13 test files.
  - **Suite runs:**
    - at the base: 322.7 s, 0 failures;
    - at the batch head, before the verification worker: about 6.6 min with attribution, 4 new failures;
    - after the worker's attempt: once more.
- **Cost of the verification step** in earlier gh-resolve batches: 18a took 94.2 min over 3 attempts (E6), and 18c took 128.5 min over 2 attempts, its slowest step by about 3×. The suite runs themselves are a small part of that. E6 puts all of ilk2's own time at 61 min of 18a's 13 h. Most of the cost is one worker session fixing every failure at once.
- **It catches real regressions.** Trial #3's 4 failures first appear at the lane merge `5999c119`. Each step's narrow gate passed, so only a full run at the head could see them.

## 9.2 Where a full suite already runs outside the batch

| Batches on | Verification in the batch | Full suite outside it |
|---|---|---|
| kira-cloudflare (resolver: `kira-live`, `kira-shadow`) | narrow: one vitest file (`b-d1925a6c4b8e`), convex `tsc` (`b-8754b8f87a27`); `verify_command` is unset | **yes**: the lefthook pre-push runs every touched area's whole suite (frontend, voice, CLI, convex non-UI, scripts; `scripts/pre-push-targeted-checks.sh:223-253`) |
| gh-resolve's own repo | the full pytest suite (`verify_command`, `-n 8`, cap 3600 s) | **no**: its pre-push only refuses commits that name the consumer repo; there is no `.github/workflows` directory; its `AGENTS.md` says nothing about verification |

## 9.3 Decisions

| # | Decision | Kind | Status |
|---|---|---|---|
| V1 | **kira-cloudflare resolver plans carry no verification sub-plan.** The pre-push runs the touched suites. The mechanism already exists in gh-resolve (its reading of its own code, Oct 19):<br>• the renderer adds the sub-plan unless the run's ship contract delegates verification (`ship.verification_subplan: "optional"`, read by `worktree.effective_delegation`, gh-resolve `handoff.py:3513-3545`, `:2965-2983`);<br>• neither host enables it today (0 matches in either host's config).<br>gh-resolve will turn it on in both hosts' configs, and add a MASTER line saying why. Native `/ilk2-plan` already writes none, because kira-live has no `verify_command`. Caveat from gh-resolve: the hook runs under `set -euo pipefail`, so a red push reports only the first failing suite | Chad's direction, Oct 19 | **decided:** Chad confirmed it directly to gh-resolve-65 (Oct 19). gh-resolve is building it, to land after 19a's deploy (v0.132.0); the version that carries it is not yet reported |
| V2 | **Attribute failures to lane merges, and re-verify after repairs.** <br>• **Mapping:** a failure first seen at a lane-merge commit is attributed to the plan that merge brought in, not `foreign`. Today only a step's accepted commit is mapped (`src/ilk2/attribution.py:90-93`), so trial #3's 4 failures read `foreign` although the batch introduced them. Codex confirmed it: `lane.merged` assigns `5999c119` to `reconcile-acts-once-on-a-row-without-a-run-id`, and its second parent is that plan's accepted commit. <br>• **A durable record:** `plans.merge_commit`, written in the same transaction as the plan's `shipped` transition. Today `lane.merged` is only an event, emitted after `shipped` (`src/ilk2/driver.py:290`), so a crash between the two loses it, and `plans` has no such column (`src/ilk2/store.py:67`). <br>• **The repair anchor** is the plan's last step, which fits `repair_of_step_id`. All failures on one plan-merge edge go into one repair plan. <br>• **Re-verify after repairs.** When a verification run creates or joins repairs, its gates-first attempt ends as measured, and the step stays pending. After the repairs, it runs the full suite again, and is accepted only when that run is green. Today an all-attributed failure set becomes pass-with-attributed (`src/ilk2/driver.py:1083`, `:1160-1169`), and the gates-first path accepts the verification step at once (`:705-715`). The repairs then run only their narrowed tests (`:1198`), so nothing re-runs the suite. This gap exists today, for failures attributed to steps; V2 would make it common | design; the re-verify part is a fix, **built Oct 19** | mapping: proposed; re-verify: built |
| V3 | **Single-flight base measurement.** Cross-batch reuse already exists (F5, since Oct 14): `measure_base` reuses a measurement from the same project, spec version and commit, up to 24 h old (`src/ilk2/driver.py:427-445`; `tests/test_driver.py:1111`). But 18b and 18c started 24 s apart, and each recorded 11 `base.measured` events and 0 `base.reused`: they raced before either had a result. <br>• **Proposal:** a batch atomically claims a measurement key. Later batches wait for the owner's result, and take over only when the owner's PID and start time are dead. No SQLite transaction is held while the suite runs. <br>• **The key** adds what `spec_version` lacks (`src/ilk2/gates/__init__.py:69-94`): a digest of the project's setup command, digests of copied `home_files`, and a key version | design | proposed |
| V4 | ~~No worker when the head is green.~~ **Already built:** a green verification run is accepted with no worker session (`src/ilk2/driver.py:705-715`; test at `tests/test_driver.py:728`) | — | exists |
| V5 | **gh-resolve's own repo keeps its in-batch full-suite verification.** gh-resolve-65's decision, Oct 19. <br>• **Basis:** it is the only full-suite run before work reaches a branch gh-resolve merges, and it caught real regressions (4 in 19a, 44 in 18a). <br>• **What changes on gh-resolve's side:** when ilk2's branch fast-forwards onto `main`, gh-resolve skips its own post-merge full suite, because the verification step already ran it at that tree. It keeps the post-merge suite only for real merges. <br>• **Falsifier (gh-resolve's):** wrong if, over the next 3 batches, what the step catches is also caught by gh-resolve's post-merge suite, at a cost above one more repair cycle. gh-resolve compares what each run found. <br>• **gh-resolve also asks ilk2 to close V2's re-verify gap:** "every red was explained" is not the same as "the repaired tree is green" | gh-resolve's call (Chad, Oct 19: "ask gh-resolve session and take whatever it says") | **decided: keep** |

**Judgment calls:**
- **V2.** Several small repairs, each running only its failing tests, should beat one 94-128 min worker session. This is unmeasured. It is wrong if repairs per plan together take longer than one worker fixing all failures. The first batches with V2 measure it.
- **V3.** A waiting batch costs nothing until the owner finishes, but the owner's crash costs the waiters a takeover delay. Wrong if takeovers become frequent.

## 9.4 Build and verification (after review)

1. **V2:**
   - a failure first seen at a lane-merge commit is attributed to that plan, and opens one repair per plan-merge edge;
   - a truly foreign failure stays `foreign`;
   - `plans.merge_commit` survives a crash between merge and `shipped`;
   - a verification step that created repairs is accepted only after a post-repair full suite is green.
2. **V3:**
   - two batches on the same key, started together, measure once;
   - an owner whose process is dead is taken over;
   - a changed setup command or home file re-measures.
4. The full suite runs once before the commit.
