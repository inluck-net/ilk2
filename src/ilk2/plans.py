"""Batch specifications: ilk2 JSON, V1 Markdown import, and the submit checks (docs/02 §2.11.2, Q2).

A batch is plans of steps. Each step declares its gates. The normalized spec is snapshotted
in the store at submission and never edited afterwards (Q3); amendments are journalled.

ilk2 JSON shape:
{
  "plans": [{"slug": "a", "depends_on": [], "verification": false,
             "steps": [{"title": "...", "instructions": "...", "scope": ["src/"],
                        "gates": [{"name": "unit", "adapter": "pytest",
                                   "command": ["python3", "-m", "pytest", "tests/x.py"],
                                   "cwd": ".", "cap_s": 600, "declares_tests": true}],
                        "expect_red": ["tests/x.py::test_new"]}]}]
}
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

ADAPTERS = {"pytest", "vitest", "shell", "script"}  # "script": a package script not yet resolved
TEST_ADAPTERS = {"pytest", "vitest"}
DEFAULT_GATE_CAP_S = 600


class SpecError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class Gate:
    name: str
    adapter: str
    command: list[str]
    cwd: str = "."
    cap_s: int = DEFAULT_GATE_CAP_S
    declares_tests: bool = True
    script: str | None = None          # package script name the command was resolved from
    script_entry: str | None = None    # its package.json entry: the only part of it hashed
    env: dict[str, str] = field(default_factory=dict)  # the script's leading VAR=value (e.g. TZ=UTC)


@dataclass
class Step:
    title: str
    instructions: str
    gates: list[Gate]
    scope: list[str] = field(default_factory=list)
    expect_red: list[str] = field(default_factory=list)
    # Tests that must pass after this step even if they failed at the batch base. Without it a
    # base-red test that stays red is not a new failure, so "make X pass" was inexpressible
    # (found in P2; docs/06).
    must_pass: list[str] = field(default_factory=list)


@dataclass
class Plan:
    slug: str
    steps: list[Step]
    depends_on: list[str] = field(default_factory=list)
    verification: bool = False


@dataclass
class BatchSpec:
    plans: list[Plan]
    # The planner's documents the steps refer to (V1: the MASTER and its sub-plans), by name.
    # The driver gives each attempt a readable copy; workers cannot read the planner's dirs.
    documents: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def adapter_for(command: list[str]) -> str:
    """Resolve an adapter from a command. Package scripts are resolved by gates.resolve()."""
    joined = " ".join(command)
    if command and Path(command[0]).name in ("npm", "bun", "pnpm", "yarn"):
        return "script"
    if re.search(r"(^|[\s/])pytest(\s|$)|-m\s+pytest", joined):
        return "pytest"
    if re.search(r"(^|[\s/])vitest(\s|$)", joined):
        return "vitest"
    return "shell"


def _gate(d: dict, i: int) -> Gate:
    cmd = d["command"]
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    return Gate(name=d.get("name") or f"gate-{i + 1}", adapter=d.get("adapter") or adapter_for(cmd),
                command=list(cmd), cwd=d.get("cwd", "."),
                cap_s=int(d.get("cap_s", DEFAULT_GATE_CAP_S)),
                declares_tests=bool(d.get("declares_tests", True)),
                script=d.get("script"), script_entry=d.get("script_entry"),
                env=dict(d.get("env") or {}))


def from_dict(d: dict) -> BatchSpec:
    plans = []
    for p in d["plans"]:
        steps = [Step(title=s.get("title", f"step {j}"), instructions=s["instructions"],
                      gates=[_gate(g, i) for i, g in enumerate(s.get("gates", []))],
                      scope=list(s.get("scope", [])), expect_red=list(s.get("expect_red", [])),
                      must_pass=list(s.get("must_pass", [])))
                 for j, s in enumerate(p["steps"])]
        plans.append(Plan(slug=p["slug"], steps=steps, depends_on=list(p.get("depends_on", [])),
                          verification=bool(p.get("verification", False))))
    return BatchSpec(plans=plans, documents=dict(d.get("documents") or {}))


def load_json(path: Path) -> BatchSpec:
    return from_dict(json.loads(path.read_text()))


# -- V1 Markdown import -------------------------------------------------------------------

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_STEP = re.compile(r"(?m)^### Step (\d+)\s*[—:-]+\s*(.*)$")
# A local_checks entry: `- command: <scalar>` with an optional `timeout:` line. The scalar is
# double-quoted, single-quoted or plain (to the end of the line): gh-resolve's plans use all three.
_CMD = re.compile(r"""-\s*command:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^']|'')*)'|([^\n]+?))[ \t]*$"""
                  r"""(?:\s*\n\s*timeout:\s*(\d+))?""", re.M)
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _frontmatter(text: str) -> dict:
    m = _FRONT.match(text)
    out: dict = {}
    if not m:
        return out
    key = None
    for line in m.group(1).splitlines():
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item and key is not None:  # YAML block list under the previous `key:`
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(item.group(1).strip().strip("\"'"))
            continue
        if ":" not in line or line.startswith(" "):
            continue
        k, v = line.split(":", 1)
        key = k.strip()
        v = v.strip()
        if v.startswith(("[", "{")):
            try:
                out[k.strip()] = json.loads(v)
                continue
            except json.JSONDecodeError:
                pass
        out[k.strip()] = v
    return out


def _block_gates(block: str) -> list[Gate] | None:
    """Gates of one local_checks block, or None if any command does not split (bad quoting)."""
    gates = []
    for j, (dq, sq, plain, tmo) in enumerate(_CMD.findall(block)):
        c = dq.replace('\\"', '"').replace("\\\\", "\\") if dq else sq.replace("''", "'") if sq else plain
        try:
            cmd = shlex.split(c)
        except ValueError:
            return None
        env = {}
        while cmd and _ASSIGN.match(cmd[0]):  # leading VAR=value: the gate's env, not argv[0]
            k, _, v = cmd.pop(0).partition("=")
            env[k] = v
        if not cmd:
            return None
        adapter = adapter_for(cmd)
        # A V1 local check that is not a test runner is an exit-code acceptance check
        gates.append(Gate(name=f"local-check-{j + 1}", adapter=adapter, command=cmd,
                          cap_s=int(tmo) if tmo else DEFAULT_GATE_CAP_S,
                          declares_tests=adapter != "shell", env=env))
    return gates


def _local_checks(sec: str) -> list[Gate]:
    """A step's gates from its last local_checks block whose commands all parse. Some V1 plans
    keep a broken block (unescaped quotes) followed by a corrected copy (gh-resolve 2026-10-02f);
    the importer must neither crash on the first nor run both."""
    blocks = re.split(r"(?m)^\s*local_checks:\s*$", sec)[1:]
    good = [g for g in (_block_gates(b.split("```")[0]) for b in blocks) if g is not None]
    return good[-1] if good else []


def parse_v1_subplan(text: str, slug: str | None = None) -> Plan:
    """A V1 sub-plan: frontmatter, then '### Step N — title' sections with local_checks."""
    fm = _frontmatter(text)
    slug = slug or fm.get("plan") or "plan"
    body = _FRONT.sub("", text, count=1)
    # Steps live in the '## Steps' section. Workers append '### Step N' notes under later
    # sections ('## Findings'); those are not steps (real gh-resolve masters, Oct 13).
    m = re.search(r"(?m)^## Steps\s*$", body)
    if m:
        end = re.search(r"(?m)^## ", body[m.end():])
        steps_end = m.end() + end.start() if end else len(body)
        heads = [h for h in _STEP.finditer(body) if m.end() <= h.start() < steps_end]
    else:
        heads = list(_STEP.finditer(body))
    preamble = body[: heads[0].start()] if heads else body
    steps = []
    for i, h in enumerate(heads):
        sec = body[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        sec = re.split(r"(?m)^## ", sec)[0]
        gates = _local_checks(sec)
        # "Remove every xfail" (all of the pinned file's tests must now pass) -> must_pass on the
        # first local_checks path. A partial "Remove AC-1's xfail" needs ids; left to the author.
        must = []
        if re.search(r"remove (every|all)( the)? xfails?", sec, re.I) and gates:
            first = next((a for a in gates[0].command[1:] if a.endswith(".py") and "/" in a), None)
            if first:
                must = [first]
        steps.append(Step(must_pass=must, title=h.group(2).strip(),
                          instructions=(preamble.strip() + "\n\n### " + h.group(0)[4:] + sec).strip(),
                          gates=gates, scope=list(fm.get("scope_paths") or [])))
    deps = fm.get("depends_on") or []
    return Plan(slug=slug, steps=steps, depends_on=list(deps) if isinstance(deps, list) else [],
                verification=str(fm.get("plan", "")).endswith("-verify"))


def parse_v1_master(master: Path) -> BatchSpec:
    """A V1 MASTER file: its linked sub-plan files, in the order they are linked."""
    text = master.read_text()
    seen, plans = set(), []
    for name in re.findall(r"\]\(\./?([^)\s]+\.md)\)", text):
        if name in seen or name.startswith("MASTER-"):
            continue
        seen.add(name)
        sub = master.parent / name
        if sub.exists():
            plans.append(parse_v1_subplan(sub.read_text()))
    return BatchSpec(plans=plans, documents=v1_documents(master))


def v1_documents(master: Path) -> dict[str, str]:
    """A V1 MASTER and the sub-plans it links, by the names the plans use to refer to each
    other (a claimed master keeps its original name). gh-resolve P5 batch, Oct 13: every
    attempt spent 1-2 minutes searching the disk for the MASTER its sub-plan says to read."""
    master = Path(master)
    try:
        text = master.read_text()
    except OSError:
        return {}
    docs = {master.name.split(".md")[0] + ".md": text}
    for name in re.findall(r"\]\(\./?([^)\s]+\.md)\)", text):
        sub = master.parent / name
        if name not in docs and sub.is_file():
            docs[name] = sub.read_text()
    return docs


# -- submit checks (§2.11.2) --------------------------------------------------------------

def _merge(a: "Step", b: "Step", keep_reds: bool) -> "Step":
    gates, seen = [], set()
    for g in a.gates + b.gates:
        key = (g.adapter, tuple(g.command), g.cwd)
        if key not in seen:
            seen.add(key)
            gates.append(g)
    return Step(title=b.title if not keep_reds else a.title,
                instructions=f"{a.instructions}\n\n--- then, in the same step: {b.title} ---\n\n"
                             f"{b.instructions}",
                gates=gates, expect_red=list(a.expect_red if keep_reds else b.expect_red),
                scope=sorted(set(a.scope) | set(b.scope)),
                must_pass=list(dict.fromkeys(a.must_pass + b.must_pass)))


def merge_v1_ceremony(spec: BatchSpec) -> BatchSpec:
    """V1's red-first pins / implement / neighbour gate pattern is one piece of work in three
    steps, and each step starts a session that re-explores the repo (Oct 13: 19 of 27.5 min of a
    gh-resolve step before its first edit). ilk2's must_pass, base comparison and attribution
    cover what the extra steps were for, so a 'Red-first…' step folds into the next one and a
    trailing 'Neighbour gate…' step into the one before. The red-first step's expected reds
    are dropped: the merged step must turn them green. Verification plans are left alone.
    Judgment call: wrong if merged steps hit the attempt cap; then the planner should split."""
    for plan in spec.plans:
        if plan.verification or len(plan.steps) < 2:
            continue
        steps = list(plan.steps)
        out: list[Step] = []
        i = 0
        while i < len(steps):
            s = steps[i]
            if s.title.lower().startswith("red-first") and i + 1 < len(steps):
                out.append(_merge(s, steps[i + 1], keep_reds=False))
                i += 2
                continue
            if s.title.lower().startswith("neighbour gate") and out:
                out[-1] = _merge(out[-1], s, keep_reds=True)
                i += 1
                continue
            out.append(s)
            i += 1
        plan.steps = out
    return spec


def check(spec: BatchSpec, root: Path | None = None, path_dirs: list[str] | None = None) -> list[str]:
    """Every reason to reject the batch at submit, or [] if it may be queued."""
    problems: list[str] = []
    slugs = [p.slug for p in spec.plans]
    if not spec.plans:
        problems.append("batch has no plans")
    dup = {s for s in slugs if slugs.count(s) > 1}
    if dup:
        problems.append(f"duplicate plan slugs: {sorted(dup)}")
    for p in spec.plans:
        if not p.steps:
            problems.append(f"plan {p.slug}: no steps")
        for d in p.depends_on:
            if d not in slugs:
                problems.append(f"plan {p.slug}: depends on missing plan {d}")
        for i, s in enumerate(p.steps):
            where = f"plan {p.slug} step {i}"
            if not s.gates:
                problems.append(f"{where}: declares no gate (vacuous)")
            for g in s.gates:
                if g.adapter not in ADAPTERS:
                    problems.append(f"{where}: gate {g.name}: unknown adapter {g.adapter}")
                if g.adapter == "script":
                    problems.append(f"{where}: gate {g.name}: package script {' '.join(g.command)!r} "
                                    "does not resolve to a runner")
                if g.declares_tests and g.adapter == "shell":
                    problems.append(f"{where}: gate {g.name}: a test gate cannot use the shell "
                                    "adapter (no test ids; set declares_tests false for lint/build)")
                if not g.command:
                    problems.append(f"{where}: gate {g.name}: empty command")
                elif path_dirs is not None and root is not None:
                    # vitest runs from the project's node_modules, which a setup hook may only
                    # create later; what must be on the pinned PATH is its runtime.
                    exe = "node" if g.adapter == "vitest" else g.command[0]
                    found = (root / g.cwd / exe).exists() if "/" in exe else \
                        shutil.which(exe, path=":".join(path_dirs)) is not None
                    if not found:
                        problems.append(f"{where}: gate {g.name}: executable {exe!r} not on the "
                                        "pinned PATH")
    problems += _cycles(spec)
    if root is not None:
        if not root.exists():
            problems.append(f"execution root {root} does not exist")
        elif not (root / ".git").exists():
            problems.append(f"execution root {root} is not a git worktree")
    return problems


def _cycles(spec: BatchSpec) -> list[str]:
    graph = {p.slug: [d for d in p.depends_on] for p in spec.plans}
    state: dict[str, int] = {}
    out: list[str] = []

    def visit(n: str, path: list[str]) -> None:
        state[n] = 1
        for m in graph.get(n, []):
            if state.get(m) == 1:
                out.append("dependency cycle: " + " -> ".join(path + [n, m]))
            elif m in graph and state.get(m) is None:
                visit(m, path + [n])
        state[n] = 2

    for n in graph:
        if state.get(n) is None:
            visit(n, [])
    return out
