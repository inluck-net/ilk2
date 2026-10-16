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
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from ..plans import Gate
from . import vitest
from .fingerprint import FINGERPRINT_VERSION, fingerprint, is_timeout_class, normalize_message

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
    passed: list[str] = field(default_factory=list)   # ids that ran and passed (for must_pass)
    not_run: list[str] = field(default_factory=list)  # collected but skipped or xfailed
    collected: int = 0
    exitstatus: int | None = None
    duration_s: float = 0.0
    missing_paths: list[str] = field(default_factory=list)
    tail: str = ""
    timeouts: int = 0                # gate-cap hits, for host-load admission (§2.11.1)
    reruns: int = 0                  # extra runs measure() made after a transient unmeasured
    red_lines: list[str] = field(default_factory=list)  # a shell gate's failing output lines

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failures"] = {k: asdict(v) for k, v in self.failures.items()}
        return d

    def failure_map(self) -> dict[str, str]:
        return {k: v.fingerprint for k, v in self.failures.items()}


# -- specification version (§2.5.1) ---------------------------------------------------------

def spec_version(gate: Gate, root: Path, env: dict[str, str]) -> str:
    h = hashlib.sha256()
    h.update(f"fingerprint-v{FINGERPRINT_VERSION}".encode())
    h.update(json.dumps([gate.adapter, gate.command, gate.cwd, gate.cap_s, gate.script,
                         gate.script_entry] + ([gate.env] if gate.env else [])).encode())
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
    elif gate.adapter == "vitest":
        out += vitest.config_files(gate.command, gate.cwd)
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
    takes_value = TAKES_VALUE
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
    return _classify(run, report, rc, out, gate, root)


def _classify(run: GateRun, report: Path, rc: int | None, out: str, gate: Gate, root: Path) -> GateRun:
    """Shared §2.5.5 classification of a report written by ilk2's pytest plugin or vitest reporter."""
    data = json.loads(report.read_text()) if report.exists() else None
    if data is None:
        run.outcome = "unmeasured"
        run.reason = "usage-error" if rc == 4 else f"harness-error (exit {rc}, no report)"
        run.transient = rc != 4
        return run
    tests = data["tests"]
    run.collected = max(data.get("collected", 0), len(tests))
    run.passed = sorted(t["nodeid"] for t in tests if t["outcome"] in ("passed", "xpassed"))
    run.not_run = sorted(t["nodeid"] for t in tests if t["outcome"] in ("skipped", "xfailed"))
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


def run_vitest(gate: Gate, root: Path, env: dict[str, str], spec: str, workdir: Path) -> GateRun:
    cwd = root / gate.cwd
    run = GateRun(gate=gate.name, spec_version=spec, outcome="pass")
    workdir.mkdir(parents=True, exist_ok=True)
    report = workdir / f"vitest-report-{os.getpid()}-{time.monotonic_ns()}.json"
    cmd = vitest.run_command(gate.command, root, gate.cwd, report)
    rc, out, capped, secs = _run(cmd, cwd, env, gate.cap_s)
    run.duration_s, run.exitstatus, run.tail = round(secs, 2), rc, out[-4000:]
    if capped:
        run.outcome, run.reason, run.transient = "unmeasured", f"gate cap {gate.cap_s}s hit", True
        return run
    if rc in (126, 127) or "command not found" in out:
        run.outcome, run.reason = "unmeasured", "runner-not-found"
        return run
    if rc is not None and rc < 0:
        run.outcome, run.reason, run.transient = "unmeasured", f"killed by signal {-rc}", True
        return run
    if not report.exists() and "No test files found" in out:
        run.outcome = "fail" if gate.declares_tests else "uncovered"
        run.reason = "no-tests"
        return run
    return _classify(run, report, rc, out, gate, root)


TAKES_VALUE = {"-k", "-m", "-p", "-c", "--deselect", "--rootdir", "-o", "--junitxml",
               "--timeout", "--timeout-method", "-n", "--maxfail", "--tb", "--ignore",
               "--dist", "-W", "--durations", "--basetemp", "-r", "--log-level", "--cov",
               "--cov-report", "--confcutdir", "--color", "--import-mode", "--capture"}


