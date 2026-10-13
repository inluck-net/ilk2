"""ilk2 command line (docs/02 §2.8). JSON output is the consumer contract; it carries api_version."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from ilk2 import __version__

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


def status(s, batch_id: str | None) -> dict:
    from . import lease
    sql = "SELECT * FROM batches" + (" WHERE id = ?" if batch_id else " ORDER BY created_at DESC LIMIT 50")
    rows = s.all(sql, *([batch_id] if batch_id else []))
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
                    "refusals": [dict(r) for r in s.open_refusals(b["id"])]})
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
    ap.add_argument("--version", action="version", version=f"ilk2 {__version__}")
    ap.add_argument("--api-version", type=int, default=API_VERSION,
                    help="the JSON contract version the caller expects (B2); only 1 exists")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("drive").add_argument("run_id")
    sp = sub.add_parser("supervise")
    sp.add_argument("--stop-after", type=float)
    sub.add_parser("health")

    psub = sub.add_parser("project").add_subparsers(dest="pcmd")
    pa = psub.add_parser("add")
    pa.add_argument("project_id")
    pa.add_argument("repo")
    pa.add_argument("--config", default="{}", help="JSON, or @file")
    pa.add_argument("--default-branch", default="main")
    pa.add_argument("--shadow-of", help="the repo this separate, push-disabled clone shadows (P5)")
    pa.add_argument("--take-over", action="store_true",
                    help="the repo is V1's: make V1 skip it and claim its queued masters (docs/04 §4.3)")
    psub.add_parser("take-over").add_argument("project_id")

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
    sm.add_argument("--json", action="store_true")

    st = sub.add_parser("status")
    st.add_argument("--batch")
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
    rl.add_argument("op", choices=["build", "install", "upgrade", "uninstall"])
    rl.add_argument("--version")
    rl.add_argument("--repo", default=".")
    sub.add_parser("doctor")
    sub.add_parser("xbar")

    rp = sub.add_parser("report")
    rp.add_argument("--json", action="store_true")
    rp.add_argument("--project", help="one project (the §4.5 targets compare a project with its V1 baseline)")
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
        else:
            release.uninstall()
        return 0
    if a.api_version != API_VERSION:
        _out({"error": "unsupported-api-version", "supported": [API_VERSION],
              "requested": a.api_version}, True)
        return 2
    if a.cmd == "doctor":
        return doctor()
    if a.cmd == "xbar":
        from . import xbar
        return xbar.main()

    from . import batches, bounds, gitops, lease, plans
    from .store import Conflict, CursorExpired
    s = _store()
    try:
        if a.cmd == "project":
            from . import v1
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
                spec = plans.BatchSpec(plans=[plans.parse_v1_subplan(f.read_text())])
            try:
                bid = batches.submit(s, a.project, spec, base=a.base, priority=a.priority,
                                     consumer_ref=a.consumer_ref,
                                     root=Path(a.root) if a.root else None,
                                     originals={f.name: f.read_text()}, branch=a.branch,
                                     deny_paths=a.deny_path)
            except batches.SubmitRejected as e:
                _out({"error": "rejected", "problems": e.problems}, True)  # B3: one shape
                return 2
            _out({"batch": bid}, a.json) if a.json else print(bid)
        elif a.cmd == "status":
            _out(status(s, a.batch), a.json)
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
            elif a.op == "renew":
                g = lease.consumer_renew(s, a.root, a.generation, length)
            elif a.op == "release":
                g = lease.consumer_release(s, a.root, a.generation, a.new_head or head)
            else:
                g = lease.recover(s, a.root, a.generation, a.new_head or head)
            _out({"generation": g}, True)
        elif a.cmd == "report":
            from . import report as report_mod
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
