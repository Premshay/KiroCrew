"""Attributed claim resolution and explicit merge linking."""

from __future__ import annotations

import pytest

from kiro_crew import arc_github, arc_metrics, arcs

MEMBER = "chat-1-1"
COORDINATOR = "chat-2-2"
PEER = "chat-3-3"


@pytest.fixture()
def arc(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    made = arcs.create_arc(
        {"title": "Pilot", "repo": "o/r", "milestones": [{"name": "M1"}]},
        members=[
            {"session_key": MEMBER, "role": "Tenon"},
            {"session_key": COORDINATOR, "role": "Baton"},
            {"session_key": PEER, "role": "Review"},
        ],
    )
    return arcs.update_settings(made["id"], coordinator=COORDINATOR)


def claim(arc, **over):
    return arcs.append_entry(
        arc["id"],
        MEMBER,
        {"kind": "CLAIM", "text": "implement", "step": "schema", "checked": ["ADR"], **over},
    )


def receipt(arc, **over):
    return arcs.record_merged_pr(
        arc["id"],
        {
            "number": 7,
            "title": "schema",
            "url": "https://github.com/o/r/pull/7",
            "lane": MEMBER,
            **over,
        },
    )


@pytest.mark.parametrize("author", [arcs.OWNER, COORDINATOR])
@pytest.mark.parametrize("resolution", ["completed", "superseded", "abandoned"])
def test_authorized_resolution_keeps_authorship_and_milestone(arc, author, resolution):
    c = claim(arc)
    e = arcs.append_entry(
        arc["id"],
        author,
        {
            "kind": "FACT",
            "text": "work replaced",
            "reply_to": c["id"][:11],
            "resolution": resolution,
        },
    )
    assert e["author"] == author
    assert arcs.read_entries(arc["id"])[0] == c
    assert arcs.open_claims(arc["id"]) == []
    assert arcs.read_arc(arc["id"])["milestone_index"] == 0


def test_member_cannot_resolve_another_claim(arc):
    c = claim(arc)
    before = arcs.read_entries(arc["id"])
    with pytest.raises(arcs.ArcAccessDenied):
        arcs.append_entry(
            arc["id"],
            PEER,
            {"kind": "FACT", "text": "stale", "reply_to": c["id"], "resolution": "abandoned"},
        )
    assert arcs.read_entries(arc["id"]) == before
    assert arcs.open_claims(arc["id"]) == [c]


def test_resolution_must_target_claim_and_include_reason(arc):
    fact = arcs.append_entry(arc["id"], MEMBER, {"kind": "FACT", "text": "note"})
    for extra in ({"reply_to": fact["id"]}, {"reply_to": fact["id"], "text": ""}, {}):
        with pytest.raises(arcs.ArcError):
            arcs.append_entry(
                arc["id"],
                COORDINATOR,
                {"kind": "FACT", "text": "stale", "resolution": "abandoned", **extra},
            )


@pytest.mark.parametrize("mode", ["claim", "step"])
def test_explicit_merge_closes_implementation_once(arc, mode):
    c = claim(arc)
    r = receipt(arc, **{mode: c["id"][:11] if mode == "claim" else " SCHEMA "})
    assert r["author"] == arcs.GITHUB
    assert r["reply_to"] == c["id"]
    assert arcs.open_claims(arc["id"]) == []
    assert receipt(arc, claim=c["id"]) is None
    assert arcs.read_arc(arc["id"])["milestone_index"] == 0


def test_merge_does_not_infer_claim_from_title(arc):
    c = claim(arc)
    receipt(arc)
    assert arcs.open_claims(arc["id"]) == [c]


def test_merge_does_not_close_review_or_ambiguous_steps(arc):
    review = claim(arc, work_type="review")
    receipt(arc, claim=review["id"])
    assert arcs.open_claims(arc["id"]) == [review]
    implementation = claim(arc)
    receipt(arc, number=8, step="schema")
    assert arcs.open_claims(arc["id"]) == [review, implementation]


def test_merge_step_requires_same_lane(arc):
    c = claim(arc)
    receipt(arc, lane=PEER, step="schema")
    assert arcs.open_claims(arc["id"]) == [c]


def test_collision_count_uses_authorized_closure(arc):
    c = claim(arc)
    arcs.append_entry(
        arc["id"],
        COORDINATOR,
        {"kind": "FACT", "text": "replaced", "reply_to": c["id"], "resolution": "superseded"},
    )
    arcs.append_entry(
        arc["id"], PEER, {"kind": "CLAIM", "text": "now work", "step": "schema", "checked": ["ADR"]}
    )
    assert arc_metrics.collision_count(arcs.read_entries(arc["id"])) == 0


def test_sync_reads_explicit_claim_link(arc):
    c = claim(arc)

    def gh(args):
        if args[:2] == ["pr", "list"]:
            return [
                {
                    "number": 7,
                    "title": "schema",
                    "url": "https://github.com/o/r/pull/7",
                    "body": f"Arc: {arc['id']}\nArc-Claim: {c['id']}",
                    "headRefName": "tenon/schema",
                }
            ]
        return {"headRefOid": "a" * 40, "mergeCommit": {"oid": "b" * 40}, "comments": []}

    recorded = arc_github.sync_arc(arc["id"], gh=gh)
    assert recorded[0]["reply_to"] == c["id"]
    assert arcs.open_claims(arc["id"]) == []


def test_duplicate_or_malformed_link_lines_are_not_used():
    assert arc_github.claim_link("Arc-Claim: ae_12345678\nArc-Claim: ae_87654321") == {}
    assert arc_github.claim_link("Arc-Claim: ae_12345678\nArc-Step: schema") == {}
    assert arc_github.claim_link("Arc-Claim: ae_") == {}
