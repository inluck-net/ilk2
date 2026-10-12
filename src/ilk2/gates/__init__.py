"""Gates: run a declared gate, classify its outcome, compare with the base (docs/02 §2.5).

A gate result is `pass`, `fail` (with its new failures) or `unmeasured` (with a reason, and
whether it is transient); a gate that collected nothing it was not declared to is `uncovered`.
Only the pytest adapter is implemented for M1's first pass; vitest follows for S20/S21.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..plans import Gate
from .fingerprint import fingerprint, is_timeout_class, normalize_message

PLUGIN_DIR = Path(__file__).parent
KILL_GRACE_S = 30

PYTEST_CONFIGS = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")


@dataclass
class Failure:
    test_id: str
    fingerprint: str
    type: str | None
    message: str
    timeout_class: bool = False


@dataclass
class GateRun:
    gate: str
    spec_version: str
    outcome: str                     # pass | fail | unmeasured | uncovered
    reason: str = ""
    transient: bool = False
    failures: dict[str, Failure] = field(default_factory=dict)
    collected: int = 0
    exitstatus: int | None = None
    duration_s: float = 0.0
    missing_paths: list[str] = field(default_factory=list)
    tail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failures"] = {k: asdict(v) for k, v in self.failures.items()}
        return d

    def failure_map(self) -> dict[str, str]:
        return {k: v.fingerprint for k, v in self.failures.items()}


# -- specification version (§2.5.1) ---------------------------------------------------------

def spec_version(gate: Gate, root: Path, env: dict[str, str]) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([gate.adapter, gate.command, gate.cwd, gate.cap_s]).encode())
    stable_env = {k: v for k, v in env.items() if k not in ("HOME", "TMPDIR")}
    h.update(json.dumps(stable_env, sort_keys=True).encode())
    for rel in config_files(gate, root):
        p = root / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.is_file() else b"<absent>")
    return h.hexdigest()[:16]


def config_files(gate: Gate, root: Path) -> list[str]:
    """Files that decide what a gate runs, relative to root."""
    out: list[str] = []
    if gate.adapter == "pytest":
        for d in sorted({".", gate.cwd}):
            out += [str(Path(d) / c) for c in PYTEST_CONFIGS]
        ls = subprocess.run(["git", "-C", str(root), "ls-files", "--", "*conftest.py"],
                            capture_output=True, text=True)
        out += sorted(ls.stdout.split())
    return sorted(set(out))


# -- running ------------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path, env: dict[str, str], cap_s: int) -> tuple[int | None, str, bool, float]:
    """(returncode, output, capped, seconds). The gate runs in its own process group."""
    t0 = time.monotonic()
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, start_new_session=True)
    except FileNotFoundError as e:
        return 127, str(e), False, 0.0
    except PermissionError as e:
        return 126, str(e), False, 0.0
    try:
        out, _ = p.communicate(timeout=cap_s)
        return p.returncode, out, False, time.monotonic() - t0
    except subprocess.TimeoutExpired:
        for sig, wait in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 10)):
            try:
                os.killpg(p.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                out, _ = p.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                out = ""
        return None, out or "", True, time.monotonic() - t0


def _split_missing(command: list[str], cwd: Path) -> tuple[list[str], list[str]]:
    """Drop positional test paths that do not exist (yet): they measure as '0 tests' (§2.5.4)."""
    kept, missing = [], []
    takes_value = {"-k", "-m", "-p", "-c", "--deselect", "--rootdir", "-o", "--junitxml",
                   "--timeout", "--timeout-method", "-n", "--maxfail", "--tb"}
    skip_next = False
    for i, a in enumerate(command):
        if skip_next:
            kept.append(a)
            skip_next = False
            continue
        if a in takes_value:
            kept.append(a)
            skip_next = True
            continue
        looks_path = not a.startswith("-") and ("/" in a or a.endswith(".py")) and i > 0 \
            and a not in ("python", "python3") and not a.endswith("/python3")
        if looks_path and not (cwd / a.split("::")[0]).exists():
            missing.append(a)
            continue
        kept.append(a)
    return kept, missing


def run_pytest(gate: Gate, root: Path, env: dict[str, str], spec: str, workdir: Path) -> GateRun:
    cwd = root / gate.cwd
    cmd, missing = _split_missing(gate.command, cwd)
    run = GateRun(gate=gate.name, spec_version=spec, outcome="pass", missing_paths=missing)
    test_args_left = any(not a.startswith("-") and ("/" in a or a.endswith(".py"))
                         for a in cmd[1:] if a not in ("python", "python3"))
    if missing and not test_args_left:
        # Every declared path is absent: 0 tests. At the base that is a valid measurement with
        # no base failures (§2.5.4); after a step, a gate that declares tests fails.
        run.outcome = "fail" if gate.declares_tests else "uncovered"
        run.reason = "no-tests"
        return run
    workdir.mkdir(parents=True, exist_ok=True)
    report = workdir / f"pytest-report-{os.getpid()}-{time.monotonic_ns()}.json"
    env = dict(env)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PLUGIN_DIR), env.get("PYTHONPATH")]))
    # A fresh bytecode cache per run: a same-size edit within the same second would otherwise
    # reuse a stale .pyc (mtime+size validation), and the gate would test old code.
    env["PYTHONPYCACHEPREFIX"] = str(workdir / f"pycache-{time.monotonic_ns()}")
    cmd = cmd + ["-p", "ilk2_pytest_report", f"--ilk2-report={report}"]
    rc, out, capped, secs = _run(cmd, cwd, env, gate.cap_s)
    run.duration_s, run.exitstatus, run.tail = round(secs, 2), rc, out[-4000:]
    if capped:
        run.outcome, run.reason, run.transient = "unmeasured", f"gate cap {gate.cap_s}s hit", True
        return run
    if rc in (126, 127) or re.search(r"No module named pytest|command not found", out):
        run.outcome, run.reason = "unmeasured", "runner-not-found"
        return run
    if rc is not None and rc < 0:
        run.outcome, run.reason, run.transient = "unmeasured", f"killed by signal {-rc}", True
        return run
    data = json.loads(report.read_text()) if report.exists() else None
    if data is None:
        run.outcome = "unmeasured"
        run.reason = "usage-error" if rc == 4 else f"harness-error (exit {rc}, no report)"
        run.transient = rc != 4
        return run
    tests = data["tests"]
    run.collected = max(data.get("collected", 0), len(tests))
    rootp = str(root)
    for t in tests:
        if t["outcome"] in ("failed", "error"):
            msg = t.get("message") or ""
            run.failures[t["nodeid"]] = Failure(
                t["nodeid"], fingerprint(t.get("type"), msg, t.get("frames", []), rootp),
                t.get("type"), normalize_message(msg)[:500], is_timeout_class(msg))
    for ce in data["collect_errors"]:
        tid = f"{ce['nodeid'] or '<root>'}::<collect>"
        msg = ce.get("message") or ""
        run.failures[tid] = Failure(tid, fingerprint(ce.get("type"), msg, ce.get("frames", []), rootp),
                                    ce.get("type"), normalize_message(msg)[:500])
    if rc == 5 or run.collected == 0 and not run.failures:
        run.outcome = "fail" if gate.declares_tests else "uncovered"
        run.reason = "no-tests"
        return run
    if rc not in (0, None) and not run.failures:
        summary = (out.strip().splitlines() or [""])[-1]
        tid = f"{gate.name}::<exit>"
        run.failures[tid] = Failure(tid, fingerprint("exit", f"{rc} {summary}", []), "exit",
                                    normalize_message(summary))
        run.reason = "nonzero-exit-no-failures"
    run.outcome = "fail" if run.failures else "pass"
    return run


def rerun_alone(gate: Gate, root: Path, env: dict[str, str], spec: str, workdir: Path,
                test_id: str) -> bool:
    """Re-run one timeout-class test alone; True if it passes (§2.5.5, S21)."""
    base_cmd = [a for a in gate.command if a not in ("-n",)]
    keep: list[str] = []
    skip = False
    for a in base_cmd:
        if skip:
            skip = False
            continue
        if a in ("-n", "--deselect", "-k"):
            skip = True
            continue
        if not a.startswith("-") and ("/" in a or a.endswith(".py")) and a not in ("python3",):
            continue
        keep.append(a)
    g = Gate(name=gate.name, adapter=gate.adapter, command=keep + [test_id], cwd=gate.cwd,
             cap_s=gate.cap_s)
    r = run_pytest(g, root, env, spec, workdir)
    return r.outcome == "pass"


def measure(gate: Gate, root: Path, env: dict[str, str], workdir: Path,
            spec: str | None = None) -> GateRun:
    """Run a gate with the §2.5.5 rules: a transient unmeasured is re-run once on the same
    tree, and each timeout-class failure is re-run alone once and dropped if it passes."""
    spec = spec or spec_version(gate, root, env)
    if gate.adapter != "pytest":
        return GateRun(gate=gate.name, spec_version=spec, outcome="unmeasured",
                       reason=f"adapter {gate.adapter} not implemented yet")
    r = run_pytest(gate, root, env, spec, workdir)
    if r.outcome == "unmeasured" and r.transient:
        r2 = run_pytest(gate, root, env, spec, workdir)
        if r2.outcome == "unmeasured":
            r2.transient = False
            r2.reason = f"{r2.reason} (twice)"
        r = r2
    for tid, f in list(r.failures.items()):
        if f.timeout_class and rerun_alone(gate, root, env, spec, workdir, tid):
            del r.failures[tid]
    if r.outcome == "fail" and not r.failures and r.reason != "no-tests":
        r.outcome = "pass"
    return r


# -- comparison with the base (§2.5.4, §2.5.6) ----------------------------------------------

@dataclass
class Verdict:
    outcome: str                      # pass | fail | unmeasured
    new_failures: list[str]
    reason: str = ""


def judge(current: GateRun, base: dict[str, str] | None, expect_red: list[str] | None = None) -> Verdict:
    """base: test id -> fingerprint failing at the base, or None when there is no comparable
    base (the spec changed): then every failure is new (§2.5.4)."""
    if current.outcome == "unmeasured":
        return Verdict("unmeasured", [], current.reason)
    expect = set(expect_red or [])
    base = base if base is not None else {}
    new = [tid for tid, f in current.failures.items()
           if tid not in expect and (tid not in base or base[tid] != f.fingerprint)]
    missing_reds = [t for t in expect if t not in current.failures]
    if current.outcome == "fail" and current.reason == "no-tests":
        return Verdict("fail", [f"{current.gate}::<no-tests>"], "no-tests")
    if missing_reds:
        return Verdict("fail", new + [f"{t} (expected red, did not fail)" for t in missing_reds],
                       "expected-red-not-red")
    return Verdict("fail" if new else "pass", sorted(new))
