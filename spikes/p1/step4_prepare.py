"""P1 step 4 preparation: four real V1 steps, their gates, and both briefs (Q10).

Usage: uv run python spikes/p1/step4_prepare.py
Reads V1 sub-plans from ~/.ilk-data read-only and uses a GitHub clone of ilk-skills in
~/.ilk2/spike-p1/v1-src (never the live V1 clone). Writes ~/.ilk2/spike-p1/step4/steps.json.

Each chosen step is a V1 "fix" step: step 0 pinned red tests (xfail), step 1 makes them pass.
Acceptance is deterministic and the same for both briefs.

Judgment calls:
- The 4 steps span sizes and areas: one Python file small and medium, Python across three
  skills, and shell. Wrong if all four are trivial or all huge for MiMo; the data will show.
- The plan's "Findings" section is removed from both briefs: the files are their final
  versions, so step 1's own findings would leak the answer. Frontmatter is reset to step 1.
- Preload: whole files of at most 600 lines; larger files get +-30-line windows around
  identifiers the plan names; 150k characters in total. Wrong if the preloaded arm keeps
  opening files outside what was preloaded.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

V1_PLANS = Path.home() / ".ilk-data/projects/users-chad-projects-github-inluck-net-ilk-skills-604d727/plans"
SRC = Path.home() / ".ilk2/spike-p1/v1-src"
OUT = Path.home() / ".ilk2/spike-p1/step4"

STEPS = [
    ("S1", "71e72ee4", "a-stale-postmortem-blacklists-nothing"),
    ("S2", "bb87e2d7", "triage-reads-the-newest-record"),
    ("S3", "300da01e", "both-rerun-arms-run-serially"),
    # S4 was 13919eff (the-scheduler-measures-before-the-train): its pinned
    # test_missing_baseline_spawns_measure fails at the real V1 commit under the gate
    # environment (preflight-S4, first run), so it cannot be judged here. Replaced by
    # another shell step.
    # d996894e itself leaves pinned test_launch_writes_run_local_checks red (launch.sh writes
    # Python `true`); V1 fixed it in the next commit, 3d1823e7, which is the green reference.
    ("S4", "d996894e", "a-relaunch-keeps-its-gates", "3d1823e7"),
]

WHOLE_FILE_MAX_LINES = 600
WINDOW = 30
PRELOAD_CAP = 150_000


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(SRC), *args], capture_output=True, text=True,
                          check=True).stdout


def plan_file(slug: str) -> Path:
    hits = sorted(V1_PLANS.glob(f"*-{slug}.md"))
    assert len(hits) == 1, (slug, hits)
    return hits[0]


def clean_plan(text: str) -> str:
    text = re.sub(r"(?m)^status: .*$", "status: in-progress", text, count=1)
    text = re.sub(r"(?m)^current_step: .*$", "current_step: 1", text, count=1)
    return re.sub(r"(?ms)^## Findings\n.*?(?=^## |\Z)", "", text)


def step1_commands(text: str) -> list[str]:
    sec = re.search(r"(?ms)^### Step 1\b.*?(?=^### |^## |\Z)", text).group(0)
    return re.findall(r'-\s*command:\s*"((?:[^"\\]|\\.)*)"', sec)


def scope_paths(text: str) -> list[str]:
    m = re.search(r"(?m)^scope_paths: (\[.*\])$", text)
    return json.loads(m.group(1)) if m else []


def step0_commit(slug: str, base: str) -> str | None:
    out = git("log", "--format=%h %s", base, "-n", "80")
    for line in out.splitlines():
        if f"[plan:{slug}#step-0]" in line:
            return line.split()[0]
    return None


def windows(lines: list[str], idents: set[str]) -> list[tuple[int, int]]:
    hits = [i for i, l in enumerate(lines) if any(x in l for x in idents)]
    spans: list[tuple[int, int]] = []
    for i in hits:
        a, b = max(0, i - WINDOW), min(len(lines), i + WINDOW + 1)
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))
    return spans


def preload(base: str, plan: str, scope: list[str], pinned: str, step0: str | None) -> str:
    idents = {m for m in re.findall(r"`([A-Za-z_][A-Za-z0-9_.]{3,})`", plan)
              if not m.endswith((".py", ".sh", ".md", ".json"))}
    idents |= {m.split(".")[-1] for m in idents if "." in m}
    parts = ["# Preloaded context (assembled by the driver at no model cost)\n"]
    files = [pinned] + [p for p in scope if p != pinned]
    for path in files:
        try:
            body = git("show", f"{base}:{path}")
        except subprocess.CalledProcessError:
            parts.append(f"\n## {path}\n(does not exist at the base commit)\n")
            continue
        lines = body.splitlines()
        if len(lines) <= WHOLE_FILE_MAX_LINES or path == pinned:
            parts.append(f"\n## {path} (whole file, {len(lines)} lines)\n```\n{body}\n```\n")
        else:
            spans = windows(lines, idents)
            parts.append(f"\n## {path} ({len(lines)} lines; windows around names the plan uses)\n")
            for a, b in spans:
                numbered = "\n".join(f"{i + 1}: {lines[i]}" for i in range(a, b))
                parts.append(f"```\n{numbered}\n```\n")
    if step0:
        parts.append(f"\n## Diff of step 0 ({step0})\n```diff\n{git('show', '--format=', step0)}\n```\n")
    text = "".join(parts)
    if len(text) > PRELOAD_CAP:
        text = text[:PRELOAD_CAP] + "\n[preload truncated at the cap]\n"
    return text


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    steps = []
    for sid, commit, slug, *ref in STEPS:
        base = git("rev-parse", "--short=8", f"{commit}^").strip()
        raw = plan_file(slug).read_text()
        plan = clean_plan(raw)
        cmds = step1_commands(raw)
        pinned = cmds[0].split()[3]
        step0 = step0_commit(slug, base)
        scope = scope_paths(raw)
        pre = preload(base, plan, scope, pinned, step0)
        steps.append({
            "id": sid, "slug": slug, "commit": commit, "reference": ref[0] if ref else commit,
            "base": base, "step0": step0,
            "plan_file": plan_file(slug).name, "plan": plan, "commands": cmds,
            "pinned": pinned, "scope_paths": scope, "preload": pre,
        })
        print(sid, slug, "base", base, "step0", step0, "cmds", len(cmds), "pinned", pinned,
              "plan_chars", len(plan), "preload_chars", len(pre))
    (OUT / "steps.json").write_text(json.dumps(steps, indent=2))


if __name__ == "__main__":
    main()
