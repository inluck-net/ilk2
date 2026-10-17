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


def test_v1_ceremony_steps_merge_into_one(ilk):
    # Oct 13: V1's red-first pins / implement / neighbour gate pattern made one piece of work
    # three steps, each re-exploring the repo. ilk2's must_pass, base comparison and
    # attribution cover what the extra steps were for.
    from ilk2 import plans as P
    g_a = {"name": "a", "adapter": "pytest", "command": ["python3", "-m", "pytest", "-q", "tests/test_a.py"]}
    g_n = {"name": "n", "adapter": "pytest", "command": ["python3", "-m", "pytest", "-q", "tests/test_a.py", "tests/test_b.py"]}
    spec = P.from_dict({"plans": [
        {"slug": "work", "steps": [
            {"title": "Red-first pins", "instructions": "write the pins", "gates": [g_a],
             "expect_red": ["tests/test_a.py::test_new"]},
            {"title": "Implement; remove every xfail", "instructions": "implement it", "gates": [g_a],
             "must_pass": ["tests/test_a.py"]},
            {"title": "Neighbour gate", "instructions": "run the neighbours", "gates": [g_n]}]},
        {"slug": "x-batch-verify", "verification": True, "steps": [
            {"title": "Red-first pins", "instructions": "never merged", "gates": [g_a]},
            {"title": "Run the suite", "instructions": "verify", "gates": [g_a]}]}]})
    P.merge_v1_ceremony(spec)
    work = spec.plans[0].steps
    assert len(work) == 1
    s = work[0]
    assert all(x in s.instructions for x in ("write the pins", "implement it", "run the neighbours"))
    assert [g.name for g in s.gates] == ["a", "n"] and s.must_pass == ["tests/test_a.py"]
    assert s.expect_red == []
    assert len(spec.plans[1].steps) == 2  # a verification plan is left alone


def test_ceremony_merge_drops_gates_that_need_the_folded_pins_commit():
    # gh-resolve P5 batch, Oct 13: the implement step's gate looked up the pins commit
    # (plan:<slug>#step-0). Merged, that commit is the step itself, made after its gates pass.
    from ilk2 import plans as P
    g_a = {"name": "a", "adapter": "pytest", "command": ["python3", "-m", "pytest", "-q", "tests/test_a.py"]}
    pins = {"name": "pins", "command": ["bash", "-c", 'b=$(git log -1 --format=%H --grep="plan:work#step-0"); '
                                        'test -n "$b"; python3 scripts/pins_only_lose_xfail.py "$b" tests/test_a.py']}
    other = {"name": "o", "command": ["bash", "-c", 'git log --grep="plan:work#step-01"; true']}
    spec = P.from_dict({"plans": [{"slug": "work", "steps": [
        {"title": "Red-first pins", "instructions": "pins", "gates": [g_a]},
        {"title": "Implement", "instructions": "impl", "gates": [g_a, pins, other]},
        {"title": "Remove every xfail", "instructions": "rest", "gates": [g_a, pins]}]}]})
    P.merge_v1_ceremony(spec)
    s0, s1 = spec.plans[0].steps
    assert [g.name for g in s0.gates] == ["a", "o"]
    assert [g.name for g in s1.gates] == ["a", "pins"]  # a later step checks the merged commit


def test_a_json_gate_command_with_a_leading_assignment_gets_it_as_env():
    # gh-resolve renders kira gates as `TZ=UTC bun run …`; from_dict ran `TZ=UTC` as argv[0].
    from ilk2 import plans as P
    spec = P.from_dict({"plans": [{"slug": "p", "steps": [{"instructions": "x", "gates": [
        {"name": "cli", "command": "TZ=UTC NODE_ENV=test bun run test:cli"},
        {"name": "list", "command": ["TZ=UTC", "bun", "test"], "env": {"TZ": "Asia/Singapore"}}]}]}]})
    g1, g2 = spec.plans[0].steps[0].gates
    assert g1.command == ["bun", "run", "test:cli"] and g1.env == {"TZ": "UTC", "NODE_ENV": "test"}
    assert g2.command == ["bun", "test"] and g2.env == {"TZ": "Asia/Singapore"}  # explicit env wins


