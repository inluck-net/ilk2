"""P1 step 4: brief comparison over real V1 steps (Q10, docs/03 §3.4).

Usage: uv run python spikes/p1/run_step4.py [--only S1-D ...]
Needs steps.json (step4_prepare.py) and valid preflight-<id>.json files (step4_preflight.py).

Each run: a fresh worktree at the V1 base, a fresh config dir and session, the step 3
isolation (sandbox + hook), up to 3 attempts (Q6: fresh, resume with red evidence, fresh with
red evidence), 60 min cap per attempt (Q7), then the driver's gate (step4_gate.py).
Two lanes run in parallel, so at most 2 MiMo sessions at once. Order is counterbalanced:
lane A runs S1 D,P then S3 D,P; lane B runs S2 P,D then S4 P,D.

Judgment calls: max 400 turns per attempt (V1 used 99 turns for a 3-line fix, E1); the
preloaded arm also gets the pinned tests' failing output at the base, as docs/02 §2.6 lists
"the failing output". Wrong if attempts hit the turn cap rather than finishing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cli_adapter import CliAdapter, Isolation, make_config_dir, sweep  # noqa: E402
from step4_gate import red_evidence, run_gate  # noqa: E402
from step4_prepare import OUT, SRC  # noqa: E402

ATTEMPT_CAP_S = 60 * 60
MAX_ATTEMPTS = 3
MAX_TURNS = 400

LANES = {
    "A": [("S1", "D"), ("S1", "P"), ("S3", "D"), ("S3", "P")],
    "B": [("S2", "P"), ("S2", "D"), ("S4", "P"), ("S4", "D")],
}

RULES = """You are the worker in an automated build loop, in a git worktree of the ilk-skills
repository checked out at the base commit of this step. Implement **Step 1** of the sub-plan
below, completely, then reply with a short summary of what you changed.

Rules:
- Do not run git commit or git push. The driver commits after its gate passes.
- Run only targeted tests for the files you change (the gate commands below are fine).
  Do not run the full test suite.
- Your sandbox cannot run setuid programs such as `ps`, and can write only inside this
  worktree. A test that needs `ps` may fail for you but pass in the driver's gate.
- Do not weaken the pinned tests to make them pass. Remove their xfail markers, as the
  sub-plan says, and make the code satisfy them.

The driver's gate after you finish:
1. each command below exits 0;
2. every test in `{pinned}` passes with `--runxfail`.

Gate commands:
{commands}

The sub-plan, as of the start of step 1 (the MASTER it links is not available):

{plan}
"""

PRELOAD_TAIL = """

{preload}

## The pinned tests at the base, run with --runxfail (the failing output)
```
{base_out}
```
"""

RESUME = """The driver's gate is red after your attempt. Fix the cause, with the same rules as
before. Gate output:

{evidence}
"""

RETRY_FRESH = """{brief}

## A previous attempt at this step left the gate red

The worktree contains that attempt's changes. Gate output:

