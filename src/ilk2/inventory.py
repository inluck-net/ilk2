"""What on this host still resolves into V1 (docs/07 §7.4.0): the installed-artifact manifest.

Read-only. An artifact is V1 when its real target is under ~/.ilk or ~/.ilk-data, or inside a V1
clone (the ilk-skills checkout). Covered: skills, commands, hooks, tools and rules links for
Claude Code, Codex, Cursor and ~/.agents; hook registrations in Claude's settings.json; the
installer's auto-plan blocks; PATH links; V1 worker homes; launchd files by name or content, in
any folder; and the consumer repos that read V1, as matching-line counts at a recorded commit.
Worker homes and plists are reported by path only: their contents can hold credentials, so
they are matched, never printed. The data trees are listed apart; they go at the archive (§7.4.6).

clean is true when every installed artifact and consumer count is 0 or on the allow-list
(~/.ilk2/v1-allowlist.json: [{"path": ..., "reason": ...}]; a consumer by its name).
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

HOST_DIRS = (".claude", ".codex", ".cursor", ".agents")
LINK_SUBDIRS = ("skills", "commands", "hooks", "tools", "rules", "agents")
AUTO_PLAN_FILES = (".claude/CLAUDE.md", ".codex/AGENTS.md")
AUTO_PLAN_MARKER = "<!-- ilk:auto-plan:start"     # the ilk2-native block uses its own marker
WORKER_HOMES = (".claude-worker", ".claude-manager")
EXTRA_PATH_DIRS = ("~/.local/bin", "~/bin", "/usr/local/bin", "/opt/homebrew/bin")
V1_LABEL = re.compile(r"net\.inluck\.ilk[.-]")    # net.inluck.ilk2.* does not match
V1_CONTENT = re.compile(rb"/\.ilk(?:-data)?/")     # nor does /.ilk2/


@dataclass
class Consumer:
    name: str
    repo: str
    sub: str
    pattern: str
    glob: str = "*.py"
    skip_tests: bool = True

    def command(self) -> str:
        g = f"--glob '{self.glob}'" + (" --glob '!**/tests/**'" if self.skip_tests else "")
        return f"rg -n {g} -e '{self.pattern}' {self.sub}"


def default_consumers(home: Path) -> list[Consumer]:
    """The consumers docs/07 §7.4.4 names; their baselines were counted with these commands."""
    base = home / "Projects" / "github" / "inluck-net"
    return [Consumer("gh-resolve", str(base / "gh-resolve"), "tools/gh_resolve",
                     r"\.ilk-data|ILK_DATA_HOME|ILK_DATA_DIR|\.ilk/current|ilk-loop|park_master\.py"),
            Consumer("ilk-pocket", str(base / "ilk-pocket"), "ilk_pocket", r"\.ilk-data|ILK_DATA_HOME"),
            Consumer("mac-provision", str(base / "mac-provision"), "lib",
                     r"\.ilk-data|\.ilk/current|net\.inluck\.ilk\.scheduler|skills/ilk-loop",
                     glob="*.sh", skip_tests=False)]


def _under(p: str, roots: list[str]) -> bool:
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _real(p: Path) -> str:
    return os.path.realpath(p)


def _expand(p: str, home: Path) -> str:
    """~ means the scanned home, not the caller's $HOME."""
    return str(home) + p[1:] if p == "~" or p.startswith("~/") else p


def _links(home: Path, roots: list[str]) -> list[dict]:
    out = []
    for h in HOST_DIRS:
        for sub in LINK_SUBDIRS:
            d = home / h / sub
            for e in sorted(d.iterdir()) if d.is_dir() else []:
                t = _real(e)
                if _under(t, roots):
                    out.append({"kind": f"{sub[:-1] if sub.endswith('s') else sub}", "host": h,
                                "path": str(e), "target": t})
    return out