GHR_LIVE_WORK = """---
plan: issue-1-work-abc
status: pending
depends_on: []
local_checks: 
  - command: bash scripts/consistency-regression.sh
    timeout: 600
  - command: python -m pytest tests/test_a.py
    timeout: 900
scope_paths:
  - "src/a.py"
---

# Sub-plan

## Steps

### Step 0

resolve issue #1.

## Gate

- `bash scripts/consistency-regression.sh`
"""

GHR_LIVE_VERIFY = """---
plan: issue-1-batch-verification-abc
status: pending
batch_verification: true
depends_on: []
local_checks:
  - command: python -m pytest tests/test_a.py
scope_paths:
  - "src/a.py"
---

## Steps

### Step 0 — run the batch suite

Run it.

### Step 1 — re-run the failures at base, and attribute from the result

Rerun.
"""


def test_gh_resolve_live_render_bare_steps_and_frontmatter_gates():
    # gh-resolve's first live dispatch to ilk2 (rezmac, Oct 14, run 00e65dcf) was rejected:
    # "no steps" for a bare '### Step 0', and "declares no gate (vacuous)" for steps whose gates
    # are the sub-plan frontmatter's local_checks. V1 accepts both.
    work = plans.parse_v1_subplan(GHR_LIVE_WORK)
    assert [s.title for s in work.steps] == ["Step 0"] and not work.verification
    g = work.steps[0].gates
    assert [(x.adapter, x.command, x.cap_s) for x in g] == [
        ("shell", ["bash", "scripts/consistency-regression.sh"], 600),
        ("pytest", ["python", "-m", "pytest", "tests/test_a.py"], 900)]
    verify = plans.parse_v1_subplan(GHR_LIVE_VERIFY)
    assert verify.verification                              # batch_verification: true
    assert [s.title for s in verify.steps] == ["run the batch suite",
                                               "re-run the failures at base, and attribute from the result"]
    assert all(s.gates and s.gates[0].command[-1] == "tests/test_a.py" for s in verify.steps)
    assert plans.check(plans.BatchSpec(plans=[work, verify])) == []


def test_a_steps_own_local_checks_win_over_the_frontmatters():
    text = GHR_LIVE_WORK.replace("resolve issue #1.", "resolve issue #1.\n\n```yaml\nlocal_checks:\n"
                                 "  - command: python -m pytest tests/test_b.py\n```\n")
    step = plans.parse_v1_subplan(text).steps[0]
    assert [x.command[-1] for x in step.gates] == ["tests/test_b.py"]


def test_a_v1_script_gate_that_is_not_a_test_runner_is_an_exit_code_check(ilk):
    # gh-resolve on rezmac kira-live, Oct 14, run a564c9f5: `bun run typecheck:convex` resolved
    # to tsc, and the submit was refused as a shell *test* gate. A V1 local check is its exit code.
    import json as _json
    from ilk2 import batches, gitops
    repo = ilk.make_repo({"package.json": _json.dumps({"scripts": {"typecheck": "tsc --noEmit",
                                                                   "test": "vitest run"}}),
                          "a.ts": "export const a = 1\n"})
    text = GHR_LIVE_WORK.replace("  - command: python -m pytest tests/test_a.py\n    timeout: 900\n",
                                 "  - command: bun run typecheck\n    timeout: 900\n")
    spec = plans.BatchSpec(plans=[plans.parse_v1_subplan(text)])
    batches.resolve_scripts(spec, repo, gitops.rev(repo, "HEAD"))
    g = spec.plans[0].steps[0].gates[1]
    assert (g.adapter, g.command, g.declares_tests) == ("shell", ["bun", "run", "typecheck"], False)
    assert plans.check(spec) == []


def test_submit_check_validates_and_creates_nothing(ilk, capsys, tmp_path):
    # Oct 14: two live gh-resolve dispatches on rezmac were rejected one after the other; a
    # consumer can now validate its rendered plans first.
    import json as _json
    from ilk2 import cli
    ilk.make_repo()
    ilk.project()
    good = tmp_path / "MASTER-ok.md"
    sub = tmp_path / "sub.md"
    sub.write_text(GHR_LIVE_WORK.replace("bash scripts/consistency-regression.sh", "true"))
    good.write_text("- [sub](./sub.md)\n")
    capsys.readouterr()
    assert cli.main(["submit", str(good), "--project", "proj", "--check"]) == 0
    out = _json.loads(capsys.readouterr().out)
    assert (out["ok"], out["plans"], out["steps"]) == (True, 1, 1)
    assert ilk.s.one("SELECT COUNT(*) n FROM batches")["n"] == 0
    sub.write_text(GHR_LIVE_WORK.replace("### Step 0", "no steps here"))
    assert cli.main(["submit", str(good), "--project", "proj", "--check"]) == 2
    assert "no steps" in capsys.readouterr().out


