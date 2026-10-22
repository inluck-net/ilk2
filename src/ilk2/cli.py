"""ilk2 command line (docs/02 §2.8). JSON output is the consumer contract; it carries api_version."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path


API_VERSION = 1


def _store():
    from . import paths
    from .store import Store
    paths.ensure()
    return Store(paths.db())


def _out(obj, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"api_version": API_VERSION, **obj}, indent=2, default=str))
    else:
        print(_human(obj))


def _human(obj) -> str:
    lines = []
    for b in obj.get("batches", []):
        lines.append(f"{b['id']}  {b['status']:<9} pri {b['priority']}  {b['project']}"
                     f"{'  [' + b['consumer_ref'] + ']' if b['consumer_ref'] else ''}"
                     f"{'  ' + b['blocked_reason'] if b['blocked_reason'] else ''}")
        for p in b["plans"]:
            lines.append(f"  {p['slug']:<30} {p['status']}")
            for st in p["steps"]:
                lines.append(f"    {st['idx']} {st['status']:<9} att {st['attempts']}  {st['title'][:60]}"
                             f"{'  ' + st['commit'][:10] if st['commit'] else ''}")
    for r in obj.get("needs_human", []):
        lines.insert(0, f"NEEDS HUMAN {r['id']} [{r['reason']}] {r['detail'][:120]}  -> {r['clear_command']}")
    return "\n".join(lines) or "(nothing)"


def _meta(arg: str | None) -> dict | None:
    if arg is None:
        return None
    try:
        return json.loads(Path(arg[1:]).read_text() if arg.startswith("@") else arg)
    except (OSError, ValueError) as e:
        raise batches.SubmitRejected([f"--meta is not readable JSON: {e}"])


def _root_setup(s, b) -> dict | None:
    """The latest setup run in the batch's execution root, and whether the root's lockfiles
    still match what it installed (deps_current: false after a merge changed them; null when
    the root is gone). None when no setup ran there (no setup hook, or before activation)."""
    from .driver import lock_digest
    root = b["execution_root"]
    for e in reversed(s.all("SELECT payload, at FROM events WHERE batch_id = ? AND kind = "
                            "'setup.ran' ORDER BY cursor", b["id"])):
        p = json.loads(e["payload"])
        if p.get("where") != root:
            continue
        now = lock_digest(Path(root)) if Path(root).is_dir() else None
        return {"commit": p.get("commit"), "lock_digest": p.get("lock_digest"), "ok": p.get("ok"),
                "seconds": p.get("seconds"), "at": e["at"],
                "deps_current": (now == p.get("lock_digest")) if Path(root).is_dir() else None}
    return None


def status(s, batch_id: str | None, consumer_ref: str | None = None) -> dict:
    from . import delivery, lease
    if batch_id:
        rows = s.all("SELECT * FROM batches WHERE id = ?", batch_id)
    elif consumer_ref:  # every batch a consumer submitted under one ref, newest first
        rows = s.all("SELECT * FROM batches WHERE consumer_ref = ? ORDER BY created_at DESC",
                     consumer_ref)
    else:
        rows = s.all("SELECT * FROM batches ORDER BY created_at DESC LIMIT 50")
    out = []
    for b in rows:
        plans = []
        for p in s.all("SELECT * FROM plans WHERE batch_id = ? ORDER BY ord", b["id"]):
            steps = []
            for st in s.all("SELECT * FROM steps WHERE plan_id = ? ORDER BY idx", p["id"]):
                last = s.one("SELECT a.id, a.outcome FROM attempts a WHERE a.step_id = ? "
                             "ORDER BY a.created_at DESC LIMIT 1", st["id"])
                gr = s.all("SELECT gate, outcome, new_failures FROM gate_results WHERE attempt_id = ?",
                           last["id"]) if last else []
                steps.append({"idx": st["idx"], "id": st["id"], "title": st["title"],
                              "status": st["status"], "blocked_reason": st["blocked_reason"],
                              "commit": st["accepted_commit"], "attempts": st["attempt_count"],
                              "restarts": st["restart_count"],
                              "gates": [{"gate": g["gate"], "outcome": g["outcome"],
                                         "new_failures": json.loads(g["new_failures"])} for g in gr]})
            plans.append({"slug": p["slug"], "status": p["status"],
                          "repair_of": p["repair_of_step_id"],
                          "blocked_reason": p["blocked_reason"], "steps": steps})
        lr = lease.get(s, b["execution_root"])
        out.append({"id": b["id"], "version": b["version"], "project": b["project_id"],
                    "status": b["status"],
                    "priority": b["priority"], "consumer_ref": b["consumer_ref"],
                    "terminal_reason": b["terminal_reason"], "blocked_reason": b["blocked_reason"],
                    "base": b["base_commit"], "branch": b["branch"],
                    "execution_root": b["execution_root"],
                    "root_lease": dict(lr) if lr else None,
                    "accepted_head": lr["accepted_head"] if lr else None, "plans": plans,
                    "refusals": [dict(r) for r in s.open_refusals(b["id"])],
                    "meta": json.loads(b["snapshot"]).get("meta"),
                    # gh-resolve D-549/D-550 (Oct 16): when the batch shipped, and what the
                    # execution root's dependencies were installed for
                    "shipped_at": (s.one("SELECT MAX(at) t FROM transitions WHERE entity = 'batches' "
                                         "AND entity_id = ? AND to_state = 'shipped'", b["id"])
                                   or {"t": None})["t"],
                    "setup": _root_setup(s, b),
                    "base_behind": json.loads(b["snapshot"]).get("base_behind"),
                    "delivery": delivery.get(s, b["id"])})  # docs/10 §10.3.3
    if consumer_ref and not batch_id:
        nh = [dict(r) for b in rows for r in s.open_human_rows(b["id"])]
    else:
        nh = [dict(r) for r in s.open_human_rows(batch_id)]
    draining = s.one("SELECT 1 FROM meta WHERE key = 'draining' AND value = '1'") is not None
    return {"batches": out, "needs_human": nh, "draining": draining}  # B4: upgrade in progress


def doctor() -> int:
    """Checks a human can read: release, launchd jobs, supervisor tick, worker template keys."""
    import os
    import subprocess
    from . import clock, paths, release
    s = _store()
    ok = True
    cur = release.current()
    print(f"release: {os.path.realpath(cur) if cur.exists() else 'none installed'}")
    for label in (release.SUPERVISOR, release.HEALTH):
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True)
        print(f"launchd {label}: {'loaded' if r.returncode == 0 else 'not loaded'}")
    row = s.one("SELECT * FROM supervisor WHERE id = 1")
    if row and row["tick_wall"]:
        age = clock.wall() - row["tick_wall"]
        ok &= age < 180
        print(f"supervisor: pid {row['pid']}, last tick {age:.0f}s ago")
    else:
        print("supervisor: never ticked")
    tmpl = paths.home() / "worker-template" / "settings.json"
    if tmpl.exists():
        d = json.loads(tmpl.read_text())  # key names and the model only, never values
        print(f"worker template: model {d.get('model')}, env keys {sorted(d.get('env', {}))}, "
              f"mode {oct(tmpl.stat().st_mode)[-3:]}")
    else:
        ok = False
        print("worker template: missing")
    print(f"needs_human open: {len(s.open_human_rows())}")
    from . import batches
    from .supervisor import HOST_BUDGET, host_config
    live = {r["project_id"]: r["n"] for r in s.all(
        "SELECT b.project_id, COUNT(*) n FROM runs r JOIN batches b ON b.id = r.batch_id WHERE "
        "r.status IN ('starting', 'running') GROUP BY b.project_id")}
    try:
        budget = int(host_config().get("host_budget", HOST_BUDGET))
    except (OSError, ValueError):
        budget = HOST_BUDGET
    print(f"runs: {sum(live.values())} of host_budget {budget} (~/.ilk2/host.json, read every tick)")
    for pr in s.all("SELECT id FROM projects ORDER BY id"):
        c = batches.project_config(s, pr["id"])
        queued = s.one("SELECT COUNT(*) n FROM batches WHERE project_id = ? AND status = 'queued'",
                       pr["id"])["n"]
        print(f"  {pr['id']}: {live.get(pr['id'], 0)} of {c.concurrency_limit} running, {queued} queued"
              + (f", reserve {c.reserve}" if c.reserve else ""))
    s.close()
    return 0 if ok else 1


def shadow_problem(clone: str, of: str) -> str | None:
    """A shadow project must be a separate clone whose push URL cannot push (docs/04 §4.2)."""
    import re
    import subprocess
    if os.path.realpath(clone) == os.path.realpath(of):
        return "a shadow project needs its own clone, not the repo it shadows"
    urls = subprocess.run(["git", "-C", clone, "remote", "-v"], capture_output=True, text=True).stdout
    for line in urls.splitlines():
        name, url, kind = (line.split() + ["", "", ""])[:3]
        if kind == "(push)" and re.match(r"(https?|ssh|git|file)://|[\w.-]+@[\w.-]+:|/", url):
            return (f"remote {name} can push to {url}; set it to nowhere first: "
                    f"git -C {clone} remote set-url --push {name} DISABLED-ilk2-shadow")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ilk2")
    from ilk2 import release as _release
    _v = _release.running_version()
    _h = _release.human(_v)
    ap.add_argument("--version", action="version",
                    version=f"ilk2 {_h}" + (f" ({_v})" if _v != _h else ""))
    ap.add_argument("--api-version", type=int, default=API_VERSION,
                    help="the JSON contract version the caller expects (B2); only 1 exists")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("drive").add_argument("run_id")
    sp = sub.add_parser("supervise")
    sp.add_argument("--stop-after", type=float)
    sub.add_parser("health")

    psub = sub.add_parser("project").add_subparsers(dest="pcmd")
    pr = psub.add_parser("remove", help="drop a project with no unfinished batch")
    pr.add_argument("project_id")
    pr.add_argument("--give-back", action="store_true",
                    help="also hand the repo back to V1 (undo --take-over)")
    ap_ = sub.add_parser("apply", help="bring a commit into a running batch's worktrees")
    ap_.add_argument("batch")
    ap_.add_argument("commit")
    ap_.add_argument("--now", action="store_true", help="interrupt and resume the batch so it applies at once")
    ap_.add_argument("--drop", action="store_true", help="remove the request")
    ps = psub.add_parser("set", help="change project settings: key=JSON value (journalled)")
    ps.add_argument("project_id")
    ps.add_argument("assignments", nargs="+", metavar="key=value")
    pa = psub.add_parser("add")
    pa.add_argument("project_id")
    pa.add_argument("repo")
    pa.add_argument("--config", default="{}", help="JSON, or @file")
    pa.add_argument("--default-branch", default="main")
    pa.add_argument("--shadow-of", help="the repo this separate, push-disabled clone shadows (P5)")
    pa.add_argument("--take-over", action="store_true",
                    help="the repo is V1's: make V1 skip it and claim its queued masters (docs/04 §4.3)")
    psub.add_parser("take-over").add_argument("project_id")
    psub.add_parser("show", help="a project's settings (env values redacted)").add_argument("project_id")
    pi = sub.add_parser("plan-issue", help="headless /ilk2-plan for one consumer issue (docs/07 §7.4.2b): "
                        "plans and checks; never submits. Exit 0 drafted-checked, 3 drafted, 1 failed")
    pi.add_argument("--input", required=True, help="the issue JSON (the planner contract, docs/07 §7.4.2b)")
    pi.add_argument("--outcome", required=True, help="where to write the outcome JSON")
    rs = sub.add_parser("rsi", help="ilk2 improving itself (docs/06 'RSI')")
    rs.add_argument("op", choices=["find", "candidates", "tick", "pending", "land", "log", "resolve",
                                   "reset-goal"])
    rs.add_argument("target", nargs="?", help="resolve: the batch whose held landing to resolve; "
                    "reset-goal: the goal id (bumps its reset in docs/rsi/goals.json and commits it)")
    rs.add_argument("--keep", action="store_true",
                    help="resolve: verify main as it is, then release and canary the merge")
    rs.add_argument("--reverted", metavar="COMMIT", help="resolve: the commit on main that undoes the merge")
    rs.add_argument("--discarded", action="store_true", help="resolve: the merge is not in main")
    rs.add_argument("--repo", help="the ilk2 repo, for Chad's docs/rsi/requests.json "
                    "(default: the 'ilk2' project's repo)")
    rs.add_argument("--all", action="store_true", help="candidates: every status, not only open")
    rs.add_argument("--evidence", action="store_true", help="candidates: include each one's evidence")
    im = sub.add_parser("improvement", help="findings about a project: postmortems, reviews (docs/07 §7.4.3)")
    im.add_argument("op", choices=["add", "list", "set"])
    im.add_argument("--project")
    im.add_argument("--source", help="add: postmortem, review, human or v1-backlog")
    im.add_argument("--fingerprint", help="add: the dedup key; the same finding again adds a hit")
    im.add_argument("--title")
    im.add_argument("--evidence", default="{}", help="add: a JSON object")
    im.add_argument("--id", help="set: the improvement id")
    im.add_argument("--status", help="list: filter; set: open, accepted, rejected or done")
    im.add_argument("--reason", default="", help="set: why (journalled)")
    v1p = sub.add_parser("v1", help="V1's scheduler on this host (docs/04 §4.9, P7)")
    v1p.add_argument("op", choices=["status", "off", "on", "inventory"],
                     help="inventory: what on this host still resolves into V1 (docs/07 §7.4.0)")
    v1p.add_argument("--apply", action="store_true", help="act; without it, report only")

    sm = sub.add_parser("submit")
    sm.add_argument("file")
    sm.add_argument("--project", required=True)
    sm.add_argument("--priority", type=int, default=0)
    sm.add_argument("--consumer-ref")
    sm.add_argument("--base")
    sm.add_argument("--root")
    sm.add_argument("--branch", help="the branch to build on (created from --base if new; B1)")
    sm.add_argument("--deny-path", action="append", default=[],
                    help="a path or glob no step may change, for this batch only (repeatable; adds "
                         "to the project's deny_paths)")
    sm.add_argument("--meta", help="a JSON object (or @file) the consumer keeps with the batch; "
                    "status returns it unchanged")
    sm.add_argument("--check", action="store_true",
                    help="validate exactly as a submit would, and create nothing")
    sm.add_argument("--not-after", type=float, metavar="UNIX_TS",
                    help="refuse (refused:not-after) if the submit is processed after this time; "
                         "checked in the transaction that makes the batch durable")
    sm.add_argument("--fence", nargs=2, metavar=("KEY", "TOKEN"),
                    help="refuse (refused:fence) if a newer integer token was seen for KEY; a "
                         "submit that passes records its token")
    fe = sub.add_parser("fence", help="advance a submit fence: later submits with an older token "
                        "for this key are refused")
    fe.add_argument("--key", required=True)
    fe.add_argument("--token", type=int, required=True)
    sm.add_argument("--json", action="store_true")
    sm.add_argument("--deliver", metavar="KIND", help="end the batch in a delivery: pr (docs/10); "
                    "needs the project's deliverer setting")
    sm.add_argument("--deliver-base", help="the PR's base branch (default: the project's)")
    sm.add_argument("--deliver-draft", action="store_true", help="open the PR as a draft")

    dl = sub.add_parser("deliver", help="the delivery protocol a deliverer speaks (docs/10 §10.3.4)")
    dsub = dl.add_subparsers(dest="dcmd", required=True)
    d_ = dsub.add_parser("list", help="owed and claimed deliveries, with what a deliverer needs")
    d_.add_argument("--by")
    d_.add_argument("--state")
    for verb in ("claim", "done", "failed", "retry"):
        d_ = dsub.add_parser(verb)
        d_.add_argument("--batch", required=True)
        if verb != "retry":
            d_.add_argument("--by", required=True)
    dsub.choices["done"].add_argument("--pr", required=True)
    dsub.choices["failed"].add_argument("--detail", required=True)

    st = sub.add_parser("status")
    st.add_argument("--batch")
    st.add_argument("--consumer-ref", help="every batch submitted with this --consumer-ref")
    st.add_argument("--json", action="store_true")

    ev = sub.add_parser("events")
    ev.add_argument("--since", type=int, default=0)
    ev.add_argument("--batch")
    ev.add_argument("--json", action="store_true")

    pk = sub.add_parser("park")
    pk.add_argument("batch")
    pk.add_argument("--now", action="store_true")
    sub.add_parser("resume").add_argument("batch")
    cn = sub.add_parser("cancel")
    cn.add_argument("batch")
    cn.add_argument("--reason", default="human")
    cn.add_argument("--superseded-by")
    ex = sub.add_parser("export")
    ex.add_argument("batch")
    ex.add_argument("--v1", action="store_true", required=True)
    ex.add_argument("--out", required=True)
    am = sub.add_parser("amend")
    am.add_argument("batch")
    am.add_argument("--file", required=True, help="JSON list of ops")
    am.add_argument("--version", type=int, required=True, help="batch version from status --json")
    am.add_argument("--reason", default="amend")
    ak = sub.add_parser("ack")
    ak.add_argument("id")
    ak.add_argument("--by", default="human")

    ls = sub.add_parser("lease")
    ls.add_argument("op", choices=["acquire", "release", "renew", "recover", "show"])
    ls.add_argument("--root", required=True)
    ls.add_argument("--generation", type=int)
    ls.add_argument("--expected-head")
    ls.add_argument("--new-head")
    ls.add_argument("--owner", default="consumer")
    ls.add_argument("--length", type=float)

    rl = sub.add_parser("release")
    rl.add_argument("op", choices=["build", "install", "upgrade", "uninstall", "push"])
    rl.add_argument("--host", action="append", default=[], help="push: the other host(s), by ssh name")
    rl.add_argument("--version")
    rl.add_argument("--repo", default=".")
    dc = sub.add_parser("doctor")
    dc.add_argument("--hosts", default="", help="other hosts (comma-separated ssh names) to compare releases with")
    sub.add_parser("xbar")

    rp = sub.add_parser("report")
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--project", help="one project (the §4.5 targets compare a project with its V1 baseline)")
    rp.add_argument("--batch", help="per-step numbers for one batch (--steps table)")
    rp.add_argument("--v1-baseline", action="store_true",
                    help="with --project: also V1's numbers for the same repo, by the same method")

    a = ap.parse_args(argv)
    if a.cmd is None:
        ap.print_help(sys.stdout)
        return 0
    if a.cmd == "drive":
        from .driver import main as drive
        return drive(a.run_id)
    if a.cmd == "supervise":
        from .supervisor import main as supervise
        return supervise(a.stop_after)
    if a.cmd == "health":
        from .health import main as health
        return health()
    if a.cmd == "release":
        from . import release
        if a.op == "build":
            print(release.build(Path(a.repo).resolve(), a.version).name)
        elif a.op == "install":
            print(json.dumps(release.install(a.version), indent=2))
        elif a.op == "upgrade":
            print(json.dumps(release.upgrade(a.version), indent=2))
        elif a.op == "push":
            v = a.version or os.path.basename(os.path.realpath(release.current()))
            out = [release.push(h, Path(a.repo).resolve(), v) for h in a.host]
            print(json.dumps(out, indent=2))
            return 0 if out and all(x["ok"] for x in out) else 1
        else:
            release.uninstall()
        return 0
    if a.api_version != API_VERSION:
        _out({"error": "unsupported-api-version", "supported": [API_VERSION],
              "requested": a.api_version}, True)
        return 2
    if a.cmd == "doctor":
        rc = doctor()
        if a.hosts:
            from . import release
            here = os.path.basename(os.path.realpath(release.current()))
            for h in [x for x in a.hosts.split(",") if x]:
                there = release.host_version(h)
                same = "same" if there == here else ("UNREACHABLE" if there is None else "DIFFERENT")
                print(f"host {h}: {there or '-'} ({same} as here: {here})")
                rc = rc or (0 if there == here else 1)
        return rc
    if a.cmd == "xbar":
        from . import xbar
        return xbar.main()

    from . import batches, bounds, gitops, lease, plans, runs
    from .store import Conflict, CursorExpired
    s = _store()
    try:
        if a.cmd == "project":
            from . import v1
            if a.pcmd == "show":
                proj = s.one("SELECT * FROM projects WHERE id = ?", a.project_id)
                if proj is None:
                    _out({"error": "unknown-project", "project": a.project_id}, True)
                    return 2
                _out({"project": proj["id"], "repo_root": proj["repo_root"],
                      "default_branch": proj["default_branch"],
                      "config": batches.redacted(batches.project_config(s, a.project_id))}, True)
                return 0
            if a.pcmd == "remove":
                proj = s.get("projects", a.project_id)
                live = s.one("SELECT COUNT(*) n FROM batches WHERE project_id = ? AND status NOT IN "
                             "('shipped', 'cancelled')", a.project_id)["n"]
                if live:
                    _out({"error": "rejected", "problems": [
                        f"{a.project_id} has {live} unfinished batch(es): cancel or finish them first"]}, True)
                    return 2
                out = {"removed": a.project_id}
                if a.give_back:
                    from . import v1
                    cfg = batches.project_config(s, a.project_id)
                    out["v1"] = v1.give_back(proj["repo_root"], cfg.v1_key)
                cfg = batches.project_config(s, a.project_id)
                cfg.retired = True
                if a.give_back:
                    cfg.v1_key = None  # nothing more to claim from V1
                s.update("projects", a.project_id, {"config": json.dumps(asdict(cfg))}, actor="human",
                         reason="project remove" + (" --give-back" if a.give_back else ""))
                _out(out, True)
                return 0
            if a.pcmd == "set":
                cfg = batches.project_config(s, a.project_id)
                known = set(batches.ProjectConfig.__dataclass_fields__)
                changes = {}
                for kv in a.assignments:
                    k, sep, v = kv.partition("=")
                    if not sep or k not in known:
                        _out({"error": "rejected", "problems": [f"unknown setting {k!r}"]}, True)
                        return 2
                    try:
                        changes[k] = json.loads(v)
                    except ValueError:
                        changes[k] = v  # a bare string
                if changes.get("worker_effort") not in (None, *batches.WORKER_EFFORTS):
                    _out({"error": "rejected", "problems": [
                        f"worker_effort must be one of {list(batches.WORKER_EFFORTS)} or null"]}, True)
                    return 2
                if "deny_commands" in changes:
                    problem = batches.deny_commands_problem(changes["deny_commands"])
                    if problem:
                        _out({"error": "rejected", "problems": [problem]}, True)
                        return 2
                for k, v in changes.items():
                    setattr(cfg, k, v)
                s.update("projects", a.project_id, {"config": json.dumps(asdict(cfg))},
                         actor="human", reason="project set " + " ".join(a.assignments))
                _out({"project": a.project_id, "set": changes}, True)
                return 0
            if a.pcmd == "take-over":
                proj = s.get("projects", a.project_id)
                key = v1.find_key(proj["repo_root"])
                done = v1.take_over(proj["repo_root"], key)
                cfg = batches.project_config(s, a.project_id)
                cfg.v1_key = key
                cfg.enforce_scope = False  # V1's scope_paths are advice, not a limit
                s.update("projects", a.project_id, {"config": json.dumps(asdict(cfg))},
                         actor="human", reason="take over from V1")
                print(json.dumps(done))
                return 0
            cfg = batches.ProjectConfig.from_json(
                Path(a.config[1:]).read_text() if a.config.startswith("@") else a.config)
            problem = batches.deny_commands_problem(cfg.deny_commands)
            if problem:
                _out({"error": "rejected", "problems": [problem]}, True)
                return 2
            root = os.path.realpath(a.repo)
            if a.shadow_of:
                why = shadow_problem(root, a.shadow_of)
                if why:
                    print(f"refused: {why}", file=sys.stderr)
                    return 2
                cfg.shadow_of = os.path.realpath(a.shadow_of)
            elif root in v1.registry_roots() and not a.take_over:
                print(f"refused: V1 still owns {root} (~/.ilk-data/projects.json). One owner per "
                      "repo (docs/04 §4.2): add --take-over to hand it to ilk2.", file=sys.stderr)
                return 2
            if a.take_over:
                cfg.v1_key = v1.find_key(root)
                cfg.enforce_scope = False  # V1's scope_paths are advice, not a limit
                print(json.dumps(v1.take_over(root, cfg.v1_key)), file=sys.stderr)
            batches.project_add(s, a.project_id, Path(a.repo), cfg, a.default_branch)
            print(a.project_id)
        elif a.cmd == "submit":
            f = Path(a.file)
            if f.suffix == ".json":
                spec = plans.load_json(f)
            elif f.name.startswith("MASTER-"):
                spec = plans.parse_v1_master(f)
            else:
                spec = plans.BatchSpec(plans=[plans.parse_v1_subplan(f.read_text())], source="v1-markdown")
            try:
                bid = batches.submit(s, a.project, spec, base=a.base, priority=a.priority,
                                     consumer_ref=a.consumer_ref,
                                     root=Path(a.root) if a.root else None,
                                     originals={f.name: f.read_text()}, branch=a.branch,
                                     deny_paths=a.deny_path, meta=_meta(a.meta),
                                     check_only=a.check, not_after=a.not_after,
                                     fence=(a.fence[0], int(a.fence[1])) if a.fence else None,
                                     deliver=a.deliver, deliver_base=a.deliver_base,
                                     deliver_draft=a.deliver_draft)
            except batches.SubmitRejected as e:
                detail = {}
                if a.check:
                    proj = s.get("projects", a.project)
                    repo = Path(proj["repo_root"])
                    base_sha = gitops.rev(repo, a.branch or a.base or proj["default_branch"])
                    detail["gates"] = batches.gate_resolutions(spec, repo, base_sha)[0]
                _out({"error": "rejected", "problems": e.problems, **detail}, True)  # B3: one shape
                return 2
            if a.check:
                proj = s.get("projects", a.project)
                repo = Path(proj["repo_root"])
                base_sha = gitops.rev(repo, a.branch or a.base or proj["default_branch"])
                bb = batches.base_behind(repo, base_sha, proj["default_branch"]) if base_sha else None
                warn = batches.base_warning(bb)
                warns = ([warn] if warn else []) + batches.plan_warnings(spec) + \
                    batches.gate_path_warnings(spec, repo, base_sha)
                resolutions = batches.gate_resolutions(spec, repo, base_sha)[0]
                _out({"ok": True, "plans": len(spec.plans),
                      "steps": sum(len(p.steps) for p in spec.plans), "base_behind": bb,
                      "gates": resolutions,
                      **({"warnings": warns} if warns else {})}, True)
            else:
                bb = json.loads(s.get("batches", bid)["snapshot"]).get("base_behind")
                warn = batches.base_warning(bb)
                if a.json:
                    _out({"batch": bid, "base_behind": bb, **({"warnings": [warn]} if warn else {})}, True)
                else:
                    print(bid)
                    if warn:
                        print(f"warning: {warn}", file=sys.stderr)
        elif a.cmd == "status":
            _out(status(s, a.batch, a.consumer_ref), a.json)
        elif a.cmd == "events":
            evs = s.events_since(a.since, a.batch)
            if a.json:
                _out({"events": evs, "cursor": evs[-1]["cursor"] if evs else a.since}, True)
            else:
                for e in evs:
                    print(e["cursor"], e["kind"], json.dumps(e["payload"])[:200])
        elif a.cmd == "park":
            batches.park(s, a.batch, now=a.now)
        elif a.cmd == "resume":
            print(json.dumps(batches.resume(s, a.batch)))
        elif a.cmd == "cancel":
            batches.cancel(s, a.batch, a.reason, a.superseded_by)
        elif a.cmd == "export":
            files = batches.export_v1(s, a.batch, Path(a.out))
            _out({"files": [str(f) for f in files]}, True)
        elif a.cmd == "amend":
            v = batches.amend(s, a.batch, json.loads(Path(a.file).read_text()),
                              expect_version=a.version, reason=a.reason)
            _out({"version": v}, True)
        elif a.cmd == "deliver":
            from . import delivery
            try:
                if a.dcmd == "list":
                    _out({"deliveries": delivery.listing(s, a.by, a.state)}, True)
                    return 0
                d = {"claim": lambda: delivery.claim(s, a.batch, a.by),
                     "done": lambda: delivery.done(s, a.batch, a.by, a.pr),
                     "failed": lambda: delivery.failed(s, a.batch, a.by, a.detail),
                     "retry": lambda: delivery.retry(s, a.batch)}[a.dcmd]()
            except delivery.Rejected as e:
                _out({"error": "rejected", "problems": [str(e)]}, True)
                return 2
            _out({"batch": a.batch, "delivery": d}, True)
            return 0
        elif a.cmd == "ack":
            s.ack(a.id, a.by)
        elif a.cmd == "lease":
            if a.op == "show":
                row = lease.get(s, a.root)
                _out({"lease": dict(row) if row else None}, True)
                return 0
            b = s.one("SELECT * FROM batches WHERE execution_root = ? ORDER BY created_at DESC", a.root)
            head = gitops.branch_head(Path(a.root), b["branch"]) if b else gitops.head(Path(a.root))
            length = a.length or bounds.load().lease
            if a.op == "acquire":
                g = lease.consumer_acquire(s, a.root, a.generation, a.expected_head, a.owner, head, length)
                if b:  # a finished batch's root is detached (runs.free_root): the consumer's
                    try:  # merge and commits under the lease go on the branch, as before
                        if gitops.git(Path(a.root), "rev-parse", "--abbrev-ref", "HEAD") != b["branch"]:
                            gitops.git(Path(a.root), "checkout", "-q", b["branch"])
                    except gitops.GitError as e:
                        lease.consumer_release(s, a.root, g, head)
                        raise Conflict(f"cannot check out {b['branch']} in {a.root}: {e}") from e
            elif a.op == "renew":
                g = lease.consumer_renew(s, a.root, a.generation, length)
            elif a.op == "release":
                g = lease.consumer_release(s, a.root, a.generation, a.new_head or head)
            else:
                g = lease.recover(s, a.root, a.generation, a.new_head or head)
            if a.op in ("release", "recover") and b and b["status"] in ("shipped", "cancelled"):
                runs.free_root(s, Path(a.root), b["id"])
            _out({"generation": g}, True)
        elif a.cmd == "apply":
            import time
            b = s.get("batches", a.batch)
            repo = Path(s.get("projects", b["project_id"])["repo_root"])
            sha = gitops.rev(repo, a.commit + "^{commit}")
            if sha is None:
                _out({"error": "rejected", "problems": [f"{a.commit} is not a commit in {repo}"]}, True)
                return 2
            key = f"apply:{a.batch}"
            row = s.one("SELECT value FROM meta WHERE key = ?", key)
            shas = json.loads(row["value"]) if row else []
            shas = [x for x in shas if x != sha] if a.drop else shas + ([sha] if sha not in shas else [])
            with s.tx() as c:
                c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                          "value = excluded.value", (key, json.dumps(shas)))
            s.event("apply.requested" if not a.drop else "apply.dropped", {"commit": sha}, batch_id=a.batch)
            out = {"batch": a.batch, "commits": shas}
            if a.now and not a.drop and s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status IN "
                                               "('starting', 'running')", a.batch):
                batches.park(s, a.batch, now=True)
                for _ in range(120):
                    if not s.one("SELECT 1 FROM runs WHERE batch_id = ? AND status IN ('starting', "
                                 "'running')", a.batch):
                        break
                    time.sleep(5)
                batches.resume(s, a.batch)
                out["restarted"] = True
            _out(out, True)
        elif a.cmd == "fence":
            _out({"key": a.key, "token": batches.advance_fence(s, a.key, a.token)}, True)
        elif a.cmd == "plan-issue":
            from . import plan_issue
            code = plan_issue.run(s, Path(a.input), Path(a.outcome))
            _out(json.loads(Path(a.outcome).read_text()), True)
            return code
        elif a.cmd == "rsi":
            from . import rsi
            repo = Path(a.repo) if a.repo else None
            if repo is None:
                row = s.one("SELECT repo_root FROM projects WHERE id = 'ilk2'")
                repo = Path(row["repo_root"]) if row else None
            if a.op == "find":
                _out(rsi.record(s, rsi.find(s, repo)), True)
            elif a.op == "reset-goal":
                from . import goals
                try:
                    if not a.target or repo is None:
                        raise goals.GoalsError("reset-goal needs a goal id and the ilk2 repo")
                    out = goals.reset(repo, a.target)
                except goals.GoalsError as e:
                    _out({"error": str(e)}, True)
                    return 2
                s.event("rsi.goal-reset", out)
                _out(out, True)
            elif a.op == "tick":
                _out(rsi.tick(s), True)
            elif a.op == "land":
                from . import rsi_land
                _out(rsi_land.land(s), True)
            elif a.op == "resolve":
                from . import rsi_land
                if not a.target:
                    _out({"error": "resolve needs a batch id"}, True)
                    return 2
                try:
                    _out(rsi_land.resolve(s, a.target, keep=a.keep, reverted=a.reverted,
                                          discarded=a.discarded), True)
                except rsi_land.ResolveError as e:
                    _out({"error": str(e)}, True)
                    return 2
            elif a.op == "log":
                from . import rsi_land
                _out({"landings": rsi_land.log(s)}, True)
            elif a.op == "pending":
                from . import rsi_land
                landing = {r["batch_id"]: r["status"] for r in s.all(
                    "SELECT batch_id, status FROM rsi_landings")}
                _out({"pending": [{"batch": b["id"], "branch": b["branch"], "ref": b["consumer_ref"],
                                   "landing": landing.get(b["id"]),
                                   **({"resolve": rsi_land.RESOLVE.format(bid=b["id"])}
                                      if landing.get(b["id"]) in rsi_land.HELD else {})}
                                  for b in rsi.pending(s)]}, True)
            else:
                rows = rsi.candidates(s, None if a.all else rsi.OPEN)
                _out({"candidates": [{**{k: r[k] for k in ("id", "source", "title", "hits", "score",
                                                            "status", "batch_id")},
                                      **({"evidence": json.loads(r["evidence"])} if a.evidence else {})}
                                     for r in rows]}, True)
        elif a.cmd == "improvement":
            from . import improvements as imp
            try:
                if a.op == "add":
                    out = imp.add(s, a.project, a.source, a.fingerprint or "", a.title or "",
                                  json.loads(a.evidence))
                elif a.op == "set":
                    out = imp.set_status(s, a.id or "", a.status or "", "human", a.reason)
                else:
                    out = {"improvements": imp.rows(s, a.project, a.status)}
            except (imp.IntakeError, ValueError) as e:
                _out({"error": "rejected", "problems": [str(e)]}, True)
                return 2
            _out(out, True)
        elif a.cmd == "v1" and a.op == "inventory":
            from . import inventory
            out = inventory.scan()
            _out(out, True)
            return 0 if out["clean"] else 1
        elif a.cmd == "v1":
            from . import v1
            owned = {os.path.realpath(r["repo_root"]) for r in s.all("SELECT repo_root FROM projects")}
            if a.op == "status":
                out = {"needs_v1": v1.needs_v1(owned), "stale": v1.stale_registrations(owned),
                       **v1.scheduler_state()}
            elif a.op == "off":
                out = v1.switch_off(owned, a.apply)
            else:
                out = v1.switch_on(a.apply)
            if a.op in ("off", "on") and a.apply:
                # A host-level switch leaves a trace in ilk2's own log (gh-resolve-e1, Oct 15:
                # rezmac's switch-off at Oct 14 22:11 could only be dated by a plist's ctime).
                s.event(f"v1.{a.op}", {"applied": bool(out.get("applied")),
                                       "blockers": out.get("blockers", [])[:20],
                                       "by": os.environ.get("USER", "")})
            _out(out, True)
            return 2 if a.op == "off" and a.apply and not out.get("applied") else 0
        elif a.cmd == "report":
            from . import report as report_mod
            if a.batch:
                out = report_mod.steps(s, a.batch)
                if not a.json:
                    print(report_mod.steps_table(out))
                    return 0
                _out(out, True)
                return 0
            out = report_mod.build(s, project=a.project)
            if a.v1_baseline and a.project:
                from . import v1
                key = batches.project_config(s, a.project).v1_key or v1.find_key(
                    s.get("projects", a.project)["repo_root"])
                out["v1_baseline"] = v1.baseline(key) if key else None
            _out(out, a.json)
    except Conflict as e:
        _out({"error": "conflict", "detail": str(e)}, True)
        return 3
    except CursorExpired as e:
        _out({"error": "cursor-expired", "detail": str(e)}, True)
        return 4
    except batches.SubmitRejected as e:
        _out({"error": "rejected", "problems": e.problems}, True)
        return 2
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
