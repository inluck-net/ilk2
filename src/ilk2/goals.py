"""Standing goals for RSI (docs/08 stage B2): Chad's goals in docs/rsi/goals.json, a closed metric
registry, an append-only ledger of rounds, and each goal's state derived on every tick.

A round is one attempt to move a goal's metric: planned, built, landed, then judged on samples
recorded after it closed only, so old samples ageing out of a window never credit a fix. Nothing
about a goal is stored except its rounds: the `goal:<id>` candidate row is a projection,
rewritten each tick from the derived state (docs/08 §8.4).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import clock

REGISTRY_VERSION = 1
GOALS_FILE = Path("docs") / "rsi" / "goals.json"
RETRY_WAIT_S = 2 * 3600       # a plan-failed round waits this long, not for samples
STOP_MISSES = 2               # judgment call (docs/08 §8.4): 2 judged misses in a row stop a goal
STOP_RETRIES = 3              # and 3 plan failures in a row
OP = {"max": "<=", "min": ">="}


class GoalsError(Exception):
    pass


# -- the metric registry (docs/08 §8.5), version 1 ------------------------------------------

def _minutes_per_accepted_step(s, project, since, until):
    rows = s.all(
        "SELECT DISTINCT st.id, (SELECT SUM(a.ended_at - a.created_at) FROM attempts a WHERE "
        "a.step_id = st.id AND a.ended_at IS NOT NULL) secs FROM transitions t JOIN steps st ON "
        "st.id = t.entity_id JOIN batches b ON b.id = st.batch_id WHERE t.entity = 'steps' AND "
        "t.to_state = 'accepted' AND t.at >= ? AND t.at < ? AND b.project_id = ?",
        since, until, project)
    secs = [r["secs"] or 0 for r in rows]
    return (round(sum(secs) / len(secs) / 60, 2) if secs else None), len(secs)


def _share_attempts_producing_nothing(s, project, since, until):
    rows = s.all("SELECT a.outcome, a.timings FROM attempts a JOIN steps st ON st.id = a.step_id "
                 "JOIN batches b ON b.id = st.batch_id WHERE a.status = 'done' AND a.ended_at >= ? "
                 "AND a.ended_at < ? AND b.project_id = ?", since, until, project)
    judged = [r for r in rows if r["outcome"] not in ("interrupted", "api", "quota", "cancelled")
              and not json.loads(r["timings"] or "{}").get("gates_first")]

    def edits(r) -> int:
        tools = json.loads(r["timings"] or "{}").get("tool_counts", {})
        return sum(tools.get(t, 0) for t in ("Edit", "Write", "MultiEdit", "NotebookEdit"))
    nothing = sum(1 for r in judged if r["outcome"] in ("red", "timeout", "crashed") and edits(r) == 0)
    return (round(nothing / len(judged), 3) if judged else None), len(judged)


_BATCH_OF = ("COALESCE(bb.id, bp.batch_id, bs.batch_id) FROM transitions t "
             "LEFT JOIN batches bb ON t.entity = 'batches' AND bb.id = t.entity_id "
             "LEFT JOIN plans bp ON t.entity = 'plans' AND bp.id = t.entity_id "
             "LEFT JOIN steps bs ON t.entity = 'steps' AND bs.id = t.entity_id ")


def _human_transitions_per_batch(s, project, since, until):
    proj = {r["id"] for r in s.all("SELECT id FROM batches WHERE project_id = ?", project)}
    active, human = set(), 0
    for r in s.all(f"SELECT t.actor, {_BATCH_OF} WHERE t.at >= ? AND t.at < ?", since, until):
        bid = r[1]
        if bid in proj:
            active.add(bid)
            human += r["actor"] == "human"
    return (round(human / len(active), 2) if active else None), len(active)


def _rsi_input_failures_last_tick(s, project, since, until):
    rows = s.all("SELECT payload FROM events WHERE kind = 'rsi.tick-completed' AND at >= ? AND "
                 "at < ? ORDER BY cursor DESC", since, until)
    if not rows:
        return None, 0
    src = json.loads(rows[0]["payload"] or "{}").get("sources") or {}
    return sum(1 for v in src.values() if v != "ok"), len(rows)


# path -> (fn(s, project, since, until) -> (value, samples), tolerance(before) -> float)
REGISTRY = {1: {
    "minutes_per_accepted_step": (_minutes_per_accepted_step, lambda b: 0.05 * (b or 0)),
    "share_attempts_producing_nothing": (_share_attempts_producing_nothing, lambda b: 0.02),
    "human_transitions.per_batch": (_human_transitions_per_batch, lambda b: 0.1),
    "rsi_input_failures_last_tick": (_rsi_input_failures_last_tick, lambda b: 0.0),
}}


def metric(s, project: str, path: str, since: float, until: float,
           registry_version: int = REGISTRY_VERSION):
    reg = REGISTRY.get(registry_version)
    if reg is None or path not in reg:
        raise GoalsError(f"metric {path!r} has no implementation in registry version {registry_version}")
    return reg[path][0](s, project, since, until)


# -- goals.json ------------------------------------------------------------------------------

def load(s, repo: Path) -> tuple[list[dict], dict[str, str]]:
    """The valid goals, and {goal id or '#n': why} for the rest. A missing file is no goals; an
    unreadable one raises (the tick records the goals source as failed)."""
    f = repo / GOALS_FILE
    if not f.exists():
        return [], {}
    raw = json.loads(f.read_text())
    if not isinstance(raw, list):
        raise GoalsError("goals.json must be a list")
    projects = {r["id"] for r in s.all("SELECT id FROM projects")}
    ids = [g.get("id") for g in raw if isinstance(g, dict)]
    ok, bad = [], {}
    for i, g in enumerate(raw):
        key = g.get("id") if isinstance(g, dict) and g.get("id") else f"#{i}"
        why = _invalid(g, projects, ids)
        if why:
            bad[key] = why
        else:
            ok.append(g)
    return ok, bad


def _invalid(g, projects: set, ids: list) -> str | None:
    if not isinstance(g, dict):
        return "not an object"
    if not isinstance(g.get("id"), str) or not g["id"]:
        return "no id"
    if ids.count(g["id"]) > 1:
        return "duplicate id"
    if g.get("project") not in projects:
        return f"project {g.get('project')!r} is not registered"
    if g.get("metric") not in REGISTRY[REGISTRY_VERSION]:
        return f"metric {g.get('metric')!r} is not in the registry"
    bounds = [k for k in ("min", "max") if g.get(k) is not None]
    if len(bounds) != 1 or isinstance(g[bounds[0]], bool) or not isinstance(g[bounds[0]], (int, float)):
        return "needs exactly one numeric min or max"
    for k in ("window_days", "min_samples"):
        if isinstance(g.get(k), bool) or not isinstance(g.get(k), int) or g[k] < 1:
            return f"{k} must be a positive integer"
    if g.get("reset") is not None and (isinstance(g["reset"], bool) or not isinstance(g["reset"], int)):
        return "reset must be an integer"
    if g.get("host"):
        return "host: remote goals are not supported yet (docs/08 §8.11)"
    return None


def canonical(g: dict) -> str:
    return json.dumps(g, sort_keys=True, separators=(",", ":"))


def ghash(g: dict) -> str:
    return hashlib.sha256(canonical(g).encode()).hexdigest()[:16]


def _bound(g: dict) -> tuple[str, float]:
    return ("max", g["max"]) if g.get("max") is not None else ("min", g["min"])


def on_target(g: dict, value) -> bool:
    if value is None:
        return True  # no samples: nothing to move
    k, t = _bound(g)
    return value <= t if k == "max" else value >= t


def better(g: dict, before, after, tol: float) -> bool:
    if after is None:
        return False
    if before is None:
        return on_target(g, after)
    k, _ = _bound(g)
    return (before - after) > tol if k == "max" else (after - before) > tol


# -- rounds ----------------------------------------------------------------------------------

def rounds(s, goal_id: str) -> list:
    return s.all("SELECT * FROM rsi_goal_rounds WHERE goal_id = ? ORDER BY started_at", goal_id)


def new_round(s, g: dict, tick: dict, now: float) -> dict:
    """The round's fields, computed before the claim's transaction (no I/O inside it)."""
    from . import procs
    before, n = metric(s, g["project"], g["metric"], now - g["window_days"] * 86400, now)
    tol = REGISTRY[REGISTRY_VERSION][g["metric"]][1](before)
    pid = tick.get("pid") or os.getpid()
    return {"id": f"gr-{ghash(g)[:6]}-{int(now * 1000)}", "goal_id": g["id"],
            "goal_json": canonical(g), "goal_hash": ghash(g), "registry_version": REGISTRY_VERSION,
            "tolerance": tol, "tick_id": tick.get("tick_id"), "tick_pid": pid,
            "tick_proc_start": procs.start_time(pid), "started_at": now,
            "before_value": before, "before_samples": n}


def close(s, rid: str, kind: str, verdict: str | None, at: float, detail: str = "") -> None:
    with s.tx() as c:
        c.execute("UPDATE rsi_goal_rounds SET result_kind = ?, verdict = ?, closed_at = ?, detail = ? "
                  "WHERE id = ? AND closed_at IS NULL", (kind, verdict, at, detail[:1000], rid))


def _held(s, repo, r) -> bool:
    from . import gitops
    b = s.one("SELECT * FROM batches WHERE id = ?", r["batch_id"])
    if not b or b["status"] != "shipped":
        return False
    land = s.one("SELECT status FROM rsi_landings WHERE batch_id = ?", r["batch_id"])
    if land and land["status"] == "merge-interrupted":
        return True
    head, main = gitops.rev(repo, b["branch"]), gitops.rev(repo, "main")
    in_main = bool(head and main and gitops.is_ancestor(repo, head, main))
    return not in_main and (land is None or land["status"] == "review")


def reconcile(s, repo: Path, goals: dict[str, dict], now: float, plan_cap_s: float) -> None:
    """Close every open round that its records close (docs/08 §8.4 close table), whatever the
    current goals file says; then judge landed rounds that have enough new samples."""
    from . import gitops, procs
    for r in s.all("SELECT * FROM rsi_goal_rounds WHERE closed_at IS NULL"):
        if not r["batch_id"]:
            b = s.one("SELECT id FROM batches WHERE json_extract(snapshot, '$.meta.rsi_goal_round_id') = ?",
                      r["id"])
            if b:  # a tick killed between submit and the round update: reattach (durable meta)
                with s.tx() as c:
                    c.execute("UPDATE rsi_goal_rounds SET batch_id = ? WHERE id = ?", (b["id"], r["id"]))
                r = s.one("SELECT * FROM rsi_goal_rounds WHERE id = ?", r["id"])
            elif now - r["started_at"] > 2 * plan_cap_s and not procs.is_same_process(
                    r["tick_pid"], r["tick_proc_start"]):
                close(s, r["id"], "plan-failed", "retry", now, "no result and no batch: the tick died")
                continue
            else:
                continue
        b = s.one("SELECT * FROM batches WHERE id = ?", r["batch_id"])
        land = s.one("SELECT * FROM rsi_landings WHERE batch_id = ?", r["batch_id"])
        g = goals.get(r["goal_id"])
        if b and b["status"] == "cancelled":
            t = s.one("SELECT MAX(at) at FROM transitions WHERE entity = 'batches' AND entity_id = ? "
                      "AND to_state = 'cancelled'", b["id"])
            close(s, r["id"], "cancelled", "miss", (t and t["at"]) or now)
        elif land and land["status"] in ("verified", "pushed"):
            close(s, r["id"], "landed", None, land["verified_at"] or now)
        elif land and land["status"] == "rolled-back":
            close(s, r["id"], "rolled-back", "miss", land["rolled_back_at"] or now)
        elif land and land["status"] == "abandoned":
            close(s, r["id"], "abandoned", "miss", now)
        elif b and b["status"] == "shipped" and (land is None or land["status"] == "review") and \
                gitops.is_ancestor(repo, gitops.rev(repo, b["branch"]) or "", gitops.rev(repo, "main") or ""):
            close(s, r["id"], "landed-by-hand", None, now)
            s.clear_needs_human(b["id"], {"rsi-review"})
        elif (g is None or ghash(g) != r["goal_hash"]) and _held(s, repo, r):
            land_status = land["status"] if land else None
            close(s, r["id"], "abandoned", "miss", now, "the goal changed or was removed while held")
            if land_status != "merge-interrupted":  # that one is resolved with ilk2 rsi resolve
                with s.tx() as c:
                    if land is None:
                        c.execute("INSERT INTO rsi_landings(batch_id, status, note, at) VALUES "
                                  "(?, 'abandoned', 'goal reset while held', ?)", (b["id"], now))
                    else:
                        c.execute("UPDATE rsi_landings SET status = 'abandoned', note = ? WHERE "
                                  "batch_id = ?", ("goal reset while held", b["id"]))
                    s.clear_needs_human(b["id"], {"rsi-review"})
    for r in s.all("SELECT * FROM rsi_goal_rounds WHERE result_kind IN ('landed', 'landed-by-hand') "
                   "AND verdict IS NULL"):
        g = json.loads(r["goal_json"])
        if r["registry_version"] not in REGISTRY:
            continue  # unjudgeable: the state says so
        after, n = metric(s, g["project"], g["metric"], r["closed_at"], now, r["registry_version"])
        if n >= g["min_samples"]:
            v = "improved" if better(g, r["before_value"], after, r["tolerance"] or 0.0) else "no-effect"
            with s.tx() as c:
                c.execute("UPDATE rsi_goal_rounds SET after_value = ?, after_samples = ?, verdict = ? "
                          "WHERE id = ? AND verdict IS NULL", (after, n, v, r["id"]))


def state(s, repo: Path, g: dict, now: float) -> tuple[str, dict]:
    """The goal's derived state (docs/08 §8.4, first match wins) and what it is based on."""
    h = ghash(g)
    mine = [r for r in rounds(s, g["id"]) if r["goal_hash"] == h]
    value, n = metric(s, g["project"], g["metric"], now - g["window_days"] * 86400, now)
    info = {"value": value, "samples": n}
    open_ = [r for r in mine if r["closed_at"] is None]
    if open_:
        info["open_round"] = open_[0]["id"]
        return ("held" if open_[0]["batch_id"] and _held(s, repo, open_[0]) else "in-round"), info
    last = mine[-1] if mine else None
    if last and last["result_kind"] in ("landed", "landed-by-hand") and last["verdict"] is None:
        if last["registry_version"] not in REGISTRY:
            return "unjudgeable", info
        _, after_n = metric(s, g["project"], g["metric"], last["closed_at"], now, last["registry_version"])
        info["needs"] = g["min_samples"] - after_n
        return "judging", info
    if on_target(g, value):
        return "met", info
    judged = [r["verdict"] for r in mine if r["verdict"]]
    if len(judged) >= STOP_MISSES and all(v in ("no-effect", "miss") for v in judged[-STOP_MISSES:]):
        info["rounds"] = [r["id"] for r in mine[-STOP_MISSES:]]
        return "stopped", info
    if len(judged) >= STOP_RETRIES and all(v == "retry" for v in judged[-STOP_RETRIES:]):
        info["rounds"] = [r["id"] for r in mine[-STOP_RETRIES:]]
        return "stopped", info
    if last:
        if last["verdict"] == "retry":
            if now - last["closed_at"] < RETRY_WAIT_S:
                return "waiting", info
        else:
            _, since_n = metric(s, g["project"], g["metric"], last["closed_at"], now)
            if since_n < g["min_samples"]:
                info["needs"] = g["min_samples"] - since_n
                return "waiting", info
    return "eligible", info


