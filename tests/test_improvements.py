"""Improvement intake (docs/07 §7.4.3, D7, D14): deduplicated, journalled, and reaching RSI only
when a person accepts a row, ranked below every other RSI input."""

from __future__ import annotations

import json

import pytest

from ilk2 import cli, improvements as imp, rsi


def test_a_finding_dedups_by_fingerprint_and_keeps_its_evidence(ilk):
    ilk.make_repo()
    ilk.project()
    a = imp.add(ilk.s, "proj", "postmortem", "timeout-bound:big-step", "steps hit the cap", {"batch": "b-1"})
    b = imp.add(ilk.s, "proj", "postmortem", "timeout-bound:big-step", "steps hit the cap", {"batch": "b-2"})
    assert a["new"] and not b["new"] and a["id"] == b["id"] and b["hits"] == 2
    row = imp.rows(ilk.s, "proj")[0]
    assert [e["batch"] for e in row["evidence"]] == ["b-1", "b-2"] and row["status"] == "open"
    journal = ilk.s.all("SELECT actor, reason FROM transitions WHERE entity = 'improvements'")
    assert [j["reason"] for j in journal] == ["added from postmortem", "seen again from postmortem"]


def test_bad_input_is_refused(ilk):
    ilk.make_repo()
    ilk.project()
    for args in [("nope", "postmortem", "f", "t"), ("proj", "guess", "f", "t"), ("proj", "review", "", "t")]:
        with pytest.raises(imp.IntakeError):
            imp.add(ilk.s, *args, {})


def test_only_accepted_ilk2_rows_reach_rsi_below_every_other_input(ilk):
    ilk.make_repo()
    ilk.project()
    from ilk2 import batches
    batches.project_add(ilk.s, rsi.PROJECT, ilk.repo, batches.ProjectConfig())
    mine = imp.add(ilk.s, rsi.PROJECT, "postmortem", "report-misses-writing", "first edit counts writing", {})
    imp.add(ilk.s, "proj", "postmortem", "other-project", "not ilk2's", {})
    assert not [c for c in rsi.find(ilk.s) if c.source == "improvement"]          # open: not yet
    imp.set_status(ilk.s, mine["id"], "accepted", "human", "worth fixing")
    got = [c for c in rsi.find(ilk.s) if c.source == "improvement"]
    assert [c.title for c in got] == ["first edit counts writing"]
    assert rsi.WEIGHTS["improvement"] < min(v for k, v in rsi.WEIGHTS.items() if k != "improvement")


def test_the_command_adds_lists_and_sets_with_a_reason(ilk, capsys):
    ilk.make_repo()
    ilk.project()
    assert cli.main(["improvement", "add", "--project", "proj", "--source", "review",
                     "--fingerprint", "f1", "--title", "a finding", "--evidence", '{"pr": 1}']) == 0
    rid = json.loads(capsys.readouterr().out)["id"]
    assert cli.main(["improvement", "set", "--id", rid, "--status", "accepted"]) == 2   # no reason
    capsys.readouterr()
    assert cli.main(["improvement", "set", "--id", rid, "--status", "accepted", "--reason", "agreed"]) == 0
    capsys.readouterr()
    assert cli.main(["improvement", "list", "--project", "proj", "--status", "accepted"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)["improvements"]] == [rid]
