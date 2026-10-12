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