def tick(s, repo: Path, now: float, plan_cap_s: float) -> dict[str, str]:
    """One pass: load goals, reconcile rounds, derive each goal's state, and project it onto its
    `goal:<id>` candidate (open only when eligible). Returns {goal id: state}."""
    from . import rsi
    ok, bad = load(s, repo)
    for gid, why in bad.items():
        s.event("rsi.goal-failed", {"goal": gid, "error": why})
    goals = {g["id"]: g for g in ok}
    reconcile(s, repo, goals, now, plan_cap_s)
    states = {gid: "invalid" for gid in bad}
    for g in ok:
        st, info = state(s, repo, g, now)
        states[g["id"]] = st
        _project(s, g, st, info, now, rsi)
        ref = f"goal:{g['id']}"
        if st in ("held", "unjudgeable", "stopped"):
            what = {"held": "its batch is held: merge it, resolve it, or reset the goal",
                    "unjudgeable": "its last round's metric version has no implementation: reset the goal",
                    "stopped": f"rounds {info.get('rounds')} did not move it: change the target or reset"}[st]
            s.open_needs_human("rsi-goal", f"goal {g['id']} is {st}: {what}",
                               "edit docs/rsi/goals.json (or bump the goal's reset)",
                               project_id=rsi.PROJECT, consumer_ref=ref)
        else:
            s.clear_needs_human_reason("rsi-goal", rsi.PROJECT, consumer_ref=ref)
    for r in s.all("SELECT DISTINCT goal_id FROM rsi_goal_rounds"):
        if r["goal_id"] not in goals and r["goal_id"] not in bad:
            states[r["goal_id"]] = "retired"
            cid = f"goal:{r['goal_id']}"
            row = s.one("SELECT status FROM rsi_candidates WHERE id = ?", cid)
            if row and row["status"] not in ("retired", "planning"):
                s.update("rsi_candidates", cid, {"status": "retired"}, actor="rsi", reason="goal removed")
            s.clear_needs_human_reason("rsi-goal", rsi.PROJECT, consumer_ref=cid)
    return states


