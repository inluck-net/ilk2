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
                script=d.get("script"), script_entry=d.get("script_entry"))


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
    return BatchSpec(plans=plans)


def load_json(path: Path) -> BatchSpec:
    return from_dict(json.loads(path.read_text()))


# -- V1 Markdown import -------------------------------------------------------------------

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_STEP = re.compile(r"(?m)^### Step (\d+)\s*[—:-]+\s*(.*)$")
_CMD = re.compile(r'-\s*command:\s*"((?:[^"\\]|\\.)*)"(?:\s*\n\s*timeout:\s*(\d+))?')


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


def parse_v1_subplan(text: str, slug: str | None = None) -> Plan:
    """A V1 sub-plan: frontmatter, then '### Step N — title' sections with local_checks."""
    fm = _frontmatter(text)
    slug = slug or fm.get("plan") or "plan"
    body = _FRONT.sub("", text, count=1)
    heads = list(_STEP.finditer(body))
    preamble = body[: heads[0].start()] if heads else body
    steps = []
    for i, h in enumerate(heads):
        sec = body[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(body)]
        sec = re.split(r"(?m)^## ", sec)[0]
        gates = [Gate(name=f"local-check-{j + 1}", adapter=adapter_for(shlex.split(c)),
                      command=shlex.split(c.replace('\\"', '"')),
                      cap_s=int(t) if t else DEFAULT_GATE_CAP_S)
                 for j, (c, t) in enumerate(_CMD.findall(sec))]
        steps.append(Step(title=h.group(2).strip(),
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
    return BatchSpec(plans=plans)


# -- submit checks (§2.11.2) --------------------------------------------------------------

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