{evidence}
"""


def worktree(path: Path, rev: str) -> Path:
    subprocess.run(["git", "-C", str(SRC), "worktree", "add", "-q", "--detach", str(path), rev],
                   check=True, capture_output=True)
    return path


def brief_for(st: dict, arm: str, pre: dict) -> str:
    b = RULES.format(pinned=st["pinned"], commands="\n".join(f"- `{c}`" for c in st["commands"]),
                     plan=st["plan"])
    if arm == "P":
        b += PRELOAD_TAIL.format(preload=st["preload"],
                                 base_out=pre["at_base"]["pinned"]["tail"][-6000:])
    return b


def one_attempt(ad: CliAdapter, wt: Path, prompt: str, resume: str | None) -> tuple[dict, str]:
    att = ad.resume(resume, prompt, wt) if resume else ad.start(prompt, wt)
    killed = {}

    def cap() -> None:
        killed["cancel"] = CliAdapter.cancel(att)

    timer = threading.Timer(ATTEMPT_CAP_S, cap)
    timer.start()
    t0 = time.monotonic()
    try:
        att.wait()
    finally:
        timer.cancel()
    wall = time.monotonic() - t0
    swept = sweep(wt)
    summary = att.summary() | {"wall_s": round(wall, 1), "capped": bool(killed),
                               "cancel": killed.get("cancel"), "swept": swept}
    return summary, att.session_id or ""


def one_run(root: Path, st: dict, arm: str, pre: dict) -> dict:
    d = root / f"{st['id']}-{arm}"
    d.mkdir(mode=0o700)
    wt = worktree(d / "wt", st["base"])
    logs = d / "logs"
    logs.mkdir()
    config = make_config_dir(d / "config", hook=True)
    iso = Isolation(worktree=wt, tmp=d / "tmp", log_dir=logs, config_dir=config)
    ad = CliAdapter(config, logs, max_turns=MAX_TURNS, isolation=iso)
    brief = brief_for(st, arm, pre)
    (logs / "brief.md").write_text(brief)
    run: dict = {"step": st["id"], "arm": arm, "base": st["base"], "brief_chars": len(brief),
                 "attempts": [], "started": datetime.now().isoformat(timespec="seconds")}
    t_run = time.monotonic()
    session, evidence = None, ""
    for n in range(1, MAX_ATTEMPTS + 1):
        if n == 1:
            prompt, resume = brief, None
        elif n == 2:
            prompt, resume = RESUME.format(evidence=evidence), session
        else:
            prompt, resume = RETRY_FRESH.format(brief=brief, evidence=evidence), None
        summary, sid = one_attempt(ad, wt, prompt, resume)
        session = sid or session
        t_gate = time.monotonic()
        gate = run_gate(wt, d / "gate-tmp", st["commands"], st["pinned"], pre["base_ids"],
                        pre["exclude"])
        summary["gate_s"] = round(time.monotonic() - t_gate, 1)
        summary["accepted"] = gate["accepted"]
        summary["gate"] = {"commands": [{k: c[k] for k in ("rc", "secs", "failed")}
                                        for c in gate["commands"]],
                           "pinned_not_passing": gate["pinned"]["not_passing"]}
        run["attempts"].append(summary)
        print(f"  {st['id']}-{arm} attempt {n}: accepted={gate['accepted']} "
              f"wall={summary['wall_s']}s turns={summary['num_turns']} "
              f"thinking={summary['thinking_s']}s capped={summary['capped']}", flush=True)
        (d / "result.json").write_text(json.dumps(run, indent=2, default=str))
        if gate["accepted"]:
            break
        evidence = red_evidence(gate)
    run["accepted"] = run["attempts"][-1]["accepted"]
    run["total_min"] = round((time.monotonic() - t_run) / 60, 2)
    run["worker_min"] = round(sum(a["wall_s"] for a in run["attempts"]) / 60, 2)
    run["diff_stat"] = subprocess.run(["git", "-C", str(wt), "diff", "--shortstat"],
                                      capture_output=True, text=True).stdout.strip()
    (d / "result.json").write_text(json.dumps(run, indent=2, default=str))
    return run


def lane(name: str, items, root: Path, steps: dict, pres: dict, results: list) -> None:
    for sid, arm in items:
        print(f"lane {name}: start {sid}-{arm} {datetime.now():%H:%M:%S}", flush=True)
        try:
            r = one_run(root, steps[sid], arm, pres[sid])
        except Exception:  # noqa: BLE001 - record and keep the other runs going
            r = {"step": sid, "arm": arm, "error": traceback.format_exc()[-3000:]}
        results.append(r)
        print(f"lane {name}: done {sid}-{arm} accepted={r.get('accepted')} "
              f"total_min={r.get('total_min')} attempts={len(r.get('attempts', []))}", flush=True)


def main(argv: list[str]) -> int:
    steps = {s["id"]: s for s in json.loads((OUT / "steps.json").read_text())}
    pres = {}
    for sid in steps:
        p = json.loads((OUT / f"preflight-{sid}.json").read_text())
        assert p["valid"], f"{sid} preflight is not valid"
        pres[sid] = p
    lanes = LANES
    if argv[:1] == ["--only"]:
        want = {tuple(x.split("-")) for x in argv[1:]}
        lanes = {k: [i for i in v if i in want] for k, v in LANES.items()}
    root = OUT / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    root.mkdir(parents=True)
    print("runs in", root, flush=True)
    results: list = []
    threads = [threading.Thread(target=lane, args=(k, v, root, steps, pres, results))
               for k, v in lanes.items() if v]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    (root / "summary.json").write_text(json.dumps(results, indent=2, default=str))
    print("STEP4 DONE", root, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
