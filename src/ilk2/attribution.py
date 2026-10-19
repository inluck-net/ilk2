"""Attribution of a new failure to the step that introduced it (docs/02 §2.5.7; suite A1-A10).

A failure that a step's gate shows, and that was already failing at the step's parent, was
introduced earlier. The driver walks backwards from the parent through the batch's commits,
newest first, to the first commit where the failure is absent. That commit and its child are
the edge; the child's step introduced it. Walking backwards (never bisecting) finds the most
recent clean-to-failing edge (A9). Both sides are re-probed twice more; any disagreement is
`flaky` (A4). Probes run in diagnostic worktrees, so the execution root is untouched (A3).

Results: attributed | base (still failing at the batch base: judged against the current
step) | current (the parent fails differently: this step changed it) | spec-changed | flaky |
budget | foreign (the failing side is not an ilk2 step).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from . import bounds, clock, gates, gitops
from .gates import vitest
from .plans import Gate

if TYPE_CHECKING:
    from .driver import Driver


@dataclass
class Attribution:
    test_id: str
    fingerprint: str
    result: str
    edge: tuple[str, str] | None = None    # (last clean commit, first failing commit)
    step_id: str | None = None
    probes: int = 0                        # probe runs this call counted against the budget
    recurring: bool = False                # its repair already shipped and it failed again

    def to_dict(self) -> dict:
        return {"result": self.result, "edge": list(self.edge) if self.edge else None,
                "step": self.step_id, "probes": self.probes}


class Attributor:
    """One per attribute phase: the wall-time budget covers the whole phase (§2.5.7)."""

    def __init__(self, drv: Driver):
        self.d = drv
        self.s = drv.s
        self.deadline = clock.mono() + drv.b.attribution_wall

    def chain(self, parent: str) -> list[str]:
        """The parent, then each earlier commit of the batch down to the base, newest first."""
        newer = gitops.git(self.d.root, "rev-list", "--first-parent",
                           f"{self.d.base}..{parent}").split()
        return newer + [self.d.base]

    def attribute(self, gate: Gate, spec_now: str, test_id: str, fingerprint: str,
                  parent: str) -> Attribution:
        a = Attribution(test_id, fingerprint, "base")
        test_file = test_id.split("::")[0]
        chain = self.chain(parent)
        # The parent probe matched the id only; the walk follows this exact failure (id and
        # fingerprint). If the parent fails differently, the current step changed it.
        at_parent = self.probe(gate, spec_now, parent, test_id, fingerprint, a)
        if at_parent is None:
            return a
        if not at_parent:
            a.result = "current"
            return a
        failing = chain[0]
        for older in chain[1:]:
            if not self.portable(gate, spec_now, older, test_file):
                a.result = "spec-changed"  # A2: no overlay of new config onto old source
                return a
            present = self.probe(gate, spec_now, older, test_id, fingerprint, a)
            if present is None:
                return a  # budget or unmeasured: the result is already set
            if present:
                failing = older
                continue
            # Confirm the edge: absent on the clean side and present on the failing side,
            # twice more each, never from the cache.
            for _ in range(2):
                for commit, want in ((older, False), (failing, True)):
                    got = self.probe(gate, spec_now, commit, test_id, fingerprint, a, cached=False)
                    if got is None:
                        return a
                    if got != want:
                        a.result = "flaky"
                        return a
            a.edge = (older, failing)
            step = self.s.one("SELECT id FROM steps WHERE batch_id = ? AND accepted_commit = ?",
                              self.d.batch_id, failing)
            a.result, a.step_id = ("attributed", step["id"]) if step else ("foreign", None)
            return a
        return a  # still failing at the base: a base failure whose fingerprint changed

    def portable(self, gate: Gate, spec_now: str, commit: str, test_file: str) -> bool:
        where = self.d.diag_worktree(commit)
        if not (where / gate.cwd / test_file).exists():
            return False
        return gates.spec_version(gate, where, self.d.env()) == spec_now

    def probe(self, gate: Gate, spec: str, commit: str, test_id: str, fingerprint: str,
              a: Attribution, cached: bool = True) -> bool | None:
        """True if the failure (same id and fingerprint) is present at commit. None when the
        budget is spent or the probe cannot measure; `a.result` then says which."""
        if cached:
            row = self.s.one("SELECT failing, fingerprint FROM probe_cache WHERE batch_id = ? AND "
                             "commit_sha = ? AND spec_version = ? AND test_id = ?",
                             self.d.batch_id, commit, spec, test_id)
            if row:  # A6: reused, not repeated, not counted
                self.d.event("attribution.probe", commit=commit, test=test_id, cached=True,
                             present=bool(row["failing"]) and row["fingerprint"] == fingerprint)
                return bool(row["failing"]) and row["fingerprint"] == fingerprint
        if a.probes >= bounds.PROBES_PER_FAILURE or clock.mono() > self.deadline:
            a.result = "budget"
            return None
        a.probes += 1
        where = self.d.diag_worktree(commit)
        if gate.adapter == "vitest":
            cmd = vitest.with_test_ids(gate.command, [test_id])
        else:
            cmd = gates.with_test_ids(gate.command, [test_id], drop=("--deselect", "-k"))
        g = replace(gate, name=f"{gate.name}-probe", command=cmd)  # keeps the gate's env
        r = gates.measure(g, where, self.d.env(), self.d.dir / "gates")
        if r.outcome == "unmeasured":
            a.result = "flaky"  # a probe that cannot measure cannot confirm an edge
            return None
        f = r.failures.get(test_id)
        present = f is not None and f.fingerprint == fingerprint
        with self.s.tx() as c:
            c.execute("INSERT OR REPLACE INTO probe_cache(batch_id, commit_sha, spec_version, test_id, "
                      "failing, fingerprint, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (self.d.batch_id, commit, spec, test_id, int(f is not None),
                       f.fingerprint if f else None, clock.wall()))
        self.d.event("attribution.probe", commit=commit, test=test_id, cached=False, present=present)
        return present


def repair_brief(s, root, a: Attribution, gate: Gate, message: str) -> str:
    clean, failing = a.edge
    step = s.get("steps", a.step_id)
    diff = gitops.git(root, "diff", f"{clean}..{failing}", check=False)[:30_000]
    return (f"An earlier accepted step broke a test, and later steps found it. Repair it.\n\n"
            f"- Failing test: {a.test_id}\n- Failure: {message}\n"
            f"- Introduced by step {a.step_id} ({step['title']!r}): the test passes at "
            f"{clean[:12]} and fails at {failing[:12]}.\n"
            f"- Gate: {' '.join(gate.command)}\n\n"
            "Fix the regression at the branch head without undoing the later steps' work. "
            "Run only that test while you work.\n\n"
            f"The change that introduced it:\n```diff\n{diff}\n```\n")