def _project(s, g: dict, st: str, info: dict, now: float, rsi) -> None:
    cid = f"goal:{g['id']}"
    k, t = _bound(g)
    title = (f"goal {g['id']}: {g['project']} {g['metric']} is {info.get('value')}, "
             f"target {OP[k]} {t} ({g['window_days']} d, {info.get('samples')} samples)")
    ev = json.dumps([{"goal": g["id"], "project": g["project"], "metric": g["metric"],
                      "value": info.get("value"), "target": f"{OP[k]} {t}",
                      "window_days": g["window_days"], "samples": info.get("samples")}])
    status = "open" if st == "eligible" else st
    row = s.one("SELECT status, title FROM rsi_candidates WHERE id = ?", cid)
    if row is None:
        s.insert("rsi_candidates", {"id": cid, "source": "goal", "title": title, "evidence": ev,
                                    "hits": 1, "score": rsi.score("goal", 1), "status": status,
                                    "first_seen": now, "last_seen": now})
    elif row["status"] != "planning" and (row["status"], row["title"]) != (status, title):
        # a claimed goal is the planner's until it answers; unchanged rows are not rewritten
        s.update("rsi_candidates", cid, {"status": status, "title": title, "evidence": ev,
                                         "last_seen": now}, actor="rsi", reason=f"goal {st}")
