"""P1 step 4 analysis: per-run and per-arm numbers for Q10 from a runs directory.

Usage: uv run python spikes/p1/step4_analyze.py [~/.ilk2/spike-p1/step4/runs/<ts>]
Thinking time here is the span covered by consecutive `thinking_tokens` stream events (gaps
under 5 s), which MiMo emits while thinking; the adapter's own thinking_s undercounts because
thinking content arrives in few assistant events.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def thinking_seconds(log: Path, gap: float = 5.0) -> float:
    total, prev = 0.0, None
    for line in log.open():
        rec = json.loads(line)
        ev = rec["ev"]
        if ev.get("type") == "system" and ev.get("subtype") == "thinking_tokens":
            if prev is not None and rec["t"] - prev < gap:
                total += rec["t"] - prev
            prev = rec["t"]
        else:
            prev = None if ev.get("type") in ("assistant", "user") else prev
    return total


def main(root: Path) -> dict:
    runs = []
    for d in sorted(root.iterdir()):
        res = d / "result.json"
        if not res.exists():
            continue
        r = json.loads(res.read_text())
        think = sum(thinking_seconds(f) for f in sorted((d / "logs").glob("attempt-*.jsonl"))
                    if not f.name.endswith(".stderr.txt"))
        first = r["attempts"][0]
        runs.append({
            "run": d.name, "step": r["step"], "arm": r["arm"], "accepted": r.get("accepted"),
            "attempts": len(r["attempts"]), "total_min": r.get("total_min"),
            "worker_min": r.get("worker_min"),
            "gate_min": round(sum(a.get("gate_s", 0) for a in r["attempts"]) / 60, 2),
            "first_edit_min": round(first["timings"].get("first_edit_s", 0) / 60, 2),
            "turns": sum(a.get("num_turns") or 0 for a in r["attempts"]),
            "thinking_min": round(think / 60, 2), "brief_chars": r["brief_chars"],
            "started": r["started"],
        })
    arms = {}
    for arm in ("D", "P"):
        xs = [x for x in runs if x["arm"] == arm]
        acc = [x for x in xs if x["accepted"]]
        arms[arm] = {
            "runs": len(xs), "accepted": len(acc),
            "attempts": sum(x["attempts"] for x in xs),
            "min_per_accepted_step": round(sum(x["total_min"] or 0 for x in xs) / len(acc), 2) if acc else None,
            "median_first_edit_min": statistics.median([x["first_edit_min"] for x in xs]) if xs else None,
            "median_thinking_min": statistics.median([x["thinking_min"] for x in xs]) if xs else None,
        }
    paired = []
    for st in sorted({x["step"] for x in runs}):
        d = next((x for x in runs if x["step"] == st and x["arm"] == "D"), None)
        p = next((x for x in runs if x["step"] == st and x["arm"] == "P"), None)
        if d and p:
            first = "D" if d["started"] < p["started"] else "P"
            paired.append({"step": st, "D_min": d["total_min"], "P_min": p["total_min"],
                           "P_minus_D": round((p["total_min"] or 0) - (d["total_min"] or 0), 2),
                           "ran_first": first})
    return {"runs": runs, "arms": arms, "paired": paired}


if __name__ == "__main__":
    root = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else \
        sorted((Path.home() / ".ilk2/spike-p1/step4/runs").iterdir())[-1]
    print(json.dumps(main(root), indent=2))
