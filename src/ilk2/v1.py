"""Coexistence with V1 and the project handover (docs/04 §4.2-4.3).

One owner per repo root. V1 finds work by scanning ~/.ilk-data/projects/<key>/plans for runnable
MASTER-*.md and resolves the repo from runtime/launcher/last-launch.json, then its registry
(~/.ilk-data/projects.json); a project it cannot resolve is skipped (`skip-unresolved`,
ilk-skills scheduler_scan.py). The handover uses exactly that: the registry entry is removed
and last-launch.json is renamed aside, so V1's scheduler skips the project from then on.

/ilk-plan keeps writing masters into the V1 plans dir. For a project ilk2 owns, the supervisor
claims each queued master (an atomic rename V1's MASTER-*.md glob no longer matches) and
submits it. Every change to V1's files is a rename or a backed-up rewrite: reversible.
Reading V1's data was approved by Chad on Oct 13 ("you can read V1's registry").
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import clock

CLAIMED = ".ilk2-claimed"
HANDOVER = ".ilk2-handover"
MARKER = "ILK2-OWNED"


def data_root() -> Path:
    return Path(os.environ.get("ILK2_V1_DATA") or (Path.home() / ".ilk-data"))


def _real(p: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def registry_roots() -> set[str]:
    reg = data_root() / "projects.json"
    try:
        data = json.loads(reg.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return set()
    return {_real(e["path"]) for e in data.get("projects", []) if e.get("path")}


def _status(master: Path) -> str | None:
    try:
        text = master.read_text()
    except OSError:
        return None
    m = re.search(r"(?ms)\A---\s*\n.*?^status:\s*([\w-]+)", text)
    return m.group(1) if m else None


def find_key(root: str | Path) -> str | None:
    """The V1 data dir key whose last launch (or handover copy) names this repo. With several,
    the one holding the most masters."""
    root = _real(root)
    best, n_best = None, -1
    projects = data_root() / "projects"
    if not projects.is_dir():
        return None
    for d in projects.iterdir():
        launcher = d / "runtime" / "launcher"
        for name in ("last-launch.json", "last-launch.json" + HANDOVER):
            try:
                pp = json.loads((launcher / name).read_text(encoding="utf-8-sig")).get("project_path")
            except (OSError, ValueError):
                continue
            if pp and _real(pp) == root:
                n = len(list((d / "plans").glob("MASTER-*.md"))) if (d / "plans").is_dir() else 0
                if n > n_best:
                    best, n_best = d.name, n
    return best


def runner_alive(key: str) -> int | None:
    """V1's runner pid for this key if it is alive. A reused pid reads as alive: the safe side."""
    f = data_root() / "projects" / key / "runtime" / "launcher" / "running.pid"
    try:
        m = re.search(r"\d+", f.read_text())
    except OSError:
        return None
    if not m:
        return None
    pid = int(m.group())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def unshipped(key: str) -> list[str]:
    plans = data_root() / "projects" / key / "plans"
    if not plans.is_dir():
        return []
    return [f"{m.name}: {_status(m)}" for m in sorted(plans.glob("MASTER-*.md"))
            if _status(m) not in ("shipped", "cancelled")]


def take_over(root: str | Path, key: str | None) -> dict:
    """Make V1 skip this repo. Refuses while V1 has unshipped masters or a live runner for it
    (drain first, §4.3 step 1). Returns what it did."""
    root = _real(root)
    if key:
        busy = unshipped(key)
        pid = runner_alive(key)
        if busy or pid:
            raise RuntimeError(f"V1 still has work for {root}: masters {busy[:5]}, runner pid {pid}; "
                               "let it ship or park it (/ilk-park), then take over")
    done: dict = {"root": root, "key": key}
    reg = data_root() / "projects.json"
    if reg.exists():
        data = json.loads(reg.read_text(encoding="utf-8-sig"))
        keep = [e for e in data.get("projects", []) if _real(e.get("path", "")) != root]
        if len(keep) != len(data.get("projects", [])):
            backup = reg.with_name(f"projects.json.bak-ilk2-{int(clock.wall())}")
            shutil.copy2(reg, backup)
            data["projects"] = keep
            tmp = reg.with_name("projects.json.ilk2-tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n")
            os.replace(tmp, reg)
            done["registry"] = f"removed; backup {backup.name}"
    if key:
        launcher = data_root() / "projects" / key / "runtime" / "launcher"
        ll = launcher / "last-launch.json"
        if ll.exists():
            os.replace(ll, launcher / ("last-launch.json" + HANDOVER))
            done["last_launch"] = "renamed aside"
        (data_root() / "projects" / key / MARKER).write_text(
            f"Owned by ilk2 since {clock.wall():.0f}. V1's scheduler skips this project "
            "(skip-unresolved); ilk2 claims its queued masters.\n")
    return done


def claim(key: str) -> list[Path]:
    """Queued masters of an ilk2-owned project, renamed out of V1's reach. Skipped while a V1
    runner is alive for the key (it may have started one)."""
    plans = data_root() / "projects" / key / "plans"
    if not plans.is_dir() or runner_alive(key):
        return []
    out = []
    for m in sorted(plans.glob("MASTER-*.md")):
        if _status(m) != "queued":
            continue
        dest = m.with_name(m.name + CLAIMED)
        try:
            os.replace(m, dest)
        except OSError:
            continue
        out.append(dest)
    return out


def baseline(key: str) -> dict:
    """V1's minutes per accepted step for one project, by ilk2's definition (docs/04 §4.5): total
    iteration minutes over steps advanced, from V1's close records. A record holds each plan's
    state after its iteration, so steps advanced are the per-plan increase between records; the
    first record has no prior state and counts 0 (it understates V1 by at most one iteration)."""
    log = data_root() / "projects" / key / "logs" / ".ilk-loop.log"
    recs = []
    for line in log.read_text().splitlines() if log.exists() else []:
        if '"record_phase": "close"' not in line:
            continue
        try:
            recs.append(json.loads(line[line.index("{"):]))
        except ValueError:
            continue
    recs.sort(key=lambda d: d.get("timestamp", ""))

    def pos(v: dict) -> int:
        try:
            s = int(v.get("step") or 0)
        except ValueError:
            s = 0
        if v.get("status") == "shipped":
            try:
                s = max(s, int(v.get("estimated_steps") or s))
            except ValueError:
                pass
        return s
    last: dict[str, int] = {}
    steps = secs = zero = 0
    for d in recs:
        adv = 0
        for plan, v in ((d.get("execution") or {}).get("step_to") or {}).items():
            if isinstance(v, dict):
                p = pos(v)
                if plan in last:
                    adv += max(0, p - last[plan])
                last[plan] = p
        steps += adv
        secs += d.get("duration_sec") or 0
        zero += adv == 0
    return {"key": key, "iterations": len(recs),
            "from": recs[0].get("timestamp") if recs else None,
            "to": recs[-1].get("timestamp") if recs else None,
            "iteration_minutes": round(secs / 60), "steps_advanced": steps,
            "iterations_without_a_step": zero,
            "minutes_per_accepted_step": round(secs / 60 / steps, 1) if steps else None}