def with_test_ids(command: list[str], ids: list[str], drop: tuple[str, ...] = ()) -> list[str]:
    """The gate's command with its positional test paths replaced by `ids`. Options and their
    values are kept, except those in `drop` (with their value)."""
    out, i = [], 0
    while i < len(command):
        a = command[i]
        if a in TAKES_VALUE:
            if a not in drop and i + 1 < len(command):
                out += [a, command[i + 1]]
            i += 2
            continue
        # Every positional after the interpreter is a test path. A bare directory ("tests")
        # used to be kept, so narrowing `pytest ... tests` to one id still ran the whole suite
        # (gh-resolve P5 batch, Oct 14: every probe and repair gate ran 8,000+ tests).
        looks_path = i > 0 and not a.startswith("-") and not a.endswith(("python", "python3"))
        if not looks_path:
            out.append(a)
        i += 1
    return out + list(ids)


def rerun_alone(gate: Gate, root: Path, env: dict[str, str], spec: str, workdir: Path,
                test_id: str) -> bool:
    """Re-run one timeout-class test alone, without xdist; True if it passes (§2.5.5, S21)."""
    env = {**env, **gate.env}
    if gate.adapter == "vitest":
        g = replace(gate, command=vitest.with_test_ids(gate.command, [test_id]))
        return run_vitest(g, root, env, spec, workdir).outcome == "pass"
    g = replace(gate, command=with_test_ids(gate.command, [test_id], drop=("-n", "--deselect", "-k")))
    return run_pytest(g, root, env, spec, workdir).outcome == "pass"


_RED_MARK = re.compile(r"❌|🚨|✗|✘|\bFAIL(?:ED)?\b|\bERROR\b|\b[Ee]rror:")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def red_lines(out: str, root: str | None = None) -> list[str]:
    """The failing lines of a shell check's whole output: the lines with a failure marker,
    without colour codes or volatile paths, digits kept (a third regression is not the same as
    two). Compared with the base's for a plan-wide check (judge_inherited_red)."""
    lines = set()
    for ln in out.splitlines():
        ln = _ANSI.sub("", ln)
        if _RED_MARK.search(ln):
            lines.add(normalize_message(ln, root)[:300])
    return sorted(lines)


def run_shell(gate: Gate, root: Path, env: dict[str, str], spec: str, workdir: Path) -> GateRun:
    """A check with plain exit-code meaning (lint, type check, build; §2.5.1). It reports one
    pseudo-test, `<gate>::<exit>`, so a red result is a failure the judge can see. It must be
    green at the base: the driver blocks the batch if it is not."""
    run = GateRun(gate=gate.name, spec_version=spec, outcome="pass")
    workdir.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    env["PYTHONPYCACHEPREFIX"] = str(workdir / f"pycache-{time.monotonic_ns()}")  # B3
    rc, out, capped, secs = _run(gate.command, root / gate.cwd, env, gate.cap_s)
    run.duration_s, run.exitstatus, run.tail = round(secs, 2), rc, out[-4000:]
    if capped:
        run.outcome, run.reason, run.transient = "unmeasured", f"gate cap {gate.cap_s}s hit", True
    elif rc in (126, 127):
        run.outcome, run.reason = "unmeasured", "runner-not-found"
    elif rc is not None and rc < 0:
        run.outcome, run.reason, run.transient = "unmeasured", f"killed by signal {-rc}", True
    elif rc != 0:
        tid = f"{gate.name}::<exit>"
        run.red_lines = red_lines(out, str(root))
        last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
        run.outcome, run.reason = "fail", f"exit {rc}"
        run.failures[tid] = Failure(tid, fingerprint("exit", f"exit {rc}: {last}", [], str(root)),
                                    "exit", normalize_message(last)[:500])
    return run


