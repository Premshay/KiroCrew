"""Durable per-arc board store."""

from __future__ import annotations

import importlib
import re
import threading

import pytest

from kiro_crew import arcs


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _spec(**over):
    spec = {
        "title": "Pilot",
        "goal": "ship the board",
        "reason": "coordination",
        "lineage": "",
        "source": "test",
        "autonomy": 2,
        "milestones": [{"name": "m0", "criteria": "c0"}, {"name": "m1", "criteria": "c1"}],
        "decisions": ["ADR-1"],
        "rules": [],
        "reuse": [],
        "channel_id": "chan",
    }
    spec.update(over)
    return spec


def _arc(*members):
    arc = arcs.create_arc(_spec())
    for key in members:
        arcs.add_member(arc["id"], key, "worker")
    return arc["id"]


def _claim(step="wire preview", **over):
    entry = {"kind": "CLAIM", "text": "taking it", "step": step, "checked": ["ADR-1"]}
    entry.update(over)
    return entry


def test_create_and_read_arc():
    arc = arcs.create_arc(_spec())
    assert re.fullmatch(r"arc_[0-9a-f]{32}", arc["id"])
    assert arc["milestone_index"] == 0
    assert arc["members"] == {}
    assert arcs.read_arc(arc["id"])["title"] == "Pilot"
    assert [a["id"] for a in arcs.list_arcs()] == [arc["id"]]
    with pytest.raises(arcs.ArcNotFound):
        arcs.read_arc("arc_" + "0" * 32)


def test_append_validates_kind_and_caps():
    arc_id = _arc("a")
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", {"kind": "NOTE", "text": "x"})
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", {"kind": "FACT", "text": "x" * 101})
    done = {"kind": "DONE", "text": "x" * 300, "link": "http://e/1"}
    stored = arcs.append_entry(arc_id, "a", done)
    assert re.fullmatch(r"ae_[0-9a-f]{32}", stored["id"])
    assert stored["author"] == "a" and stored["milestone"] == 0 and stored["ts"]
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", {**done, "text": "x" * 301})


def test_claim_requires_step_and_checked():
    arc_id = _arc("a")
    bad = _claim()
    del bad["checked"]
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", bad)
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", _claim(checked=["x"] * 9))
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", _claim(step=""))
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", _claim(step="s" * 101))
    arcs.append_entry(arc_id, "a", _claim())


def test_ask_requires_to_and_done_requires_link():
    arc_id = _arc("a", "b")
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", {"kind": "ASK", "text": "q?"})
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", {"kind": "ASK", "text": "q?", "to": "stranger"})
    arcs.append_entry(arc_id, "a", {"kind": "ASK", "text": "q?", "to": "b"})
    arcs.append_entry(arc_id, "a", {"kind": "ASK", "text": "q?", "to": arcs.OWNER})
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc_id, "a", {"kind": "DONE", "text": "done"})


def test_non_member_cannot_append():
    arc_id = _arc("a")
    with pytest.raises(arcs.ArcAccessDenied):
        arcs.append_entry(arc_id, "stranger", {"kind": "FACT", "text": "hi"})
    assert (
        arcs.append_entry(arc_id, arcs.OWNER, {"kind": "FACT", "text": "hi"})["author"] == "owner"
    )


def test_claim_closes_on_done_reply():
    arc_id = _arc("a", "b")
    claim = arcs.append_entry(arc_id, "a", _claim())
    assert [c["id"] for c in arcs.open_claims(arc_id)] == [claim["id"]]
    arcs.append_entry(
        arc_id, "b", {"kind": "DONE", "text": "x", "link": "l", "reply_to": claim["id"]}
    )
    assert arcs.open_claims(arc_id)
    arcs.append_entry(
        arc_id, "a", {"kind": "DONE", "text": "x", "link": "l", "reply_to": claim["id"]}
    )
    assert arcs.open_claims(arc_id) == []


