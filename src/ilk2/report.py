"""`ilk2 report`: the docs/04 §4.5 acceptance rows, for one project or all.

These are the numbers each phase gate is judged on (P4: gh-resolve's batches against its V1
baseline; P5/P7: the resolver). Every row is computed from the store's own records: attempts,
the transitions journal, needs_human rows and refusal rows.
"""

from __future__ import annotations

import json
import statistics

from . import clock


def _in_project(project: str | None, alias: str = "b") -> tuple[str, list]:
    return (f" AND {alias}.project_id = ?", [project]) if project else ("", [])


def build(s, project: str | None = None) -> dict:
    pf, pa = _in_project(project)

    # Minutes per accepted step, all attempts (§4.5 row 1)
    acc = s.all("SELECT st.id, (SELECT SUM(a.ended_at - a.created_at) FROM attempts a WHERE "
                "a.step_id = st.id AND a.ended_at IS NOT NULL) AS secs FROM steps st JOIN batches b "
                f"ON b.id = st.batch_id WHERE st.status = 'accepted'{pf}", *pa)
    secs = [r["secs"] or 0 for r in acc]

    # Share of attempts that produce nothing (row 2): a judged attempt that ended red, timed out
    # or crashed without a single edit. Interrupted/API attempts were never judged.
    atts = s.all("SELECT a.outcome, a.timings FROM attempts a JOIN steps st ON st.id = a.step_id "
                 f"JOIN batches b ON b.id = st.batch_id WHERE a.status = 'done'{pf}", *pa)
    # A gates-first run (verification, kept work) is a measurement with no worker: not judged.
    judged = [a for a in atts if a["outcome"] not in ("interrupted", "api", "quota", "cancelled")
              and not json.loads(a["timings"] or "{}").get("gates_first")]

    def edits(a) -> int:
        tools = json.loads(a["timings"] or "{}").get("tool_counts", {})
        return sum(tools.get(t, 0) for t in ("Edit", "Write", "MultiEdit", "NotebookEdit"))
    nothing = sum(1 for a in judged if a["outcome"] in ("red", "timeout", "crashed") and edits(a) == 0)
    firsts = [json.loads(a["timings"] or "{}").get("timings", {}).get("first_edit_s") for a in judged]
    firsts = [f for f in firsts if f is not None]

    # Human transitions per batch, each with its reason (row 3)
    items = s.all(
        "SELECT t.entity, t.entity_id, t.from_state, t.to_state, t.actor, t.reason, t.at, "
        "COALESCE(bb.id, bp.batch_id, bs.batch_id) AS batch_id FROM transitions t "
        "LEFT JOIN batches bb ON t.entity = 'batches' AND bb.id = t.entity_id "
        "LEFT JOIN plans bp ON t.entity = 'plans' AND bp.id = t.entity_id "
        "LEFT JOIN steps bs ON t.entity = 'steps' AND bs.id = t.entity_id "
        "WHERE t.actor = 'human'")
    proj_of = {r["id"]: r["project_id"] for r in s.all("SELECT id, project_id FROM batches")}
    items = [dict(i) for i in items if not project or proj_of.get(i["batch_id"]) == project]
    per_batch: dict[str, int] = {}
    for i in items:
        per_batch[i["batch_id"]] = per_batch.get(i["batch_id"], 0) + 1
    n_batches = s.one(f"SELECT COUNT(*) n FROM batches b WHERE 1=1{pf}", *pa)["n"]

    # Silent stuck states (row 4, I9): a terminal or blocked state without a readable reason
    silent = s.one(f"SELECT COUNT(*) n FROM batches b WHERE ((b.status IN ('shipped', 'cancelled') "
                   f"AND b.terminal_reason IS NULL) OR (b.status = 'blocked' AND b.blocked_reason IS "
                   f"NULL)){pf}", *pa)["n"]
    silent += s.one("SELECT COUNT(*) n FROM runs r JOIN batches b ON b.id = r.batch_id WHERE "
                    f"r.status = 'ended' AND r.terminal_reason IS NULL{pf}", *pa)["n"]
    silent += s.one("SELECT COUNT(*) n FROM attempts a JOIN steps st ON st.id = a.step_id JOIN batches "
                    f"b ON b.id = st.batch_id WHERE a.status = 'done' AND a.outcome IS NULL{pf}", *pa)["n"]

    # Bound passed without an action (row 5, I11): a live run past the supervisor's own bound
    from .supervisor import Supervisor
    sup = Supervisor(s, load_check=False)
    now = clock.mono()
    past = 0
    for r in s.all("SELECT r.* FROM runs r JOIN batches b ON b.id = r.batch_id WHERE r.status IN "
                   f"('starting', 'running'){pf}", *pa):
        if r["boot_time"] not in (None, clock.boot_time()) or r["phase_started_mono"] is None:
            continue
        if now - r["phase_started_mono"] > sup.phase_bound(r):
            past += 1

    # Human waits (row 6): open rows, and median minutes from creation to clearing
    rows = s.all("SELECT n.state, n.created_at, n.acked_at, n.cleared_at FROM needs_human n "
                 "LEFT JOIN batches b ON b.id = n.batch_id WHERE 1=1"
                 + (" AND (b.project_id = ? OR n.project_id = ?)" if project else ""),
                 *([project, project] if project else []))
    waited = [((r["acked_at"] or r["cleared_at"]) - r["created_at"]) / 60 for r in rows
              if (r["acked_at"] or r["cleared_at"])]

    # Refused hours (row 7, I10), by reason
    refused: dict[str, float] = {}
    for r in s.all("SELECT f.reason, f.opened_at, f.cleared_at FROM refusals f LEFT JOIN batches b ON "
                   f"b.id = f.batch_id WHERE 1=1{pf}", *pa):
        end = r["cleared_at"] or clock.wall()
        refused[r["reason"]] = refused.get(r["reason"], 0.0) + (end - r["opened_at"]) / 3600

    return {
        "project": project,
        "batches": n_batches,
        "accepted_steps": len(secs),
        "attempts": len(atts),
        "minutes_per_accepted_step": round(sum(secs) / 60 / len(secs), 2) if secs else None,
        "share_attempts_producing_nothing": round(nothing / len(judged), 3) if judged else None,
        "human_transitions": {"total": len(items), "per_batch": round(len(items) / (n_batches or 1), 2),
                              "max_per_batch": max(per_batch.values()) if per_batch else 0,
                              "items": [{k: i[k] for k in ("batch_id", "entity", "from_state",
                                                            "to_state", "actor", "reason")}
                                        for i in items]},
        "silent_stuck": silent,
        "runs_past_bound_now": past,
        "human_waits": {"open": sum(1 for r in rows if r["state"] == "open"),
                        "cleared": len(waited),
                        "median_minutes_to_clear": round(statistics.median(waited), 1) if waited else None},
        "refused_hours": {k: round(v, 2) for k, v in refused.items()},
        "minutes_to_first_edit_median": round(statistics.median(firsts) / 60, 2) if firsts else None,
        # pace: per attempt, model seconds per turn; per accepted step, turns over its attempts
        "seconds_per_turn_median": _median([tm["model_s"] / tm["turns"] for tm in
                                            (json.loads(x["timings"] or "{}") for x in judged)
                                            if tm.get("turns")]),
        "turns_per_accepted_step_median": _median(_turns_per_accepted(s, project)),
    }


