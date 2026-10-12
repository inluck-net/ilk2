---
plan: a-stale-postmortem-blacklists-nothing
status: shipped
current_step: 2
tickets: [backlog-20b15e294c3c837d]
priority: P1
estimated_steps: 2
last_updated: 2026-10-10
verification_tier: loop-verified
regression_for:
depends_on: []
data_prereqs: []
env_prereqs: []
local_checks: []
scope_paths:
  - "skills/ilk-watchdog/scripts/blacklist_status.py"
  - "skills/ilk-watchdog/tests/test_a_stale_postmortem_blacklists_nothing.py"
unit_test_targets: [skills/ilk-watchdog/tests/test_a_stale_postmortem_blacklists_nothing.py]
e2e_test_targets: []
must_add_tests: true
ci_required: false
ci_status_endpoint: github
extra_dangerous_paths: []
allow_dangerous_paths: []
expected_entities: {migrations: [], api_endpoints: [], db_tables: []}
---

# Sub-plan: a stale postmortem blacklists nothing

Part of [MASTER-2026-10-10a-the-pipeline-says-why-it-waits-execution-plan.md](./MASTER-2026-10-10a-the-pipeline-says-why-it-waits-execution-plan.md).
**Order #2.**

## Before you start

1. Read the MASTER, especially judgment call (c). It is binding.
2. Read `skills/ilk-loop/references/detached-component-contracts.md`, the section on the
   runner's exit sentinel (`last-exit.json`) and blacklist semantics.
3. Anchors at base `376b3be5`, in `skills/ilk-watchdog/scripts/blacklist_status.py`:
   - The docstring at `:1-27` states the decision. `latest_postmortem(project_data_dir)`
     at `:87` returns `(classification, generated_at, master)` of the newest postmortem
     by mtime.
   - `is_blacklisted(project_data_dir, now=None, master=None)` begins around `:124` and
     builds `res`.
   - Postmortems are `<data_dir>/runtime/launcher/postmortems/<run-id>.md`. Their
     front-matter carries `run_id: "YYYYMMDD-HHMMSS"`.
   - The runner's exit sentinel is `<data_dir>/runtime/launcher/last-exit.json`
     (`{"state", "run_id", "ended_at", ...}`).
4. Consumers are scheduler.sh, scheduler.ps1, scheduler_scan.py, status_all.py,
   park_master.py, triage_apply.py, doctor.py, and render_tray/render_xbar. The
   `is_blacklisted` result keys they read must keep their meaning. Only add.

## Tickets in scope

| Ticket | Title | Type | Pri | Module |
|---|---|---|---|---|
| backlog-20b15e294c3c837d | a run with no postmortem of its own inherits the previous run's blacklist class | bug | P1 | blacklist_status.py |

## Design (binding)

- `latest_postmortem` additionally exposes the newest postmortem's `run_id`. Use the
  front-matter `run_id`, else the file stem. Do it through a new function
  `latest_postmortem_run_id(project_data_dir) -> str | None`; the 3-tuple's shape does
  not change.
- `last_exit_run_id(project_data_dir) -> str | None` reads `last-exit.json`. It returns
  None on a missing file, bad JSON, or no `run_id`.
- In `is_blacklisted`, if the newest postmortem's class is a blacklist class, and
  `last_exit_run_id` is not None, and it is lexically **greater** than the postmortem's
  run id, then the result is NOT blacklisted with `reason: "stale-postmortem"`. Also add
  `stale_postmortem_run_id` and `last_run_id`.
- In every other case the decision is exactly as today (backoff, resolve-ack, master
  scoping). The stale check comes **before** the backoff check.
- Update the module docstring's "Decision" list with the new rule and its incident
  (run 20261009-143942).

## Acceptance criteria

All with a `tmp_path` data dir holding `runtime/launcher/postmortems/*.md` and
`runtime/launcher/last-exit.json`, and `now` set to 10 minutes after the postmortem's
`generated_at`.

- **AC-1 (stale):** postmortem `20261009-133401` with `local-checks-stuck`, and
  last-exit `run_id: 20261009-143942`. Not blacklisted, with
  `reason == "stale-postmortem"`.
- **AC-2 (own run, control):** the same postmortem with last-exit `run_id: 20261009-133401`.
  Blacklisted, as today.
- **AC-3 (no sentinel, control):** with no last-exit.json, blacklisted as today.
- **AC-4 (older sentinel, control):** last-exit `run_id: 20261009-120000`, older than the
  postmortem. Blacklisted as today.
- **AC-5 (clean newest, control):** a newest postmortem with `clean-success` is not
  blacklisted, with the same `reason` value as today.