def measure(gate: Gate, root: Path, env: dict[str, str], workdir: Path,
            spec: str | None = None) -> GateRun:
    """Run a gate with the §2.5.5 rules: a transient unmeasured is re-run once on the same
    tree, and each timeout-class failure is re-run alone once and dropped if it passes."""
    spec = spec or spec_version(gate, root, env)
    if gate.env:
        env = {**env, **gate.env}  # the gate's own assignments win over the pinned environment
    runner = {"pytest": run_pytest, "vitest": run_vitest, "shell": run_shell}.get(gate.adapter)
    if runner is None:
        return GateRun(gate=gate.name, spec_version=spec, outcome="unmeasured",
                       reason=f"adapter {gate.adapter} cannot be a measured test gate")
    r = runner(gate, root, env, spec, workdir)
    timeouts = int("cap" in r.reason and r.transient)
    if r.outcome == "unmeasured" and r.transient:
        # A run that hit its cap is re-run once with twice the cap, then left to a person (Chad,
        # Oct 16: #8519's first kira batch waited 12 h overnight on a suite near its 900 s cap).
        # Judgment call: x2; wrong if suites outgrow any fixed multiple. Still never a pass:
        # an unmeasured gate is not green (I5).
        again = replace(gate, cap_s=gate.cap_s * 2) if "cap" in r.reason else gate
        r2 = runner(again, root, env, spec, workdir)
        timeouts += int("cap" in r2.reason and r2.transient)
        if r2.outcome == "unmeasured":
            r2.transient = False
            r2.reason = f"{r2.reason} (twice)"
        r = r2
        r.reruns = 1  # measure() ran the gate twice: the caller's cost includes both
    r.timeouts = timeouts
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


def unmet_obligations(runs: list[GateRun], expect_red: list[str] | None,
                      must_pass: list[str] | None) -> tuple[list[str], list[str]]:
    """The step's own obligations, over all of its gates together: (must_pass ids not met,
    expected reds that did not fail). Each id needs one gate to meet it; a step whose gates
    split the tests between them must not fail on a gate that never runs the id."""
    passed = {p for r in runs for p in r.passed}
    failed = {f for r in runs for f in r.failures}
    not_run = {n for r in runs for n in r.not_run}
    unmet = []
    for t in must_pass or []:
        if "::" not in t:
            # A whole file: at least one test in it passed, and none failed, was skipped or is
            # still xfailed (V1's "remove every xfail"; Codex M1 review #6).
            ok = any(p.startswith(t + "::") for p in passed) and \
                not any(f.startswith(t + "::") for f in failed | not_run)
        else:
            ok = t in passed and t not in failed
        if not ok:
            unmet.append(t)  # this step must make it pass: failing, absent or uncollected all fail
    return unmet, [t for t in expect_red or [] if t not in failed]


def judge(current: GateRun, base: dict[str, str] | None, expect_red: list[str] | None = None,
          must_pass: list[str] | None = None, obligations: bool = True) -> Verdict:
    """base: test id -> fingerprint failing at the base, or None when there is no comparable
    base (the spec changed): then every failure is new (§2.5.4). With obligations=False the
    expected reds only exclude, and must_pass is left to unmet_obligations over all gates."""
    if current.outcome == "unmeasured":
        return Verdict("unmeasured", [], current.reason)
    expect = set(expect_red or [])
    base = base if base is not None else {}
    new = [tid for tid, f in current.failures.items()
           if tid not in expect and (tid not in base or base[tid] != f.fingerprint)]
    missing_reds: list[str] = []
    if obligations:
        unmet, missing_reds = unmet_obligations([current], expect_red, must_pass)
        new += [t for t in unmet if t not in new]
    if current.outcome == "fail" and current.reason == "no-tests":
        return Verdict("fail", [f"{current.gate}::<no-tests>"], "no-tests")
    if missing_reds:
        return Verdict("fail", new + [f"{t} (expected red, did not fail)" for t in missing_reds],
                       "expected-red-not-red")
    return Verdict("fail" if new else "pass", sorted(new))
