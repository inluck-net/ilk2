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
    """V1's runner pid for this key if it is alive. A runner starts before it writes its pid
    file, so a process that started after the file was last written holds a reused pid and is
    not the runner (rezmac, Oct 15: a pid file from Sep 4 named pid 52670, by then an
    sshd-session, and held `ilk2 v1 off`). When the start time cannot be read, a live pid
    still reads as alive: the safe side."""
    f = data_root() / "projects" / key / "runtime" / "launcher" / "running.pid"
    try:
        m = re.search(r"\d+", f.read_text())
        written = f.stat().st_mtime
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
        pass
    started = _started_epoch(pid)
    if started is not None and started > written + 5:  # 5 s: ps reports whole seconds
        return None
    return pid


def _started_epoch(pid: int) -> float | None:
    from datetime import datetime
    from .procs import start_time
    s = start_time(pid)
    try:
        return datetime.strptime(" ".join(s.split()), "%a %b %d %H:%M:%S %Y").timestamp() if s else None
    except ValueError:
        return None


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
    left: list[str] = []
    if key:
        # Only what V1 could run blocks the handover: a queued or active master, or a live runner.
        # Drafts, paused and blocked masters stay where they are; one released later (queued) is
        # claimed by ilk2 (ilk-skills, Oct 13: 6 such masters, none runnable).
        pending = unshipped(key)
        runnable = [m for m in pending if m.endswith((": queued", ": active"))]
        left = [m for m in pending if m not in runnable]
        pid = runner_alive(key)
        if runnable or pid:
            raise RuntimeError(f"V1 still has work for {root}: masters {runnable[:5]}, runner pid {pid}; "
                               "let it ship or park it (/ilk-park), then take over")
    done: dict = {"root": root, "key": key, "left_in_v1": left}
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


# -- the per-host switch-off (docs/04 §4.9, P7) ------------------------------------------------

SCHEDULER = "net.inluck.ilk.scheduler"
DISABLED_DIR = "disabled-by-ilk2"


def launch_agents() -> Path:
    return Path(os.environ.get("ILK2_LAUNCH_AGENTS") or (Path.home() / "Library" / "LaunchAgents"))


def launchctl() -> str:
    return os.environ.get("ILK2_LAUNCHCTL") or "/bin/launchctl"


def needs_v1(owned_roots: set[str]) -> list[str]:
    """What on this host still needs V1's scheduler (§4.9): a registered repo ilk2 does not own,
    a queued or active master, or a live V1 runner, for any key."""
    out = [f"registered in V1, not owned by ilk2: {r}" for r in sorted(registry_roots() - owned_roots)
           if Path(r).exists()]
    projects = data_root() / "projects"
    for d in sorted(projects.iterdir()) if projects.is_dir() else []:
        runnable = [m for m in unshipped(d.name) if m.endswith((": queued", ": active"))]
        out += [f"{d.name}: runnable master {m}" for m in runnable]
        pid = runner_alive(d.name)
        if pid:
            out.append(f"{d.name}: V1 runner alive, pid {pid}")
    return out


def stale_registrations(owned_roots: set[str]) -> list[str]:
    """Registered repos whose path is gone: V1's scheduler skips them (`skip-missing-path`), so
    they need nothing. rezmac, Oct 14: 5 of 8 per-run resolver registrations pointed at
    worktrees that gh-resolve's release had disposed."""
    return sorted(r for r in registry_roots() - owned_roots if not Path(r).exists())


def scheduler_state() -> dict:
    agents = launch_agents()
    return {"plist": (agents / f"{SCHEDULER}.plist").exists(),
            "disabled_plist": (agents / DISABLED_DIR / f"{SCHEDULER}.plist").exists()}


def switch_off(owned_roots: set[str], apply: bool) -> dict:
    """Disable, never delete: boot the scheduler out and move its plist aside. Refused while
    anything still needs V1. Without apply, reports what it would do."""
    import subprocess
    blockers = needs_v1(owned_roots)
    agents = launch_agents()
    plist = agents / f"{SCHEDULER}.plist"
    out = {"blockers": blockers, **scheduler_state(), "applied": False}
    if blockers or not plist.exists() or not apply:
        return out
    subprocess.run([launchctl(), "bootout", f"gui/{os.getuid()}/{SCHEDULER}"], capture_output=True)
    (agents / DISABLED_DIR).mkdir(exist_ok=True)
    os.replace(plist, agents / DISABLED_DIR / plist.name)
    out.update(scheduler_state(), applied=True)
    return out


def switch_on(apply: bool) -> dict:
    """The rollback: move the plist back and bootstrap the scheduler."""
    import subprocess
    agents = launch_agents()
    moved = agents / DISABLED_DIR / f"{SCHEDULER}.plist"
    out = {**scheduler_state(), "applied": False}
    if not moved.exists() or not apply:
        return out
    plist = agents / moved.name
    os.replace(moved, plist)
    subprocess.run([launchctl(), "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True)
    out.update(scheduler_state(), applied=True)
    return out


def give_back(root: str | Path, key: str | None) -> dict:
    """Undo take_over for one repo: V1 resolves it again. Only that repo's registry entry is
    restored, from the newest ilk2 backup that has it, so registry changes made since are kept;
    the key's last-launch.json is renamed back and the ownership marker removed."""
    root = _real(root)
    done: dict = {"root": root, "key": key}
    reg = data_root() / "projects.json"
    entry = None
    for bak in sorted(data_root().glob("projects.json.bak-ilk2-*"), reverse=True):
        try:
            data = json.loads(bak.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        entry = next((e for e in data.get("projects", []) if _real(e.get("path", "")) == root), None)
        if entry:
            done["entry_from"] = bak.name
            break
    if entry and reg.exists():
        data = json.loads(reg.read_text(encoding="utf-8-sig"))
        if not any(_real(e.get("path", "")) == root for e in data.get("projects", [])):
            data.setdefault("projects", []).append(entry)
            tmp = reg.with_name("projects.json.ilk2-tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n")
            os.replace(tmp, reg)
            done["registry"] = "entry restored"
    if key:
        launcher = data_root() / "projects" / key / "runtime" / "launcher"
        aside = launcher / ("last-launch.json" + HANDOVER)
        if aside.exists() and not (launcher / "last-launch.json").exists():
            os.replace(aside, launcher / "last-launch.json")
            done["last_launch"] = "renamed back"
        marker = data_root() / "projects" / key / MARKER
        if marker.exists():
            marker.unlink()
    return done
