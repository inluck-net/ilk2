"""Delivering a batch as a pull request (docs/10 §10.3): ilk2 records and tracks the request
through a consumer-neutral protocol; a deliverer opts in and does the push. ilk2 never pushes."""

from __future__ import annotations

import json

import pytest

from conftest import one_step

from ilk2 import batches, cli, clock, delivery

GOOD = {"write": "calc.py", "text": "def add(a, b):\n    return a + b\n"}


def call(capsys, *argv):
    capsys.readouterr()
    rc = cli.main(list(argv))
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip().startswith("{") else out


def shipped(ilk, deliverer="dlv", **kw):
    ilk.make_repo()
    ilk.project(deliverer=deliverer, **kw)
    ilk.script([GOOD])
    bid = ilk.submit(one_step(), deliver="pr")
    assert delivery.get(ilk.s, bid)["state"] == "waiting"
    assert ilk.drive_inprocess(bid) == "shipped"
    return bid


def test_submit_refuses_a_delivery_nobody_will_serve(ilk):
    ilk.make_repo()
    ilk.project()                                               # no deliverer
    with pytest.raises(batches.SubmitRejected, match="needs the project's deliverer setting"):
        ilk.submit(one_step(), deliver="pr")
    with pytest.raises(batches.SubmitRejected, match="need --deliver pr"):
        ilk.submit(one_step(), deliver_draft=True)
    with pytest.raises(batches.SubmitRejected, match="need --deliver pr"):
        ilk.submit(one_step(), deliver_base="dev")
    with pytest.raises(batches.SubmitRejected, match="--deliver kind"):
        ilk.submit(one_step(), deliver="email")
    bid = ilk.submit(one_step())
    assert delivery.get(ilk.s, bid) is None


def test_a_shipped_batch_owes_its_delivery_and_says_so_everywhere(ilk, capsys):
    bid = shipped(ilk)
    d = delivery.get(ilk.s, bid)
    assert {k: d[k] for k in ("kind", "base", "draft", "deliverer", "state", "pr_url", "claimed_by")} == \
        {"kind": "pr", "base": "main", "draft": False, "deliverer": "dlv", "state": "owed",
         "pr_url": None, "claimed_by": None}
    ev = [e for e in ilk.s.events_since(0, bid) if e["kind"] == "batch.shipped"][0]
    assert ev["payload"]["delivery"]["state"] == "owed"
    rc, st = call(capsys, "status", "--batch", bid, "--json")
    assert rc == 0 and st["batches"][0]["delivery"]["state"] == "owed"
    rc, ls = call(capsys, "deliver", "list", "--by", "dlv")
    row = ls["deliveries"][0]
    assert rc == 0 and row["batch"] == bid and row["delivery"]["state"] == "owed"
    assert set(row) == {"batch", "project", "branch", "execution_root", "accepted_head",
                        "root_lease", "delivery"}
    assert set(row["root_lease"]) == {"holder", "generation"}
    assert call(capsys, "deliver", "list", "--by", "other")[1]["deliveries"] == []


def test_one_claim_wins_and_only_the_claimant_closes_it(ilk, capsys):
    bid = shipped(ilk)
    rc, out = call(capsys, "deliver", "claim", "--batch", bid, "--by", "nobody")
    assert rc == 3 and "not the deliverer" in out["detail"]
    rc, out = call(capsys, "deliver", "claim", "--batch", bid, "--by", "dlv")
    assert rc == 0 and out["delivery"]["state"] == "claimed" and out["delivery"]["claimed_by"] == "dlv"
    rc, out = call(capsys, "deliver", "claim", "--batch", bid, "--by", "dlv")   # a second pass
    assert rc == 3 and "claimed" in out["detail"]
    rc, out = call(capsys, "deliver", "done", "--batch", bid, "--by", "nobody", "--pr", "https://x/1")
    assert rc == 3
    rc, out = call(capsys, "deliver", "done", "--batch", bid, "--by", "dlv", "--pr", "ftp://x/1")
    assert rc == 2
    rc, out = call(capsys, "deliver", "done", "--batch", bid, "--by", "dlv", "--pr", "https://x/1")
    assert rc == 0 and out["delivery"]["state"] == "done" and out["delivery"]["pr_url"] == "https://x/1"
    assert call(capsys, "deliver", "list", "--by", "dlv")[1]["deliveries"] == []
    rc, out = call(capsys, "deliver", "claim", "--batch", "b-nope", "--by", "dlv")
    assert rc == 2


def test_a_failed_delivery_pages_a_person_and_retry_requeues_it(ilk, capsys):
    bid = shipped(ilk)
    call(capsys, "deliver", "claim", "--batch", bid, "--by", "dlv")
    rc, out = call(capsys, "deliver", "failed", "--batch", bid, "--by", "dlv", "--detail", "pre-push red")
    assert rc == 0 and out["delivery"]["state"] == "failed" and out["delivery"]["detail"] == "pre-push red"
    rows = ilk.s.open_human_rows(bid)
    assert [r["reason"] for r in rows] == ["delivery-failed"] and "pre-push red" in rows[0]["detail"]
    assert call(capsys, "deliver", "retry", "--batch", bid)[0] == 0
    assert delivery.get(ilk.s, bid)["state"] == "owed" and ilk.s.open_human_rows(bid) == []
    assert call(capsys, "deliver", "retry", "--batch", bid)[0] == 3       # only from failed


def test_both_bounds_hold(ilk, monkeypatch):
    bid = shipped(ilk, deliver_owed_after_s=60, deliver_claim_max_s=120)
    t0 = clock.wall()
    delivery.tick(ilk.s, t0 + 30)
    assert ilk.s.open_human_rows(bid) == []
    delivery.tick(ilk.s, t0 + 61)                               # owed and unclaimed too long
    assert [r["reason"] for r in ilk.s.open_human_rows(bid)] == ["delivery-owed"]
    delivery.claim(ilk.s, bid, "dlv")
    assert ilk.s.open_human_rows(bid) == []                     # the claim clears it
    monkeypatch.setattr(delivery.clock, "wall", lambda: t0 + 300)
    delivery.tick(ilk.s, t0 + 300)                              # a claim held past its bound
    d = delivery.get(ilk.s, bid)
    assert d["state"] == "failed" and "claimed by dlv for more than 120 s" in d["detail"]
    assert [r["reason"] for r in ilk.s.open_human_rows(bid)] == ["delivery-failed"]


def test_a_cancelled_batch_drops_its_request(ilk):
    ilk.make_repo()
    ilk.project(deliverer="dlv")
    bid = ilk.submit(one_step(), deliver="pr", deliver_base="dev", deliver_draft=True)
    d = delivery.get(ilk.s, bid)
    assert (d["base"], d["draft"]) == ("dev", True)
    batches.cancel(ilk.s, bid, "stand-down")
    assert delivery.get(ilk.s, bid)["state"] == "dropped"
    assert delivery.listing(ilk.s, "dlv", None) == []
