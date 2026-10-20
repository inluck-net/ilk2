"""Releases, install and upgrade (docs/02 §2.7).

- A release is ~/.ilk2/releases/<version>/: a uv-built virtualenv with the ilk2 wheel, and a
  manifest with hashes. <version> is `0.3.57+g5f58fcdd`: a number people read, and the commit
  after `+g` that tools read (push, the RSI push check). See version_of. ~/.ilk2/current points at one release (an atomic symlink swap).
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
import re
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


def version_of(repo: Path, sha: str) -> str:
    """The release version for commit `sha` (Chad, Oct 20: one number a person can read, kept
    for every release): `__version__` as of that commit, its patch raised by the commits since
    the commit that last set `__version__`, then `+g<sha>`. Only git history decides it, so both
    hosts, and RSI's unattended releases, name a commit alike. Setting `__version__` by hand
    (0.4.0) restarts the count."""
    init = "src/ilk2/__init__.py"
    text = subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{init}"], capture_output=True,
                          text=True, check=True).stdout
    major, minor, patch = re.search(r'__version__ = "(\d+)\.(\d+)\.(\d+)"', text).groups()
    anchor = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%H", "-G", "^__version__",
                             sha, "--", init], capture_output=True, text=True, check=True).stdout.strip()
    n = int(subprocess.run(["git", "-C", str(repo), "rev-list", "--count", f"{anchor}..{sha}"],
                           capture_output=True, text=True, check=True).stdout.strip()) if anchor else 0
    return f"{major}.{minor}.{int(patch) + n}+g{sha}"


def human(version: str | None) -> str:
    """The part of a release version a person reads: 0.3.57+g5f58fcdd -> 0.3.57."""
    return (version or "?").split("+", 1)[0]


def running_version() -> str:
    """This process's release version, from its release's manifest (the venv lives in
    releases/<version>/venv); the package version when not run from a release."""
    try:
        return json.loads((Path(os.sys.prefix).parent / "manifest.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return __version__


def build(repo: Path, version: str | None = None) -> Path:
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short=8", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--", "src"],
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        raise RuntimeError("src/ has uncommitted changes; a release is built from a commit")
    version = version or version_of(repo, sha)
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


# -- other hosts (docs/02 §2.7: both hosts run the same release) ---------------------------------

def _ssh() -> list[str]:
    return (os.environ.get("ILK2_SSH") or "ssh").split()


def _scp() -> list[str]:
    return (os.environ.get("ILK2_SCP") or "scp").split()


REMOTE_ENV = 'export PATH="$HOME/.local/bin:$PATH"; '


def host_version(host: str) -> str | None:
    """The release `current` points at on another host, or None if unreachable."""
    p = subprocess.run([*_ssh(), host, REMOTE_ENV + "readlink ~/.ilk2/current"],
                       capture_output=True, text=True, timeout=60)
    return Path(p.stdout.strip()).name if p.returncode == 0 and p.stdout.strip() else None


def push(host: str, repo: Path, version: str) -> dict:
    """Ship this repo's main to another host and build and upgrade the same release there:
    a git bundle (the hosts share no remote), fetched into ~/.ilk2/src, built, upgraded."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        bundle = Path(d) / "ilk2.bundle"
        subprocess.run(["git", "-C", str(repo), "bundle", "create", str(bundle), "main"],
                       check=True, capture_output=True)
        subprocess.run([*_scp(), "-q", str(bundle), f"{host}:/tmp/ilk2.bundle"], check=True,
                       capture_output=True, timeout=300)
    # The commit of the release being pushed, not main's tip: main may have moved on since this
    # host's release (Oct 14: a docs-only commit after the release made the remote build a
    # version the upgrade then could not find).
    sha = version.rsplit("+g", 1)[1] if "+g" in version else "FETCH_HEAD"
    script = (REMOTE_ENV + "set -e; cd ~/.ilk2/src && git fetch -q /tmp/ilk2.bundle main && "
              f"git reset -q --hard {sha} && uv run -q ilk2 release build --repo . >/dev/null && "
              f"uv run -q ilk2 release upgrade --version {version} >/dev/null && "
              "readlink ~/.ilk2/current")
    p = subprocess.run([*_ssh(), host, script], capture_output=True, text=True, timeout=900)
    now = Path(p.stdout.strip().splitlines()[-1]).name if p.stdout.strip() else None
    return {"host": host, "ok": p.returncode == 0 and now == version, "current": now,
            "error": p.stderr.strip()[-500:] if p.returncode else None}
