import subprocess
from pathlib import Path

from ilk2 import plans

FIX = Path(__file__).parent / "fixtures" / "v1"


def _spec(**step):
    s = {"title": "t", "instructions": "do it",
         "gates": [{"name": "unit", "command": "python3 -m pytest tests/a.py"}]}
    s.update(step)
    return plans.from_dict({"plans": [{"slug": "a", "steps": [s]}]})


def test_json_gate_adapter_resolved():
    spec = _spec()
    g = spec.plans[0].steps[0].gates[0]
    assert g.adapter == "pytest" and g.command[:3] == ["python3", "-m", "pytest"]
    assert plans.check(spec) == []


def test_s9_step_without_gate_rejected():
    spec = _spec(gates=[])
    assert any("declares no gate" in p for p in plans.check(spec))


def test_s9_missing_executable_rejected(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    spec = _spec(gates=[{"name": "unit", "adapter": "pytest", "command": ["no-such-runner-xyz", "x"]}])
    probs = plans.check(spec, root=tmp_path, path_dirs=["/usr/bin", "/bin"])
    assert any("not on the pinned PATH" in p for p in probs)


def test_shell_test_gate_rejected():
    spec = _spec(gates=[{"name": "t", "command": "make test"}])
    assert any("cannot use the shell adapter" in p for p in plans.check(spec))
    spec = _spec(gates=[{"name": "lint", "command": "make lint", "declares_tests": False}])
    assert plans.check(spec) == []


def test_dependency_missing_and_cycle():
    spec = plans.from_dict({"plans": [
        {"slug": "a", "depends_on": ["b"], "steps": [{"instructions": "x", "gates": [
            {"command": "pytest"}]}]},
        {"slug": "b", "depends_on": ["a"], "steps": [{"instructions": "x", "gates": [
            {"command": "pytest"}]}]},
        {"slug": "c", "depends_on": ["zzz"], "steps": [{"instructions": "x", "gates": [
            {"command": "pytest"}]}]},
    ]})
    probs = plans.check(spec)
    assert any("cycle" in p for p in probs)
    assert any("missing plan zzz" in p for p in probs)


def test_root_must_be_git_worktree(tmp_path):
    assert any("not a git worktree" in p for p in plans.check(_spec(), root=tmp_path))
    assert any("does not exist" in p for p in plans.check(_spec(), root=tmp_path / "nope"))


def test_v1_subplan_import():
    f = next(FIX.glob("*.md"))
    p = plans.parse_v1_subplan(f.read_text())
    assert p.slug == "a-stale-postmortem-blacklists-nothing"
    assert [s.title for s in p.steps][:2] == ["red-first pins",
                                              "key the decision to the last run; widen the gate"]
    g0 = p.steps[0].gates[0]
    assert g0.adapter == "pytest" and g0.cap_s == 180
    assert "python3" == g0.command[0]
    assert "--deselect" in p.steps[1].gates[0].command
    assert p.steps[1].scope
    assert plans.check(plans.BatchSpec(plans=[p])) == []


def test_v1_remove_every_xfail_maps_to_file_must_pass():
    text = ('---\nplan: x\n---\n### Step 1 — fix\n\n```yaml\nlocal_checks:\n'
            '  - command: "python3 -m pytest skills/a/tests/test_x.py skills/a/tests/test_y.py -q"\n'
            '    timeout: 300\n```\n\n- Implement it. Remove every xfail.\n')
    p = plans.parse_v1_subplan(text)
    assert p.steps[0].must_pass == ["skills/a/tests/test_x.py"]
    partial = text.replace("Remove every xfail", "Remove AC-1's xfail")
    assert plans.parse_v1_subplan(partial).steps[0].must_pass == []


def test_s9_rejected_at_submit_and_nothing_runs(ilk):
    import pytest as _pytest
    from ilk2 import batches
    ilk.make_repo()
    ilk.project()
    for g in ([], [{"name": "unit", "adapter": "pytest", "command": ["no-such-runner-xyz", "x"]}]):
        spec = {"plans": [{"slug": "p", "steps": [{"title": "t", "instructions": "x", "gates": g}]}]}
        with _pytest.raises(batches.SubmitRejected):
            ilk.submit(spec)
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0
    assert ilk.s.one("SELECT COUNT(*) n FROM runs")["n"] == 0


def test_submit_preflight_rejects_a_gate_that_cannot_run(ilk, tmp_path):
    # docs/06 "Self-resolution": the P3 block of Oct 13 (pytest hidden by the pinned HOME)
    # must be a synchronous rejection at submit, not a needs_human row found later.
    import json as _json
    import pytest as _pytest
    from ilk2 import batches
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "python").write_text("#!/bin/sh\necho 'No module named pytest' >&2\nexit 1\n")
    (fake / "python").chmod(0o755)
    ilk.make_repo()
    ilk.project(path_prelude=[str(fake), "/usr/bin", "/bin"])
    with _pytest.raises(batches.SubmitRejected, match="runner-not-found"):
        ilk.submit({"plans": [{"slug": "p", "steps": [{"title": "t", "instructions": "x",
                    "gates": [{"name": "unit", "adapter": "pytest",
                               "command": ["python", "-m", "pytest", "-q", "tests"]}]}]}]})
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0


