"""pytest plugin loaded into gate runs (-p ilk2_pytest_report): writes one JSON report.

Runs inside the project's own interpreter, which may be old (V1 uses Python 3.9), so it uses
no syntax newer than 3.8. With xdist, reports are forwarded to the controller, where these
hooks run, so one file is written. Output path: --ilk2-report (an option, not an environment
variable, so the pinned environment carries no ILK2_* name; docs/02 §2.5.2, S25).
"""

import json

_tests = {}
_collect_errors = []
_meta = {"collected": 0}


def _crash(rep):
    lr = rep.longrepr
    out = {"type": None, "message": "", "frames": []}
    if lr is None:
        return out
    crash = getattr(lr, "reprcrash", None)
    if crash is not None:
        msg = crash.message or ""
        out["message"] = msg
        first = msg.split(":", 1)[0].strip()
        out["type"] = first if first and " " not in first else None
    tb = getattr(lr, "reprtraceback", None)
    if tb is not None:
        for e in getattr(tb, "reprentries", []):
            loc = getattr(e, "reprfileloc", None)
            if loc is not None:
                out["frames"].append({"path": loc.path, "lineno": loc.lineno,
                                      "message": loc.message})
    if not out["message"]:
        out["message"] = str(lr)[-2000:]
    return out


def pytest_addoption(parser):
    parser.addoption("--ilk2-report", default=None)


def pytest_collection_finish(session):
    _meta["collected"] = len(session.items)


def pytest_collectreport(report):
    if report.failed:
        c = _crash(report)
        _collect_errors.append({"nodeid": report.nodeid, **c})


def pytest_runtest_logreport(report):
    t = _tests.setdefault(report.nodeid, {"nodeid": report.nodeid, "outcome": "passed",
                                          "when": None, "duration": 0.0})
    t["duration"] += getattr(report, "duration", 0.0) or 0.0
    if report.when == "call" and report.skipped and hasattr(report, "wasxfail"):
        t["outcome"] = "xfailed"
    elif report.skipped and t["outcome"] == "passed":
        t["outcome"] = "skipped"
    if report.failed:
        t["outcome"] = "failed" if report.when == "call" else "error"
        t["when"] = report.when
        t.update(_crash(report))
    if report.when == "call" and report.passed and hasattr(report, "wasxfail"):
        t["outcome"] = "xpassed"


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("--ilk2-report")
    if not path or hasattr(session.config, "workerinput"):  # xdist workers do not write
        return
    with open(path, "w") as f:
        json.dump({"exitstatus": int(exitstatus), "collected": _meta["collected"],
                   "tests": list(_tests.values()), "collect_errors": _collect_errors}, f)