# -- structural refusals ported from V1's preflight (docs/07 §7.4.1) ------------------------

SUB_OK = """---
plan: {slug}
---

## Steps

### Step 0 — do it
```yaml
local_checks:
  - command: python -m pytest -q tests/test_calc.py
```
"""


def _master(tmp_path: Path, body: str, subs: dict[str, str]) -> Path:
    for name, slug in subs.items():
        (tmp_path / name).write_text(SUB_OK.format(slug=slug))
    m = tmp_path / "MASTER-2026-10-17-execution-plan.md"
    m.write_text("---\ntitle: t\nstatus: queued\n---\n\n" + body)
    return m


def test_a_linked_subplan_that_is_missing_is_refused_not_dropped(tmp_path):
    m = _master(tmp_path, "| # | Sub-plan |\n|---|---|\n| 1 | [a](./2026-10-17-a.md) |\n"
                          "| 2 | [b](./2026-10-17-b.md) |\n\nSee [the template](./<name>.md).\n",
                {"2026-10-17-a.md": "a"})
    spec = plans.parse_v1_master(m)
    assert [p.slug for p in spec.plans] == ["a"]
    probs = plans.check(spec)
    assert any("2026-10-17-b.md, which does not exist" in p for p in probs)
    assert not any("<name>" in p for p in probs)          # a prose placeholder is not a sub-plan


def test_the_registry_and_the_links_must_agree(tmp_path):
    body = ("## Sub-plan registry\n\n| # | Sub-plan | File |\n|---|---|---|\n"
            "| 1 | a | `2026-10-17-a.md` |\n| 2 | b | `2026-10-17-b.md` |\n\n"
            "## Order\n\n[a](./2026-10-17-a.md)\n")
    m = _master(tmp_path, body, {"2026-10-17-a.md": "a", "2026-10-17-b.md": "b"})
    probs = plans.check(plans.parse_v1_master(m))
    assert any("registry lists 2026-10-17-b.md, which the MASTER does not link" in p for p in probs)
    body_ok = body.replace("[a](./2026-10-17-a.md)", "[a](./2026-10-17-a.md) [b](./2026-10-17-b.md)")
    assert plans.check(plans.parse_v1_master(_master(tmp_path, body_ok, {}))) == []


def test_a_step_whose_local_checks_cannot_be_parsed_is_refused():
    # Before: the step silently ran the frontmatter's plan-wide checks instead of its own.
    text = ("---\nplan: p\nlocal_checks:\n  - command: python -m pytest -q tests/test_calc.py\n---\n\n"
            "## Steps\n\n### Step 0 — bad quoting\n```yaml\nlocal_checks:\n"
            "  - command: \"python -m pytest -q tests/x.py\n```\n\n"
            "### Step 1 — json fence\n```json\n{\"local_checks\": [{\"command\": \"pytest\"}]}\n```\n\n"
            "### Step 2 — none declared\nUses the plan-wide checks.\n")
    p = plans.parse_v1_subplan(text)
    probs = plans.check(plans.BatchSpec(plans=[p]))
    assert sum("cannot be parsed" in x for x in probs) == 2
    assert any("step 0:" in x for x in probs) and any("step 1:" in x for x in probs)
    assert not any("step 2:" in x for x in probs)


def test_the_snapshot_does_not_carry_import_problems(tmp_path):
    m = _master(tmp_path, "[a](./2026-10-17-a.md) [b](./2026-10-17-b.md)\n", {"2026-10-17-a.md": "a"})
    d = plans.parse_v1_master(m).to_dict()
    assert "problems" not in d and all("problems" not in p for p in d["plans"])


def test_a_write_bullet_under_a_denied_path_is_refused(ilk):
    # Replaces V1's safety-kernel admission: declared writes, not only the scope.
    import pytest
    from ilk2 import batches
    from conftest import one_step
    ilk.make_repo()
    ilk.project(deny_paths=["migrations/"])
    with pytest.raises(batches.SubmitRejected, match="denied paths"):
        ilk.submit(one_step(instructions="1. **Write** `migrations/0003.py`: the new table"))


# -- plan-shape warnings (docs/07 §7.4.1) --------------------------------------------------