SUBPLAN_WITH_FINDINGS = """---
plan: waits
---

# Sub-plan: Waits

## Steps

### Step 0 — Add the regression
```yaml
local_checks:
  - command: "python3 -m pytest -q tests/test_a.py"
    timeout: 600
  - command: "bash -c '! grep -nE mark.xfail tests/test_a.py'"
```

### Step 1 — Prove the path
```yaml
local_checks:
  - command: "python3 -m pytest -q tests/test_b.py"
```

## Findings

### Step 0 — old-code reproduction (base `abc`)

notes a worker appended after running step 0

### Step 1 — production path (commit `93de960e`)

more notes
"""


def test_v1_subplan_steps_come_only_from_the_steps_section():
    # Real gh-resolve masters (Oct 13): workers append '### Step N' notes under '## Findings';
    # they were imported as gate-less steps, and 4 of the 5 latest masters were rejected.
    p = plans.parse_v1_subplan(SUBPLAN_WITH_FINDINGS)
    assert [s.title for s in p.steps] == ["Add the regression", "Prove the path"]
    shell = [g for g in p.steps[0].gates if g.adapter == "shell"]
    assert len(shell) == 1 and shell[0].declares_tests is False  # an exit-code check
    assert plans.check(plans.BatchSpec(plans=[p])) == []


def test_v1_step_uses_its_last_parseable_local_checks_block():
    # gh-resolve 2026-10-02f: a step carries a broken block (unescaped quotes) and then a
    # corrected copy. Both HEAD and V1-import crashed on the first; use the last good block.
    text = (
        "---\nplan: up\n---\n\n## Steps\n\n### Step 0: pins\n"
        '```yaml\nlocal_checks:\n  - command: "python3 -c "import sys""\n```\n'
        "Corrected:\n"
        '```yaml\nlocal_checks:\n  - command: "python3 -c \\"import sys\\""\n    timeout: 120\n```\n')
    p = plans.parse_v1_subplan(text)
    assert [g.command for g in p.steps[0].gates] == [["python3", "-c", "import sys"]]
    assert p.steps[0].gates[0].cap_s == 120


def test_v1_local_checks_in_every_yaml_scalar_style_with_env_assignments():
    # 748 of gh-resolve's gate-less steps used unquoted YAML commands with a leading
    # PYTHONPATH=tools; an assignment as argv[0] would not even execute.
    text = (
        "---\nplan: s\n---\n\n## Steps\n\n### Step 0 — skeleton\n"
        "```yaml\nlocal_checks:\n"
        "  - command: PYTHONPATH=tools python3 -m pytest -q tests/test_config.py\n    timeout: 120\n"
        "  - command: bash -o pipefail -c 'jq -e \"length >= 5\" f.json'\n"
        "  - command: 'python3 -c \"print(1)\"'\n"
        "```\n")
    g = plans.parse_v1_subplan(text).steps[0].gates
    assert g[0].adapter == "pytest" and g[0].command == ["python3", "-m", "pytest", "-q", "tests/test_config.py"]
    assert g[0].env == {"PYTHONPATH": "tools"} and g[0].cap_s == 120
    assert g[1].adapter == "shell" and g[1].command[:4] == ["bash", "-o", "pipefail", "-c"]
    assert g[2].command == ["python3", "-c", 'print(1)']
