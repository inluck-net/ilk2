"""Releases, install and upgrade (docs/02 §2.7).

- A release is ~/.ilk2/releases/<version>/: a uv-built virtualenv with the ilk2 wheel, and a
  manifest with hashes. ~/.ilk2/current points at one release (an atomic symlink swap).
- launchd runs the supervisor (KeepAlive) and the health job (every 60 s) from `current`.
- Upgrade drains: the supervisor stops admitting, drivers stop at step boundaries, and when
  no run is left (or the drain bound passes) the supervisor exits; launchd restarts it from
  the new `current`. Code runs only from immutable releases (I7).
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import __version__, clock, paths

SUPERVISOR = "net.inluck.ilk2.supervisor"
HEALTH = "net.inluck.ilk2.health"
AGENTS = Path.home() / "Library" / "LaunchAgents"


def releases() -> Path:
    return paths.home() / "releases"


def current() -> Path:
    return paths.home() / "current"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build(repo: Path, version: str | None = None) -> Path:
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=8", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", "src"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        raise RuntimeError("src/ has uncommitted changes; a release is built from a commit")
    version = version or f"{__version__}+g{sha}"
    dest = releases() / version
    if dest.exists():
        return dest
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["uv", "build", "--wheel", "-o", d, str(repo)], check=True,
                       capture_output=True)
        wheel = next(Path(d).glob("*.whl"))
        tmp = releases() / f".{version}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        subprocess.run(["uv", "venv", "-q", "--python", "3.12", str(tmp / "venv")], check=True)
        subprocess.run(["uv", "pip", "install", "-q", "--python", str(tmp / "venv" / "bin" / "python"),
                        str(wheel)], check=True)
        shutil.copy2(wheel, tmp / wheel.name)
        (tmp / "manifest.json").write_text(json.dumps({
            "version": version, "commit": sha, "wheel": wheel.name, "wheel_sha256": sha256(wheel),
            "built_at": clock.wall()}, indent=2))
        tmp.rename(dest)
    relocate_venv(dest / "venv", tmp, dest)
    return dest


def relocate_venv(venv: Path, old: Path, new: Path) -> None:
    """The venv is built under a temporary dir and renamed into place; its console scripts'
    shebangs and activate scripts still name the temporary dir (`ilk2` failed with "bad
    interpreter" on both hosts, Oct 13). Rewrite every text file in bin/ that does."""
    old_s, new_s = str(old), str(new)
    for f in (venv / "bin").iterdir():
        if f.is_symlink() or not f.is_file():
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if old_s in text:
            mode = f.stat().st_mode
            f.write_text(text.replace(old_s, new_s))
            f.chmod(mode)


def launchd_path() -> str:
    """PATH for launchd jobs: the real dirs of claude and node, then the system dirs.
    launchd gives jobs a minimal PATH, and fnm's per-shell dirs are ephemeral."""
    dirs = []
    for exe in ("claude", "node", "uv", "git"):
        w = shutil.which(exe)
        if w:
            d = str(Path(os.path.realpath(w)).parent)
            if d not in dirs:
                dirs.append(d)
    return ":".join(dirs + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def plist(label: str, args: list[str], keepalive: bool) -> dict:
    log = str(paths.logs() / f"{label.rsplit('.', 1)[-1]}.log")
    d = {"Label": label, "ProgramArguments": args, "RunAtLoad": True,
         "StandardOutPath": log, "StandardErrorPath": log,
         "EnvironmentVariables": {"PATH": launchd_path()}}
    if os.environ.get("ILK2_HOME"):
        d["EnvironmentVariables"]["ILK2_HOME"] = os.environ["ILK2_HOME"]
    if keepalive:
        d["KeepAlive"] = True
    else:
        d["StartInterval"] = 60
    return d


def install(version: str, load: bool = True) -> dict:
    rel = releases() / version
    if not (rel / "manifest.json").exists():
        raise RuntimeError(f"release {version} is not built")
    link = current()
    tmp = link.with_name(".current.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(rel)
    os.replace(tmp, link)  # atomic swap
    py = str(link / "venv" / "bin" / "python")
    AGENTS.mkdir(parents=True, exist_ok=True)
    out = {"current": str(rel), "agents": []}
    for label, args, keep in ((SUPERVISOR, [py, "-m", "ilk2", "supervise"], True),
                              (HEALTH, [py, "-m", "ilk2", "health"], False)):
        p = AGENTS / f"{label}.plist"
        p.write_bytes(plistlib.dumps(plist(label, args, keep)))
        out["agents"].append(str(p))
        if load:
            uid = os.getuid()
            loaded = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                                    capture_output=True).returncode == 0
            if not loaded:
                subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], check=True)
    return out


def needs_drain(recorded: dict[str, str] | None, pkg: Path) -> bool:
    """A drain restarts the supervisor, and admits nothing until every live run reaches an
    attempt boundary. It is needed only when code the running supervisor loaded changed:
    drivers spawn from `current`, so driver and worker changes reach the next run anyway
    (docs/06, upgrades block admission, Oct 14). Unknown means drain."""
    if not recorded:
        return True
    for rel, sha in recorded.items():
        f = pkg / rel
        if not f.exists() or sha256(f) != sha:
            return True
    return False


def upgrade(version: str) -> dict:
    """Install the new release; ask the running supervisor to drain only when its own code
    changed (§2.7)."""
    from .store import Store
    out = install(version, load=True)
    s = Store(paths.db())
    try:
        row = s.one("SELECT value FROM meta WHERE key = 'supervisor_code'")
        pkgs = sorted((releases() / version / "venv" / "lib").glob("python*/site-packages/ilk2"))
        if pkgs and not needs_drain(json.loads(row["value"]) if row else None, pkgs[-1]):
            s.event("upgrade", {"version": version, "drain": False})
            out["drain"] = False
            return out
        out["drain"] = True
        with s.tx() as c:
            c.execute("INSERT INTO meta(key, value) VALUES ('draining', '1') "
                      "ON CONFLICT(key) DO UPDATE SET value = '1'")
            c.execute("INSERT INTO meta(key, value) VALUES ('drain_started_wall', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(clock.wall()),))
        s.event("upgrade", {"version": version})
    finally:
        s.close()
    return out


def uninstall() -> None:
    uid = os.getuid()
    for label in (SUPERVISOR, HEALTH):
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        (AGENTS / f"{label}.plist").unlink(missing_ok=True)