def _hook_registrations(home: Path, roots: list[str]) -> list[dict]:
    try:
        cfg = json.loads((home / ".claude" / "settings.json").read_text())
    except (OSError, ValueError):
        return []
    out = []
    for event, groups in (cfg.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                for tok in re.findall(r"[~/][^\s'\"]+", h.get("command", "")):
                    path = _expand(tok, home)
                    t = _real(Path(path))
                    if _under(t, roots):
                        out.append({"kind": "hook-registration", "host": ".claude", "event": event,
                                    "path": path, "target": t})
    return out


def _auto_plan(home: Path) -> list[dict]:
    out = []
    for rel in AUTO_PLAN_FILES:
        f = home / rel
        try:
            if AUTO_PLAN_MARKER in f.read_text():
                out.append({"kind": "auto-plan-block", "path": str(f)})
        except OSError:
            pass
    rule = home / ".cursor" / "rules" / "ilk-auto-plan.mdc"
    if rule.exists():
        out.append({"kind": "auto-plan-block", "path": str(rule)})
    return out


def _path_links(path_dirs: list[str], roots: list[str]) -> list[dict]:
    out, seen = [], set()
    for d in path_dirs:
        d = Path(os.path.expanduser(d))
        if str(d) in seen or not d.is_dir():
            continue
        seen.add(str(d))
        for e in sorted(d.iterdir()):
            if e.is_symlink() and _under(_real(e), roots):
                out.append({"kind": "path-link", "path": str(e), "target": _real(e)})
    return out


def _launchd(agents: Path) -> list[dict]:
    out = []
    for f in sorted(agents.rglob("*")) if agents.is_dir() else []:
        if not f.is_file() or ".plist" not in f.name:
            continue
        why = []
        if V1_LABEL.search(f.name):
            why.append("name")
        try:
            if V1_CONTENT.search(f.read_bytes()):
                why.append("content")
        except OSError:
            pass
        if why:
            out.append({"kind": "launchd", "path": str(f), "matched": why})
    return out


def _consumer(c: Consumer) -> dict:
    repo = Path(c.repo)
    row = {"name": c.name, "repo": c.repo, "command": c.command()}
    if not (repo / c.sub).is_dir():
        return {**row, "absent": True}
    commit = subprocess.run(["git", "-C", c.repo, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip() or None
    rx = re.compile(c.pattern)
    lines = occ = 0
    files = set()
    for f in sorted((repo / c.sub).rglob(c.glob)):
        rel = f.relative_to(repo)
        if c.skip_tests and "tests" in rel.parts:
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            n = len(rx.findall(line))
            if n:
                lines += 1
                occ += n
                files.add(str(rel))
    return {**row, "commit": commit, "lines": lines, "occurrences": occ, "files": len(files)}


def allowlist(home: Path) -> dict[str, str]:
    try:
        rows = json.loads((home / ".ilk2" / "v1-allowlist.json").read_text())
    except (OSError, ValueError):
        return {}
    return {_expand(r["path"], home): r.get("reason", "") for r in rows if r.get("path")}


def scan(home: Path | None = None, clones: list[str] | None = None,
         consumers: list[Consumer] | None = None, path_dirs: list[str] | None = None,
         agents: Path | None = None) -> dict:
    home = home or Path.home()
    clones = clones if clones is not None else [str(home / "Projects/github/inluck-net/ilk-skills")]
    roots = [_real(home / ".ilk"), _real(home / ".ilk-data"), *(_real(Path(c)) for c in clones)]
    if path_dirs is None:
        path_dirs = os.environ.get("PATH", "").split(os.pathsep) + list(EXTRA_PATH_DIRS)
    agents = agents or home / "Library" / "LaunchAgents"
    allowed = allowlist(home)
    arts = (_links(home, roots) + _hook_registrations(home, roots) + _auto_plan(home)
            + _path_links(path_dirs, roots) + _launchd(agents)
            + [{"kind": "worker-home", "path": str(home / w)} for w in WORKER_HOMES
               if (home / w).exists()])
    for a in arts:
        if a["path"] in allowed:
            a["allowed"] = allowed[a["path"]]
    cons = [_consumer(c) for c in (consumers if consumers is not None else default_consumers(home))]
    for c in cons:
        if c["name"] in allowed:
            c["allowed"] = allowed[c["name"]]
    open_arts = [a for a in arts if "allowed" not in a]
    open_cons = [c for c in cons if c.get("lines") and "allowed" not in c]
    return {"host": socket.gethostname().split(".")[0], "artifacts": arts, "consumers": cons,
            "data": [str(home / d) for d in (".ilk", ".ilk-data") if (home / d).exists()],
            "open": {"artifacts": len(open_arts), "consumers": len(open_cons)},
            "clean": not open_arts and not open_cons}