def _plans(*specs) -> plans.BatchSpec:
    """specs: (slug, depends_on, scope, instructions)."""
    g = [{"name": "unit", "command": "python3 -m pytest tests/a.py"}]
    return plans.from_dict({"plans": [
        {"slug": s, "depends_on": d, "steps": [{"title": "t", "instructions": ins, "scope": sc, "gates": g}]}
        for s, d, sc, ins in specs]})


def test_size_warnings_are_per_plan_per_step_and_fire_past_their_limits_only():
    from ilk2 import batches
    ok = _plans(("a", [], [f"f{i}.py" for i in range(6)], "x" * 12_000))
    assert batches.plan_warnings(ok) == []
    big = _plans(("a", [], [f"f{i}.py" for i in range(7)], "x" * 12_001))
    assert {(w["kind"], w["value"]) for w in batches.plan_warnings(big)} == {
        ("files-per-step", 7), ("chars-per-step", 12_001)}


def test_a_v1_plan_split_into_small_steps_does_not_warn():
    # 17e's shape: one 36k-char sub-plan with 11 files of plan-level scope, split into 7 steps.
    # Each step repeats the preamble and scope; per step the plan is small.
    pre = "## Objectives\n" + "o" * 34_000 + "\n\n"
    text = "---\nplan: p\nscope_paths:\n" + "".join(f"  - f{i}.py\n" for i in range(11)) + "---\n\n" + pre + \
        "## Steps\n\n" + "".join(f"### Step {i} — part {i}\n```yaml\nlocal_checks:\n"
                                   f"  - command: python -m pytest -q tests/t{i}.py\n```\n" + "s" * 1_000 + "\n\n"
                                   for i in range(7))
    p = plans.parse_v1_subplan(text)
    assert len(p.steps) == 7 and len(p.steps[3].instructions) > 34_000 and len(p.steps[3].scope) == 11
    from ilk2 import batches
    assert batches.plan_warnings(plans.BatchSpec(plans=[p])) == []
    one = plans.parse_v1_subplan(text.replace("### Step", "#### Sub", 6).replace("#### Sub", "### Note"))
    assert {w["kind"] for w in batches.plan_warnings(plans.BatchSpec(plans=[one]))} == {
        "files-per-step", "chars-per-step"}          # the same sub-plan as a single step warns


def test_serial_chain_counts_transitive_chains_and_needs_a_disjoint_pair():
    from ilk2 import batches
    # B→A, C→B: a total order by transitivity; C's scope is disjoint from A's and B's
    chain = _plans(("a", [], ["x.py"], "i"), ("b", ["a"], ["x.py"], "i"), ("c", ["b"], ["y.py"], "i"))
    assert batches.serial_chain(chain) == [["a", "c"], ["b", "c"]]
    # 17d's shape: every plan lists every earlier one, all sharing a file: ordered for a reason
    shared = _plans(("a", [], ["x.py"], "i"), ("b", ["a"], ["x.py", "y.py"], "i"),
                    ("c", ["a", "b"], ["x.py"], "i"))
    assert batches.serial_chain(shared) == []
    # c is unordered against a and b: lanes can already run it
    loose = _plans(("a", [], ["x.py"], "i"), ("b", ["a"], ["x.py"], "i"), ("c", [], ["y.py"], "i"))
    assert batches.serial_chain(loose) == []
    # a directory scope overlaps the files under it
    nested = _plans(("a", [], ["tools/"], "i"), ("b", ["a"], ["tools/x.py"], "i"))
    assert batches.serial_chain(nested) == []


def test_warnings_are_recorded_at_submit_and_never_refuse(ilk):
    ilk.make_repo()
    ilk.project()
    g = [{"name": "unit", "command": "python -m pytest -q tests/test_calc.py"}]
    bid = ilk.submit({"plans": [
        {"slug": "a", "steps": [{"title": "t", "instructions": "x", "scope": ["calc.py"], "gates": g}]},
        {"slug": "b", "depends_on": ["a"],
         "steps": [{"title": "t", "instructions": "y" * 12_001, "scope": ["other.py"], "gates": g}]}]})
    kinds = sorted(e["payload"]["kind"] for e in ilk.s.events_since(0, bid) if e["kind"] == "submit.warning")
    assert kinds == ["chars-per-step", "serial-chain"]
    assert ilk.s.get("batches", bid)["status"] == "queued"
