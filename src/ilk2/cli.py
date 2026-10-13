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
    return {"batches": out, "needs_human": nh}


def report(s) -> dict:
    acc = s.all("SELECT st.id, (SELECT SUM(a.ended_at - a.created_at) FROM attempts a WHERE "
                "a.step_id = st.id) AS secs FROM steps st WHERE st.status = 'accepted'")
    secs = [r["secs"] or 0 for r in acc]
    atts = s.all("SELECT outcome, timings FROM attempts WHERE status = 'done'")
    nothing = sum(1 for a in atts if a["outcome"] in ("red", "timeout", "crashed")
                  and json.loads(a["timings"] or "{}").get("tool_counts", {}).get("Edit", 0) == 0)
    firsts = [json.loads(a["timings"] or "{}").get("timings", {}).get("first_edit_s") for a in atts]
    firsts = [f for f in firsts if f is not None]
    humans = s.one("SELECT COUNT(*) n FROM transitions WHERE actor = 'human'")["n"]
    nb = s.one("SELECT COUNT(*) n FROM batches")["n"] or 1
    return {"minutes_per_accepted_step": round(sum(secs) / 60 / len(secs), 2) if secs else None,
            "accepted_steps": len(secs),
            "share_attempts_producing_nothing": round(nothing / len(atts), 3) if atts else None,
            "human_transitions_per_batch": round(humans / nb, 2),
            "refused_hours": {k: round(v, 2) for k, v in s.refused_hours().items()},
            "minutes_to_first_edit_median": round(sorted(firsts)[len(firsts) // 2] / 60, 2) if firsts else None}


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ilk2")
    ap.add_argument("--version", action="version", version=f"ilk2 {__version__}")
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
    if a.cmd == "doctor":
        return doctor()
    if a.cmd == "xbar":
        from . import xbar
        return xbar.main()

    from . import batches, bounds, gitops, lease, plans
    from .store import Conflict
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
                s.update("projects", a.project_id, {"config": json.dumps(asdict(cfg))},
                         actor="human", reason="take over from V1")
                print(json.dumps(done))
                return 0
            cfg = batches.ProjectConfig.from_json(
                Path(a.config[1:]).read_text() if a.config.startswith("@") else a.config)
            root = os.path.realpath(a.repo)
            if root in v1.registry_roots() and not a.take_over:
                print(f"refused: V1 still owns {root} (~/.ilk-data/projects.json). One owner per "
                      "repo (docs/04 §4.2): add --take-over to hand it to ilk2.", file=sys.stderr)
                return 2
            if a.take_over:
                cfg.v1_key = v1.find_key(root)
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
                                     originals={f.name: f.read_text()})
            except batches.SubmitRejected as e:
                _out({"rejected": e.problems}, True)
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
            _out(report(s), a.json)
    except Conflict as e:
        _out({"error": "conflict", "detail": str(e)}, True)
        return 3
    except batches.SubmitRejected as e:
        _out({"error": "rejected", "problems": e.problems}, True)
        return 2
    finally:
        s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
