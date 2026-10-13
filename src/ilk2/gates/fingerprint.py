"""Failure fingerprints (docs/02 §2.5.4).

A fingerprint hashes the failure type, the message with only recognized volatile values
replaced, and the top three normalized frames inside the project. Assertion operands are kept,
so "expected 1, got 2" and "expected 3, got 4" differ (A8); line numbers and temp paths do not
change a fingerprint. Timeout-class failures are fingerprinted by class only (§2.5.5).
"""

from __future__ import annotations

import hashlib
import re

# Part of every gate's spec_version: a change here makes recorded base measurements and probe
# results incomparable, so they are measured again rather than compared across rules.
FINGERPRINT_VERSION = 2

_VOLATILE = [
    # ilk2's own dirs (a gate's pinned TMPDIR is ~/.ilk2/data/runs/<run>/tmp, diag worktrees and
    # execution roots live under ~/.ilk2/data): a path there differs per run. gh-resolve P5
    # batch, Oct 13: "/Users/chad/.ilk2/data/runs/r-54fa3e3a58a7/tmp/pytest-of-chad/..." kept its
    # run id, so a failure already recorded at the base read as new in every later run.
    (re.compile(r"/[^\s'\"]*/\.ilk2/[^\s'\"),\]]*"), "<ilk2>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b1[5-9]\d{8}(?:\.\d+)?\b"), "<epoch>"),
    # Absolute paths pytest truncated ("/private/var/f...tmp/tmpzt9d"): where the cut falls
    # depends on the path's length, so the whole token is volatile.
    (re.compile(r"/[^\s'\"]*\.\.\.[^\s'\"),\]]*"), "<path>"),
    (re.compile(r"(?:/private)?/(?:var|tmp)/[^\s'\"),\]]*"), "<tmp>"),
    (re.compile(r"/[^\s'\"]*pytest-of-[^\s'\"]*"), "<tmp>"),
    (re.compile(r"\b0x[0-9a-fA-F]{6,}\b"), "<addr>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    # pytest truncates long reprs ("6be3a945-39d...-43ca1a592438"): UUID fragments.
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{1,4}(?:-[0-9a-f]{0,4})*(?=\.\.\.)"), "<uuid>"),
    (re.compile(r"(?<=\.\.\.)-?(?:[0-9a-f]{0,4}-)*[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"\b(?:pid|PID)[ =:]+\d+"), "pid <pid>"),
    (re.compile(r"(?:port|localhost:|127\.0\.0\.1:)\s*\d{2,5}\b"), "port <port>"),
]

TIMEOUT_MARKERS = ("Timeout >", "Failed: Timeout", "Test timed out", "TimeoutError: Test timed out")


def normalize_message(msg: str, root: str | None = None) -> str:
    out = msg or ""
    if root:  # the base is measured in a diag worktree, a step in the execution root
        out = out.replace(root.rstrip("/"), "<root>")
    for rx, rep in _VOLATILE:
        out = rx.sub(rep, out)
    return re.sub(r"\s+", " ", out).strip()[:2000]


def is_timeout_class(message: str) -> bool:
    return any(m in (message or "") for m in TIMEOUT_MARKERS)


def project_frames(frames: list[dict], root: str | None = None, n: int = 3) -> list[str]:
    """path:function for the top n frames inside the project (no line numbers)."""
    out = []
    for f in frames:
        path = f.get("path") or ""
        if "site-packages" in path or path.startswith("<"):
            continue
        if root and path.startswith(root):
            path = path[len(root):].lstrip("/")
        out.append(f"{path}:{f.get('message') or f.get('function') or ''}")
        if len(out) == n:
            break
    return out


def fingerprint(ftype: str | None, message: str, frames: list[dict], root: str | None = None) -> str:
    if is_timeout_class(message):
        basis = "timeout-class"
    else:
        basis = "\x1f".join([ftype or "", normalize_message(message, root),
                              *project_frames(frames, root)])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]