def phase_minutes(s, batch_id: str) -> dict[str, float]:
    """Minutes the batch spent in each driver phase, summed over its runs (§2.9). Events pair
    per run in cursor order: a phase lasts from its `phase.start` to the next `phase.end` of the
    same phase in that run. A start the driver never closed (an exception emits no `phase.end`)
    lasts until the next `phase.start` in that run, else the run's ended_at, else now."""
    evs = s.all("SELECT run_id, kind, payload, at FROM events WHERE batch_id = ? AND kind IN "
                "('phase.start', 'phase.end') ORDER BY cursor", batch_id)
    ended = {r["id"]: r["ended_at"] for r in
             s.all("SELECT id, ended_at FROM runs WHERE batch_id = ?", batch_id)}
    per_run: dict[str | None, list] = {}
    for e in evs:
        per_run.setdefault(e["run_id"], []).append(e)
    secs: dict[str, float] = {}
    for rid, items in per_run.items():
        open_: dict[str, list] = {}      # phase name -> [(start at, index in items)], unmatched
        spans: list[tuple[str, float, float]] = []
        for i, e in enumerate(items):
            name = json.loads(e["payload"] or "{}").get("phase") or ""
            if e["kind"] == "phase.start":
                open_.setdefault(name, []).append((e["at"], i))
            elif open_.get(name):
                start, _ = open_[name].pop(0)          # the next end of the same phase
                spans.append((name, start, e["at"]))
        for name, queue in open_.items():
            for start, i in queue:
                close = next((x["at"] for x in items[i + 1:]
                              if x["kind"] == "phase.start"), None)
                spans.append((name, start, close if close is not None
                              else ended.get(rid) if ended.get(rid) is not None else clock.wall()))
        for name, start, close in spans:
            secs[name] = secs.get(name, 0.0) + (close - start)
    mins = {k: v / 60 for k, v in secs.items()}
    return {k: round(v, 1) for k, v in sorted(mins.items(), key=lambda kv: (-kv[1], kv[0]))}


def _median(xs):
    return round(statistics.median(xs), 1) if xs else None


def _turns_per_accepted(s, project) -> list[float]:
    sql = ("SELECT st.id FROM steps st JOIN batches b ON b.id = st.batch_id WHERE st.status = "
           "'accepted'" + (" AND b.project_id = ?" if project else ""))
    out = []
    for st in s.all(sql, *([project] if project else [])):
        n = sum((json.loads(a["timings"] or "{}").get("turns") or 0)
                for a in s.all("SELECT timings FROM attempts WHERE step_id = ?", st["id"]))
        if n:
            out.append(n)
    return out


