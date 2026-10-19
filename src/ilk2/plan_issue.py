"""`ilk2 plan-issue`: a headless /ilk2-plan for one admitted consumer issue (docs/07 §7.4.2b).

gh-resolve's resolver calls it once per issue, in place of V1's `autoplan.py plan-issue`, with the
same contract: an input JSON, an outcome JSON written atomically on every path, exit 0 / 3 / 1,
never a wait for a person. One planning session, through ilk2's own worker path and template
(never a V1 home), follows commands/ilk2-plan.md as `/ilk2-plan --draft` into the drafts dir.
Then ilk2 runs `submit --check` itself and stops: gh-resolve submits the checked MASTER (its
submit stamps its ledger, passes the run's deny paths and owns the dispatch slot).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import clock, paths

EXIT = {"drafted-checked": 0, "drafted": 3, "failed": 1}
ISSUE_FIELDS = ("repo", "number", "title", "body")
UNPLANNABLE = "UNPLANNABLE.txt"


def planner_command() -> Path:
    """The planner prompt: the installed /ilk2-plan (today linked from the ilk-skills clone;
    after the switch, a release location, docs/07 §7.4.5)."""
    env = os.environ.get("ILK2_PLANNER_COMMAND")
    return Path(env) if env else (Path.home() / ".claude" / "commands" / "ilk2-plan.md").resolve()


BRIEF = """Follow {command} exactly, as `/ilk2-plan --draft`. This session is headless: there is
no person to ask, and nothing here is submitted by you.

# Task: one admitted GitHub issue

- Repo: {repo}
- Issue: #{number}
- Title: {title}
- URL: {url}

Issue body (verbatim):

```
{body}
```

## Fixed facts (they replace the steps that would look them up)

- The ilk2 project is `{project}`. Skip step 2's registry lookup and `ilk2 status`: ilk2's store
  is not readable from this session.
- The batch id is `{batch_id}`, and the drafts dir is `{drafts}`. Write every plan file there,
  with the Write and Edit tools, and nowhere else. Do not edit this checkout: it is the project at
  the base, for reading only. Scratch output (a check's report, notes) goes under `$TMPDIR`, never
  in the checkout or `/tmp`.
- Do not run the project's tests, suites, builds or installs (`pytest`, `vitest`, `bun run test`,
  `npm test`, ...). Planning reads code; ilk2 runs the gates after a step is built.
- The project's `verify_command` is {verify}. {verify_rule}
- base_branch: {base_branch}. Write it as the MASTER's `base_branch:`.
- Record the scope in the MASTER frontmatter before planning (step 3b): `source: {repo}#{number}`,
  `source_tickets: 1`, and `source_ac_groups:` counted from the issue body.
- No step may write under these deny paths: {deny}.
- Constraints from the consumer, to carry literally:
  - scope_paths: {scope_paths}
  - write_targets: {write_targets}
  - local_checks: {local_checks}
  - frontmatter the MASTER must carry: {frontmatter}
- Skip 7h and 8: ilk2 runs `ilk2 submit --check` after you, outside this session. Do run 7g
  (`plan_check.py`) from this checkout.
