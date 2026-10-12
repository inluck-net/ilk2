"""Where ilk2 keeps things. ILK2_HOME overrides ~/.ilk2 (tests and the stall suite)."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("ILK2_HOME") or Path.home() / ".ilk2")


def data() -> Path:
    return home() / "data"


def db() -> Path:
    return data() / "state.db"


def worktrees() -> Path:
    return data() / "worktrees"


def diag(batch_id: str) -> Path:
    return data() / "diag" / batch_id


def run_dir(run_id: str) -> Path:
    return data() / "runs" / run_id


def logs() -> Path:
    return data() / "logs"


def ensure() -> None:
    for p in (data(), worktrees(), data() / "diag", data() / "runs", logs()):
        p.mkdir(parents=True, exist_ok=True)
