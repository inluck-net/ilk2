"""Where one batch's time went (docs/evidence E5): read-only over the store, git and worker transcripts.

Usage: uv run python tools/batch_time.py <batch-id> [--long-s 60]

Per attempt it splits the attempt's wall time into model requests, long requests and tool
time, from the worker transcript rows between the attempt's created_at and ended_at. A
request is one model message id; its time runs from the row that preceded it (prompt or tool
result) to its last row. Subagent rows (isSidechain) are counted apart. It then compares the
batch's accepted work steps with the project's other batches, by one declared method: the
median over batches of each batch's per-step mean, and the pooled per-step median.
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import statistics
import subprocess
from datetime import datetime
from pathlib import Path

DATA = Path(os.environ.get("ILK2_HOME", Path.home() / ".ilk2")) / "data"
EDITS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def ts(t: str) -> float:
    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()


def transcript(run_id: str, session_id: str) -> Path | None:
    hits = glob.glob(str(DATA / "runs" / run_id / "*" / "config" / "projects" / "*" / f"{session_id}.jsonl"))
    return Path(hits[0]) if hits else None


def attempt_split(path: Path, start: float, end: float, long_s: float) -> dict:
    reqs: dict[str, list] = {}
    side = set()
    prev = None          # timestamp of the row a request answers
    last_asst = None
    tool_s = 0.0
    first_edit = None    # (request start, request end, chars)
    pre_edit_calls = 0
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not d.get("timestamp"):
            continue
        t = ts(d["timestamp"])
        if t < start or t > end:
            continue
        if d.get("isSidechain"):
            if d.get("type") == "assistant":
                side.add(d["message"].get("id"))
            continue
        if d.get("type") == "user":
            if last_asst is not None:
                tool_s += t - last_asst
                last_asst = None
            prev = t
        elif d.get("type") == "assistant":
            m = d["message"]
            r = reqs.setdefault(m.get("id"), [prev if prev is not None else t, t])
            r[1] = t
            last_asst = t
            for c in m.get("content", []):
                if c.get("type") != "tool_use":
                    continue
                if c["name"] in EDITS and first_edit is None:
                    i = c["input"]
                    first_edit = (r[0], t, len(i.get("content") or i.get("new_string") or ""))
                elif first_edit is None:
                    pre_edit_calls += 1
    for sub in path.with_suffix("").glob("subagents/*.jsonl"):
        for line in sub.read_text().splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "assistant" and d.get("timestamp") and start <= ts(d["timestamp"]) <= end:
                side.add(d["message"].get("id"))
    lat = [b - a for a, b in reqs.values()]
    long = [x for x in lat if x > long_s]
    return {"wall_min": (end - start) / 60, "requests": len(lat), "subagent_requests": len(side),
            "model_min": sum(lat) / 60, "long_n": len(long), "long_min": sum(long) / 60,
            "tool_min": tool_s / 60, "median_request_s": statistics.median(lat) if lat else None,
            "first_edit_start_min": (first_edit[0] - start) / 60 if first_edit else None,
            "first_edit_end_min": (first_edit[1] - start) / 60 if first_edit else None,
            "first_edit_chars": first_edit[2] if first_edit else None, "pre_edit_calls": pre_edit_calls}


def numstat(repo: str, commit: str) -> dict:
    out = subprocess.run(["git", "-C", repo, "show", "--numstat", "--format=", commit],
                         capture_output=True, text=True).stdout
    tests = src = docs = files = 0
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        files += 1
        n, path = int(parts[0]), parts[2]
        if path.startswith("tests/"):
            tests += n
        elif path.startswith("docs/"):
            docs += n
        else:
            src += n
    return {"files": files, "ins": tests + src + docs, "tests": tests, "src": src, "docs": docs}


def work_steps(db, batch_id: str) -> list:
    return db.execute("""
        select s.id, p.slug, s.idx, s.accepted_commit, length(s.instructions) chars,
               (select sum(a.ended_at - a.created_at) from attempts a where a.step_id = s.id) secs
        from steps s join plans p on p.id = s.plan_id
        where s.batch_id = ? and s.status = 'accepted' and not p.verification
        order by p.ord, s.idx""", (batch_id,)).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("batch")
    ap.add_argument("--long-s", type=float, default=60.0)
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{DATA / 'state.db'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    b = db.execute("select * from batches where id = ?", (a.batch,)).fetchone()
    repo = db.execute("select repo_root from projects where id = ?", (b["project_id"],)).fetchone()[0]
    ev = db.execute("select kind, at from events where batch_id = ? and kind in ('submitted', 'batch.shipped') "
                    "order by cursor", (a.batch,)).fetchall()
    times = {e["kind"]: e["at"] for e in ev}
    plans = db.execute("select slug, verification from plans where batch_id = ?", (a.batch,)).fetchall()
    print(f"# {a.batch} ({b['project_id']}, {b['consumer_ref']}), status {b['status']}")
    print(f"plans: {sum(not p['verification'] for p in plans)} work + {sum(p['verification'] for p in plans)} verification")
    if "submitted" in times and "batch.shipped" in times:
        print(f"submitted to shipped: {(times['batch.shipped'] - times['submitted']) / 3600:.2f} h")

    print("\n## Accepted work steps\n")
    print("| Plan · step | Minutes | Files | Inserted (tests / src / docs) | Instruction chars |")
    print("|---|---|---|---|---|")
    mine = []
    for s in work_steps(db, a.batch):
        n = numstat(repo, s["accepted_commit"])
        mine.append((s["secs"] / 60, n["ins"]))
        print(f"| {s['slug']} · {s['idx']} | {s['secs'] / 60:.1f} | {n['files']} | "
              f"{n['ins']} ({n['tests']} / {n['src']} / {n['docs']}) | {s['chars']} |")

    print(f"\n## Attempts (long request > {a.long_s:.0f} s)\n")
    print("| Step · attempt | Outcome | Wall | Model | Long (n, min) | Tools | Requests (+subagent) | "
          "Median req s | Pre-edit calls | First edit start → end (min), chars |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in db.execute("""select a.*, s.idx, p.slug from attempts a join steps s on s.id = a.step_id
                           join plans p on p.id = s.plan_id where s.batch_id = ? and not p.verification
                           order by a.created_at""", (a.batch,)):
        path = transcript(r["run_id"], r["session_id"]) if r["session_id"] else None
        outcome = r["outcome"]
        inner = json.loads(r["timings"] or "{}").get("outcome")
        if inner and inner != outcome:
            outcome = f"{outcome} ({inner})"
        if not path:
            print(f"| {r['slug']} · {r['idx']} #{r['n']} | {outcome} | no transcript |||||||")
            continue
        x = attempt_split(path, r["created_at"], r["ended_at"], a.long_s)
        fe = (f"{x['first_edit_start_min']:.1f} → {x['first_edit_end_min']:.1f}, {x['first_edit_chars']}"
              if x["first_edit_start_min"] is not None else "—")
        print(f"| {r['slug']} · {r['idx']} #{r['n']} | {outcome} | {x['wall_min']:.1f} | {x['model_min']:.1f} | "
              f"{x['long_n']}, {x['long_min']:.1f} | {x['tool_min']:.1f} | {x['requests']} (+{x['subagent_requests']}) | "
              f"{x['median_request_s']:.0f} | {x['pre_edit_calls']} | {fe} |")

    print("\n## Against the project's other batches (accepted work steps)\n")
    per_batch, pooled = [], []
    for o in db.execute("select id from batches where project_id = ? and id != ? order by created_at",
                        (b["project_id"], a.batch)):
        rows = [(s["secs"] / 60, numstat(repo, s["accepted_commit"])["ins"], s["chars"])
                for s in work_steps(db, o["id"]) if s["accepted_commit"]]
        if rows:
            per_batch.append(tuple(statistics.mean(c) for c in zip(*rows)))
            pooled.extend(rows)
    m = [statistics.mean(c) for c in zip(*mine)]
    print(f"other batches: {len(per_batch)}, their accepted work steps: {len(pooled)}")
    print("| Measure | This batch (per-step mean) | Median of batch means | Pooled per-step median |")
    print("|---|---|---|---|")
    for i, name in enumerate(["Minutes per step", "Inserted lines per step"]):
        print(f"| {name} | {m[i]:.1f} | {statistics.median(x[i] for x in per_batch):.1f} | "
              f"{statistics.median(x[i] for x in pooled):.1f} |")
    print(f"| Instruction chars per step | — | {statistics.median(x[2] for x in per_batch):.0f} | "
          f"{statistics.median(x[2] for x in pooled):.0f} |")


if __name__ == "__main__":
    main()
