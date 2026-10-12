"""The pinned gate environment (docs/02 §2.5.2).

- HOME is a private directory per run, created empty, plus files the project declares.
- No ILK_* or ILK2_* variable is inherited; a project may set named ones to declared values.
- PATH is the project's declared prelude plus the release's tools, and nothing else.
- Every other variable comes from the project's allow-list; anything not listed is absent.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Variables every process needs to behave; their values come from the host, never ILK_*.
BASELINE = ("LANG", "LC_ALL", "USER", "LOGNAME", "SHELL")


def pinned_env(*, home: Path, tmp: Path, path_prelude: list[str], allow: list[str] | None = None,
               set_vars: dict[str, str] | None = None, home_files: list[str] | None = None,
               release_tools: list[str] | None = None, host: dict[str, str] | None = None) -> dict[str, str]:
    host = dict(os.environ if host is None else host)
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    for rel in home_files or []:
        src = Path(host.get("HOME", "~")).expanduser() / rel
        if src.is_file():
            dst = home / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    env: dict[str, str] = {}
    for k in (*BASELINE, *(allow or [])):
        if k in host and not k.startswith(("ILK_", "ILK2_")):
            env[k] = host[k]
    for k, v in (set_vars or {}).items():
        env[k] = v  # declared values, ILK_* names included (e.g. ILK_TEST_TOOLKIT_ROOT)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp) + "/"
    env["PATH"] = os.pathsep.join([*path_prelude, *(release_tools or [])])
    return env