def test_ask_open_until_reply():
    arc_id = _arc("a", "b")
    ask = arcs.append_entry(arc_id, "a", {"kind": "ASK", "text": "q?", "to": "b"})
    opened = arcs.open_asks(arc_id)
    assert [x["id"] for x in opened] == [ask["id"]]
    assert opened[0]["age_secs"] >= 0
    arcs.append_entry(arc_id, "b", {"kind": "FACT", "text": "a", "reply_to": ask["id"]})
    assert arcs.open_asks(arc_id) == []


def test_grep_or_and():
    arc_id = _arc("a")
    for text in ("Alpha and BETA", "alpha only", "gamma here", "nothing"):
        arcs.append_entry(arc_id, "a", {"kind": "FACT", "text": text})
    hits = [e["text"] for e in arcs.grep_entries(arc_id, "alpha&beta,gamma")]
    assert hits == ["Alpha and BETA", "gamma here"]


def test_unseen_and_mark_seen():
    arc_id = _arc("a", "b")
    first = arcs.append_entry(arc_id, "a", {"kind": "FACT", "text": "one"})
    second = arcs.append_entry(arc_id, "a", {"kind": "FACT", "text": "two"})
    assert [e["id"] for e in arcs.unseen_entries(arc_id, "b")] == [first["id"], second["id"]]
    arcs.mark_seen(arc_id, "b", first["id"])
    assert [e["id"] for e in arcs.unseen_entries(arc_id, "b")] == [second["id"]]
    assert arcs.seen_by(arc_id, first["id"]) == ["b"]
    assert arcs.seen_by(arc_id, second["id"]) == []
    assert [e["id"] for e in arcs.read_entries(arc_id, after=first["id"])] == [second["id"]]


def test_concurrent_appends_keep_both():
    arc_id = _arc("a", "b")

    def post(author):
        for i in range(50):
            arcs.append_entry(arc_id, author, {"kind": "FACT", "text": f"{author}{i}"})

    threads = [threading.Thread(target=post, args=(who,)) for who in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = arcs.read_entries(arc_id, limit=500)
    assert len(entries) == 100
    assert len({e["id"] for e in entries}) == 100


def test_state_survives_reload():
    global arcs
    arc_id = _arc("a", "b")
    arcs.append_entry(arc_id, "a", {"kind": "FACT", "text": "one"})
    ask = arcs.append_entry(arc_id, "a", {"kind": "ASK", "text": "q?", "to": "b"})
    arcs.mark_seen(arc_id, "b", ask["id"])
    arcs = importlib.reload(arcs)
    assert [a["id"] for a in arcs.list_arcs()] == [arc_id]
    assert len(arcs.read_entries(arc_id)) == 2
    assert arcs.seen_by(arc_id, ask["id"]) == ["b"]
    assert [x["id"] for x in arcs.open_asks(arc_id)] == [ask["id"]]


def test_milestone_defaults_and_accept():
    arc_id = _arc("a")
    assert arcs.accept_milestone(arc_id)["milestone_index"] == 1
    assert arcs.append_entry(arc_id, "a", {"kind": "FACT", "text": "x"})["milestone"] == 1
    assert arcs.arcs_for_member("a")[0]["id"] == arc_id
    assert arcs.arcs_for_member("nobody") == []


def test_claim_collisions_normalizes_step():
    arc_id = _arc("a", "b")
    mine = arcs.append_entry(arc_id, "a", _claim("Wire  Preview"))
    candidate = {**_claim("wire preview"), "author": "b"}
    assert [c["id"] for c in arcs.claim_collisions(arc_id, candidate)] == [mine["id"]]
    assert arcs.claim_collisions(arc_id, {**candidate, "author": "a"}) == []
    assert arcs.claim_collisions(arc_id, {**candidate, "kind": "FACT"}) == []
    arcs.append_entry(
        arc_id, "a", {"kind": "DONE", "text": "x", "link": "l", "reply_to": mine["id"]}
    )
    assert arcs.claim_collisions(arc_id, candidate) == []