def _pace(atts) -> dict:
    turns = model_s = 0.0
    for a in atts:
        tm = json.loads(a["timings"] or "{}")
        turns += tm.get("turns") or 0
        model_s += tm.get("model_s") or 0
    return {"turns": int(turns), "s_per_turn": round(model_s / turns, 1) if turns else None}


def steps(s, batch_id: str) -> dict:
    """Per-step numbers for one batch, the baseline table later batches compare against
    (docs/evidence/2026-10-14-gh-resolve-p5-batch-baseline.md was this, by hand). Minutes are
    worker time summed over every attempt of the step, interrupted ones included."""
    rows = []
    for st in s.all("SELECT st.*, p.slug, p.ord, p.repair_of_step_id, p.verification FROM steps st "
                    "JOIN plans p ON p.id = st.plan_id WHERE st.batch_id = ? ORDER BY p.ord, st.idx",
                    batch_id):
        every = s.all("SELECT * FROM attempts WHERE step_id = ? ORDER BY created_at", st["id"])
        # Minutes keep gates-first runs (comparable with the P5 baseline); attempt and red counts
        # leave them out: a measurement with no worker is not an attempt.
        mins = sum(((a["ended_at"] or clock.wall()) - a["created_at"]) for a in every) / 60
        atts = [a for a in every if not json.loads(a["timings"] or "{}").get("gates_first")]
        firsts = [json.loads(a["timings"] or "{}").get("timings", {}).get("first_edit_s") for a in atts]
        firsts = [f for f in firsts if f is not None]
        carried = s.one("SELECT 1 FROM events WHERE batch_id = ? AND kind = 'phase.start' AND "
                        "payload LIKE ? AND payload LIKE '%\"carried\": true%'", batch_id,
                        f'%"step": "{st["id"]}"%') is not None
        rows.append({"plan": st["slug"], "idx": st["idx"], "title": st["title"],
                     "kind": "repair" if st["repair_of_step_id"] else
                             "verification" if st["verification"] else "work",
                     "status": st["status"], "attempts": len(atts),
                     "red": sum(a["outcome"] == "red" for a in atts),
                     "interrupted": sum(a["outcome"] == "interrupted" for a in atts),
                     "minutes": round(mins, 1),
                     "first_edit_min": round(min(firsts) / 60, 1) if firsts else None,
                     "carried": carried,
                     # W4 (docs/06): the largest prompt any attempt of the step reached
                     "peak_context_k": round(max([json.loads(a["timings"] or "{}").get(
                         "peak_context_tokens") or 0 for a in atts] or [0]) / 1000),
                     # pace (kira-cloudflare-44, Oct 16): model turns over every attempt of the
                     # step, and the model seconds per turn, so plans can be sized against it
                     **_pace(atts)})
    done = [r for r in rows if r["status"] == "accepted"]
    first_try = [r for r in done if r["attempts"] - r["interrupted"] == 1 and r["red"] == 0]
    return {"batch": batch_id, "steps": rows,
            "accepted": len(done), "of": len(rows),
            "minutes_per_accepted_step": round(sum(r["minutes"] for r in done) / len(done), 1) if done else None,
            "first_attempt_share": round(len(first_try) / len(done), 2) if done else None,
            # Driver phases, so time before the first worker is not invisible (Oct 14: a kira
            # batch spent 37 min measuring its base before attempt 1)
            "phase_minutes": phase_minutes(s, batch_id)}


def steps_table(r: dict) -> str:
    """The steps() result as a Markdown table, ready for docs/evidence."""
    out = ["| # | Plan · step | Kind | Status | Attempts | Red | Interrupted | Minutes | First edit | Carried "
           "| Peak context (k tokens) | Turns | s/turn |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, x in enumerate(r["steps"], 1):
        out.append(f"| {i} | {x['plan']} · {x['idx']} {x['title'][:50]} | {x['kind']} | {x['status']} | "
                   f"{x['attempts']} | {x['red']} | {x['interrupted']} | {x['minutes']} | "
                   f"{x['first_edit_min'] if x['first_edit_min'] is not None else ''} | "
                   f"{'yes' if x['carried'] else ''} | {x.get('peak_context_k') or ''} | "
                   f"{x.get('turns') or ''} | {x.get('s_per_turn') or ''} |")
    out.append(f"\n{r['accepted']} of {r['of']} steps accepted; {r['minutes_per_accepted_step']} min per "
               f"accepted step; first-attempt share {r['first_attempt_share']}.")
    phases = sorted(((k, v) for k, v in (r.get("phase_minutes") or {}).items() if v >= 0.05),
                    key=lambda kv: (-kv[1], kv[0]))
    if phases:
        out.append("Phases (min): " + ", ".join(f"{k} {v}" for k, v in phases))
    return "\n".join(out)