- **AC-6 (through the scheduler's entry point):** AC-1's data dir, run as the CLI the
  scheduler calls (`scheduler.sh:1237`): `python3 skills/ilk-watchdog/scripts/blacklist_status.py check --project <dir>`
  in a subprocess with `HOME`/`ILK_DATA_HOME` set to tmp dirs. The JSON on stdout says
  not blacklisted with `reason == "stale-postmortem"`. On AC-2's dir it says blacklisted.
  Mark it xfail-strict at step 0 together with AC-1.

## Steps

### Step 0 — red-first pins

```yaml
local_checks:
  - command: "python3 -m pytest skills/ilk-watchdog/tests/test_a_stale_postmortem_blacklists_nothing.py -q -p no:cacheprovider --timeout=120 --timeout-method=signal"
    timeout: 180
```

- Write `skills/ilk-watchdog/tests/test_a_stale_postmortem_blacklists_nothing.py` with one test per AC. Mark AC-1 xfail-strict with the MASTER's `raises=` tuple.
  AC-2..AC-5 are unmarked controls.
- Pin shape (literal): `@pytest.mark.xfail(strict=True, raises=(AssertionError, ImportError, AttributeError, TypeError), reason="not built yet")`. The step-0 gate passes because every pin xfails.
- Commit: `test(blacklist): pin that a previous run's postmortem does not blacklist the next run [plan:a-stale-postmortem-blacklists-nothing#step-0]`

### Step 1 — key the decision to the last run; widen the gate

```yaml
local_checks:
  - command: "python3 -m pytest skills/ilk-watchdog/tests/test_a_stale_postmortem_blacklists_nothing.py skills/ilk-watchdog/tests/test_blacklist_ack.py skills/ilk-watchdog/tests/test_a_blacklist_holds_one_master.py skills/ilk-watchdog/tests/test_a_blacklisted_run_is_triaged.py skills/ilk-launcher/tests/test_ilk_watch.py skills/ilk-loop/tests/test_a_day_of_decisions_renders_as_one_page.py skills/ilk-loop/tests/test_a_deferred_merge_is_retried_when_nothing_is_runnable.py skills/ilk-loop/tests/test_a_human_park_holds_the_project.py skills/ilk-loop/tests/test_a_pending_merge_holds_new_work.py skills/ilk-loop/tests/test_frontmatter_inline_comments.py skills/ilk-loop/tests/test_status_all_batch_fields.py skills/ilk-loop/tests/test_status_all_heartbeat.py skills/ilk-loop/tests/test_status_all_model_fallback.py skills/ilk-loop/tests/test_status_all_repo_path.py skills/ilk-loop/tests/test_status_all_shared_blocks_once.py skills/ilk-loop/tests/test_status_all_skips_blocked.py skills/ilk-loop/tests/test_status_all_steer_paused.py skills/ilk-loop/tests/test_status_read_is_pure.py skills/ilk-loop/tests/test_superseded_parks_are_not_owed.py skills/ilk-loop/tests/test_the_alert_names_the_failing_check.py skills/ilk-loop/tests/test_the_page_shows_every_batch_and_judgment_call.py skills/ilk-loop/tests/test_the_panel_shows_parked_work.py skills/ilk-loop/tests/test_the_panel_shows_the_gate.py skills/ilk-runner/tests/test_doctor_resolution.py skills/ilk-runner/tests/test_doctor_scheduler_visibility.py skills/ilk-runner/tests/test_doctor.py skills/ilk-watchdog/tests/test_a_plain_red_is_relaunched_not_escalated.py skills/ilk-watchdog/tests/test_a_release_train_runs_alone.py skills/ilk-watchdog/tests/test_a_stopped_run_gets_a_diagnosis.py skills/ilk-watchdog/tests/test_a_triage_decision_is_applied_within_its_rails.py skills/ilk-watchdog/tests/test_a_triage_test_never_writes_the_real_backlog.py skills/ilk-watchdog/tests/test_a_triaged_failure_becomes_an_improvement_candidate.py skills/ilk-watchdog/tests/test_dispatch_marker_per_master.py skills/ilk-watchdog/tests/test_dispatch_on_drain.py skills/ilk-watchdog/tests/test_master_pending_as_queued.py skills/ilk-watchdog/tests/test_master_selection_agreement.py skills/ilk-watchdog/tests/test_scheduler_dispatch.py skills/ilk-watchdog/tests/test_scheduler_scan_resilience.py skills/ilk-watchdog/tests/test_scheduler_scan_ts_awareness.py skills/ilk-watchdog/tests/test_the_scheduler_writes_yesterdays_page_once.py skills/ilk-watchdog/tests/test_triage_escalates_a_kernel_steer.py skills/ilk-watchdog/tests/test_verify_dispatch_guards.py --deselect skills/ilk-watchdog/tests/test_a_plain_red_is_relaunched_not_escalated.py::TestAC2_BoundAtMostTwice::test_third_consecutive_red_goes_to_decide -n 8 -q -p no:cacheprovider --timeout=120 --timeout-method=signal" 
    timeout: 300
```

- Baseline-green on darwin: the widened set's existing files (50) measured at base `376b3be5` on 2026-10-10:
  486 passed, 1 failed in 16.3 s with `-n 8`. The 1 is `test_third_consecutive_red_goes_to_decide`, red alone at base
  AND at its own shipping commit `c29c93d7`, and not in baseline_red. It is deselected here and filed as row
  `6e6f8232096ed728`. Do not fix it in this batch. Any OTHER red: run it alone at base before blaming this batch.
- Implement "Design (binding)". Remove AC-1's xfail.
- Commit: `fix(blacklist): a postmortem older than the last run blacklists nothing [plan:a-stale-postmortem-blacklists-nothing#step-1]`

## Out of scope

- The scheduler's 300 s postmortem grace (v0.9.186). It stays.
- Why a postmortem was written late.

## Findings

_(filled by the loop during execution)_

## Reference reading

- `skills/ilk-watchdog/scripts/blacklist_status.py`
- `skills/ilk-loop/references/detached-component-contracts.md`
