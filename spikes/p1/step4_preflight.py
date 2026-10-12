"""P1 step 4 preflight: each step's gate must be red at its V1 base and green at the real V1 commit.

Usage: uv run python spikes/p1/step4_preflight.py [S1 S2 ...]
Model-free. Proves the gate environment before any MiMo time is spent. Writes
~/.ilk2/spike-p1/step4/preflight-<id>.json, including the pinned test ids at the base.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from step4_gate import pinned_ids, run_gate  # noqa: E402
from step4_prepare import OUT, SRC  # noqa: E402


def worktree(path: Path, rev: str) -> Path:
    if path.exists():
        subprocess.run(["git", "-C", str(SRC), "worktree", "remove", "--force", str(path)],
                       capture_output=True)
        shutil.rmtree(path, ignore_errors=True)
    subprocess.run(["git", "-C", str(SRC), "worktree", "add", "-q", "--detach", str(path), rev],
                   check=True, capture_output=True)
    return path


def main(ids: list[str]) -> int:
    steps = json.loads((OUT / "steps.json").read_text())
    ok = True
    for st in steps:
        if ids and st["id"] not in ids:
            continue
        d = OUT / "preflight" / st["id"]
        base_wt = worktree(d / "base", st["base"])
        ids_base = pinned_ids(base_wt, d / "tmp", st["pinned"])
        real_wt = worktree(d / "real", st["reference"])
        first = run_gate(real_wt, d / "tmp", st["commands"], st["pinned"], ids_base)
        exclude = sorted({t for c in first["commands"] for t in c["failed"]})
        pinned_hit = [t for t in exclude if t.split("::")[0] == st["pinned"]]
        at_real = run_gate(real_wt, d / "tmp", st["commands"], st["pinned"], ids_base, exclude) \
            if exclude else first
        at_base = run_gate(base_wt, d / "tmp", st["commands"], st["pinned"], ids_base, exclude)
        out = {"base_ids": ids_base, "exclude": exclude, "pinned_excluded": pinned_hit,
               "at_base": at_base, "at_real": at_real,
               "valid": bool(ids_base) and not pinned_hit and not at_base["accepted"]
                        and at_real["accepted"]}
        (OUT / f"preflight-{st['id']}.json").write_text(json.dumps(out, indent=2))
        ok &= out["valid"]
        print(st["id"], "pinned ids", len(ids_base), "excluded", exclude,
              "| base: accepted", at_base["accepted"], [c["rc"] for c in at_base["commands"]],
              "pinned rc", at_base["pinned"]["rc"],
              "| real: accepted", at_real["accepted"], [c["rc"] for c in at_real["commands"]],
              "pinned rc", at_real["pinned"]["rc"], "not passing", at_real["pinned"]["not_passing"][:5],
              "| secs", [c["secs"] for c in at_real["commands"]], "| VALID" if out["valid"] else "| INVALID",
              flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