- If the issue cannot be planned, write the reason to `{drafts}/{unplannable}` and stop.
"""


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def validate(raw) -> list[str]:
    if not isinstance(raw, dict):
        return ["bad-input: not an object"]
    out = []
    issue = raw.get("issue")
    if not isinstance(issue, dict):
        out.append("bad-input: issue")
    else:
        out += [f"bad-input: issue.{k}" for k in ISSUE_FIELDS if not issue.get(k)]
    for k in ("project_root", "base_branch", "run_id"):
        if not raw.get(k):
            out.append(f"bad-input: {k}")
    if raw.get("deny_paths") is not None and not isinstance(raw["deny_paths"], list):
        out.append("bad-input: deny_paths")
    return out


def _project(s, raw) -> str | None:
    if raw.get("project"):
        return raw["project"] if s.one("SELECT 1 FROM projects WHERE id = ?", raw["project"]) else None
    root = os.path.realpath(raw["project_root"])
    for r in s.all("SELECT id, repo_root FROM projects"):
        if root == os.path.realpath(r["repo_root"]) or root.startswith(os.path.realpath(r["repo_root"]) + os.sep):
            return r["id"]
    return None


def run(s, input_path: Path, outcome_path: Path, cap_s: float | None = None) -> int:
    """Plan one issue; write the outcome; return the exit code (0 / 3 / 1)."""
    out: dict = {"outcome": "failed", "problems": [], "master_path": None, "plans_dir": None,
                 "planner_log": None, "session": None, "meta": None, "source_ac_groups": None,
                 "warnings": []}

    def finish(outcome: str, *problems: str) -> int:
        out["outcome"] = outcome
        out["problems"] += [p[:1000] for p in problems]
        _write(Path(outcome_path), out)
        return EXIT[outcome]

    try:
        try:
            raw = json.loads(Path(input_path).read_text())
        except (OSError, ValueError) as e:
            return finish("failed", f"bad-input: {e}")
        bad = validate(raw)
        if bad:
            return finish("failed", *bad)
        return _plan(s, raw, out, finish, cap_s)
    except Exception as e:  # noqa: BLE001 - the outcome is written on every path
        return finish("failed", f"error: {type(e).__name__}: {e}")


def _plan(s, raw: dict, out: dict, finish, cap_s: float | None) -> int:
    from . import batches, bounds, gitops, plans, rsi, worker
    project = _project(s, raw)
    if project is None:
        return finish("failed", f"bad-input: no ilk2 project for {raw.get('project') or raw['project_root']}")
    proj = s.get("projects", project)
    cfg = batches.project_config(s, project)
    repo = Path(proj["repo_root"])
    base_sha = gitops.rev(repo, raw.get("base") or raw["base_branch"])
    if not base_sha:
        return finish("failed", f"bad-input: base {raw.get('base') or raw['base_branch']!r} is not a commit")
    issue = raw["issue"]
    batch_id = f"issue-{issue['number']}-{raw['run_id']}"
    drafts = paths.home() / "plans" / project / batch_id
    drafts.mkdir(parents=True, exist_ok=True)
    out["plans_dir"] = str(drafts)
    work = paths.data() / "plan-issue" / f"{batch_id}-{int(clock.wall())}"
    wt = work / "wt"
    gitops.add_worktree(repo, wt, base_sha)
    try:
        wcfg = worker.WorkerConfig(claude_bin=cfg.claude_bin, max_turns=cfg.max_turns,
                                   sandbox=cfg.sandbox, sandbox_allow=cfg.sandbox_allow,
                                   write_dirs=[str(drafts)],
                                   model=cfg.worker_model, effort=cfg.worker_effort)
        files = worker.AttemptFiles(work / "attempt")
        worker.prepare(wcfg, files, wt, [])
        out["planner_log"] = str(files.logs)
        verify = cfg.verify_command
        prompt = BRIEF.format(
            command=planner_command(), repo=issue["repo"], number=issue["number"],
            title=issue["title"], url=issue.get("url", ""), body=issue["body"], project=project,
            batch_id=batch_id, drafts=drafts, base_branch=raw["base_branch"],
            verify=json.dumps(verify),
            verify_rule=("Write the batch-verification sub-plan with it." if verify else
                         "It is unset, so write no batch-verification sub-plan, and say so in the MASTER."),
            deny=json.dumps(raw.get("deny_paths") or []),
            scope_paths=json.dumps(raw.get("scope_paths") or []),
            write_targets=json.dumps(raw.get("write_targets") or []),
            local_checks=json.dumps(raw.get("local_checks") or []),
            frontmatter=json.dumps(raw.get("master_frontmatter") or {}), unplannable=UNPLANNABLE)
        b = bounds.load()
        session = worker.dispatch(wcfg, files, wt, prompt)
        cap = cap_s or rsi.PLAN_CAP_S
        ended = session.wait(cap, b.term_grace, b.kill_grace)
        out["session"] = ended
        changed = gitops.changed_paths(wt)
        changed_problem = f"the planner changed the checkout, which it must not: {changed[:10]}"
        if ended != "done":
            # Name the session's end first: on Oct 19 a session cut at the cap was reported only
            # by a stray file it left, with master_path null although its MASTER was on disk.
            masters = sorted(drafts.glob("MASTER-*.md"))
            if len(masters) == 1:
                out["master_path"] = str(masters[0])
            left = sorted(p.name for p in drafts.iterdir())
            why = (f"the planning session ended {ended}"
                   + (f" at the {cap:.0f} s cap" if ended == "timeout" else "")
                   + f"; drafts left: {left[:10]}")
            return finish("failed", why, *([changed_problem] if changed else []))
        if changed:
            return finish("failed", changed_problem)
        if (drafts / UNPLANNABLE).exists():
            return finish("failed", "unplannable: " + (drafts / UNPLANNABLE).read_text().strip())
        masters = sorted(drafts.glob("MASTER-*.md"))
        if len(masters) != 1:
            return finish("failed", f"the planner wrote {len(masters)} MASTER files (session {ended})")
        master = masters[0]
        out["master_path"] = str(master)
        front = plans._frontmatter(master.read_text())
        try:
            scope = {"source": front["source"], "tickets": int(front["source_tickets"]),
                     "ac_groups": int(front["source_ac_groups"])}
        except (KeyError, TypeError, ValueError):
            return finish("drafted", "the MASTER does not record its scope (source, source_tickets, "
                                     "source_ac_groups)")
        out["meta"], out["source_ac_groups"] = {"scope": scope}, scope["ac_groups"]
        spec = plans.parse_v1_master(master)
        try:
            batches.submit(s, project, spec, base=raw.get("base"), branch=raw.get("branch"),
                           root=Path(raw["execution_root"]) if raw.get("execution_root") else None,
                           deny_paths=raw.get("deny_paths"), check_only=True)
        except batches.SubmitRejected as e:
            return finish("drafted", *e.problems)
        bb = batches.base_behind(repo, base_sha, proj["default_branch"])
        warn = batches.base_warning(bb)
        out["warnings"] = ([warn] if warn else []) + batches.plan_warnings(spec) + \
            batches.gate_path_warnings(spec, repo, base_sha)
        if out["warnings"]:
            return finish("drafted", "warnings stand (the planner's QC treats them as failures)")
        return finish("drafted-checked")
    finally:
        gitops.remove_worktree(repo, wt)
