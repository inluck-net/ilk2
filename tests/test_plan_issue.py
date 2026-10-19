"""`ilk2 plan-issue`: a headless /ilk2-plan for one consumer issue (docs/07 §7.4.2b)."""

from __future__ import annotations

import json

from ilk2 import cli, paths, plan_issue

ISSUE = {"repo": "keyreply/kira-cloudflare", "number": 7, "title": "add works",
         "body": "add(1, 2) is 3, and add(0, 0) is 0", "url": "https://x/7"}
SCOPE = "source: keyreply/kira-cloudflare#7\nsource_tickets: 1\nsource_ac_groups: 2\n"
SUB = """---
plan: a
---

## Steps

### Step 0 — feat(calc): add adds
```yaml
local_checks:
  - command: {check}
```
"""


def drafts():
    return paths.home() / "plans" / "proj" / "issue-7-r1"


def plan_files(scope=SCOPE, check="python -m pytest -q tests/test_calc.py"):
    d = drafts()
    master = (f"---\ntitle: issue 7\nbase_branch: main\n{scope}---\n\n"
              "| # | Sub-plan |\n|---|---|\n| 1 | [a](./issue-7-r1-a.md) |\n")
    return [{"write": str(d / "MASTER-issue-7-r1-execution-plan.md"), "text": master},
            {"write": str(d / "issue-7-r1-a.md"), "text": SUB.format(check=check)}]


def run(ilk, tmp_path, cap_s=None, **over):
    raw = {"issue": ISSUE, "project_root": str(ilk.repo), "base_branch": "main", "run_id": "r1",
           "scope_paths": [], "write_targets": [], "local_checks": [], "master_frontmatter": {},
           "deny_paths": ["AGENTS.md"], **over}
    inp, out = tmp_path / "issue.json", tmp_path / "outcome.json"
    inp.write_text(json.dumps(raw))
    if cap_s:   # the CLI has no cap; PLAN_CAP_S is not scaled by ILK2_TIME_SCALE
        code = plan_issue.run(ilk.s, inp, out, cap_s=cap_s)
    else:
        code = cli.main(["plan-issue", "--input", str(inp), "--outcome", str(out)])
    return code, json.loads(out.read_text())


def test_a_clean_plan_is_drafted_and_checked_and_never_submitted(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    ilk.script(plan_files())
    code, out = run(ilk, tmp_path)
    assert (code, out["outcome"]) == (0, "drafted-checked"), out
    assert out["meta"] == {"scope": {"source": "keyreply/kira-cloudflare#7", "tickets": 1, "ac_groups": 2}}
    assert out["source_ac_groups"] == 2 and out["master_path"].endswith("MASTER-issue-7-r1-execution-plan.md")
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0             # never submitted
    prompt = ilk.stub_prompts()[0]
    assert "/ilk2-plan --draft" in prompt and str(drafts()) in prompt
    assert "write no batch-verification sub-plan" in prompt                 # no verify_command
    assert out["session"] == "done"
    assert "Do not run the project's tests" in prompt and "`$TMPDIR`" in prompt


def test_bad_input_fails_and_starts_nothing(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    code, out = run(ilk, tmp_path, issue={"repo": "x"}, run_id="")
    assert code == 1 and out["outcome"] == "failed" and ilk.stub_calls() == 0
    assert "bad-input: issue.number" in out["problems"] and "bad-input: run_id" in out["problems"]


def test_no_master_or_an_unplannable_issue_fails(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    ilk.script([{"write": str(drafts() / "notes.txt"), "text": "thinking"}])
    code, out = run(ilk, tmp_path)
    assert code == 1 and "0 MASTER files" in out["problems"][0]
    ilk.script([{"write": str(drafts() / "UNPLANNABLE.txt"), "text": "the issue has no acceptance"}])
    code, out = run(ilk, tmp_path)
    assert code == 1 and out["problems"] == ["unplannable: the issue has no acceptance"]


def test_a_refused_check_or_a_standing_warning_is_only_drafted(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    ilk.script(plan_files(check="no-such-tool x"))                          # refused at the check
    code, out = run(ilk, tmp_path)
    assert (code, out["outcome"]) == (3, "drafted") and out["problems"]
    ilk.script(plan_files(check="python -m pytest -q tests/test_calc.py tests/test_74a.py"))
    code, out = run(ilk, tmp_path)                                          # a gate-path warning
    assert (code, out["outcome"]) == (3, "drafted")
    assert [w["kind"] for w in out["warnings"]] == ["gate-path-absent"]


def test_a_master_without_its_scope_is_only_drafted(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    ilk.script(plan_files(scope=""))
    code, out = run(ilk, tmp_path)
    assert (code, out["outcome"]) == (3, "drafted") and "scope" in out["problems"][0]


def test_a_planner_that_edits_the_checkout_fails(ilk, tmp_path):
    ilk.make_repo()
    ilk.project()
    ilk.script(plan_files() + [{"write": "calc.py", "text": "oops\n"}])
    code, out = run(ilk, tmp_path)
    assert code == 1 and "changed the checkout" in out["problems"][0]


def test_a_session_cut_at_the_cap_says_so_and_points_at_its_drafts(ilk, tmp_path):
    # Oct 19, #8751: cut at the cap, the outcome named only a stray file and master_path was null.
    ilk.make_repo()
    ilk.project()
    ilk.script(plan_files() + [{"write": "pc-out.txt", "text": "x\n"}, {"hang": True}])
    code, out = run(ilk, tmp_path, cap_s=2)
    assert (code, out["outcome"], out["session"]) == (1, "failed", "timeout")
    assert out["problems"][0].startswith("the planning session ended timeout at the")
    assert "MASTER-issue-7-r1-execution-plan.md" in out["problems"][0]
    assert "changed the checkout" in out["problems"][1]
    assert out["master_path"].endswith("MASTER-issue-7-r1-execution-plan.md")
