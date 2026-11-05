"""Arc v2: merged arc PRs from GitHub, coordinator sign-off, role targets, short ids."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import arc_brief, arc_github, arcs

TENON = "chat-1-1"
ATELIER = "chat-2-2"
BATON = "chat-3-3"
REPO = "o/r"
HEAD = "c4978e364f41029eccefed06f64fb420cfb3e486"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _arc(**over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "title": "Pilot",
        "repo": REPO,
        "milestones": [{"name": "M1"}, {"name": "M2"}],
    }
    spec.update(over)
    return arcs.create_arc(
        spec,
        members=[
            {"session_key": TENON, "role": "Tenon"},
            {"session_key": ATELIER, "role": "Atelier (timetable)"},
            {"session_key": BATON, "role": "Baton"},
        ],
    )


def _marker(reviewer: str, head: str, verdict: str = "clean") -> dict[str, str]:
    return {
        "body": f"<!-- ke-review head={head} route=session reviewer={reviewer} "
        f"by={reviewer} depth=full verdict={verdict} --> **Review**"
    }


class FakeGh:
    def __init__(self, arc_id: str) -> None:
        tag = arcs.arc_tag(arc_id)
        self.listed = [
            {
                "number": 11,
                "title": "M2 slice 1",
                "url": "https://github.com/o/r/pull/11",
                "body": f"### Goal\nslice 1\n\n{tag}\n",
                "headRefName": "tenon/m2-slice-1",
                "mergedAt": "2026-11-03T10:00:00Z",
            },
            {
                "number": 12,
                "title": "Mentions the arc only in prose",
                "url": "https://github.com/o/r/pull/12",
                "body": f"related to {tag} somehow",
                "headRefName": "atelier/x",
                "mergedAt": "2026-11-03T11:00:00Z",
            },
            {
                "number": 13,
                "title": "Atelier walk fix",
                "url": "https://github.com/o/r/pull/13",
                "body": f"**{tag}**",
                "headRefName": "atelier/walk-fix",
                "mergedAt": "2026-11-03T12:00:00Z",
            },
        ]
        self.views = {
            11: {
                "headRefOid": HEAD,
                "mergeCommit": {"oid": "040eaad231"},
                "comments": [
                    _marker("assay", "5db821035b"),  # an earlier head: does not count
                    _marker("derrick", HEAD[:10], verdict="changes"),
                    _marker("assay", HEAD),
                    _marker("derrick", HEAD[:10]),
                ],
            },
            # Reviewed clean only on an earlier head, then pushed again: not verified.
            13: {
                "headRefOid": "aaaaaaaaaa11",
                "mergeCommit": {"oid": "bb"},
                "comments": [_marker("assay", "9999999999")],
            },
        }
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> Any:
        self.calls.append(args)
        if args[:2] == ["pr", "list"]:
            return self.listed
        return self.views[int(args[2])]


def test_sync_records_tagged_merged_prs_once_with_reviewed_head():
    arc = _arc()
    gh = FakeGh(arc["id"])

    recorded = arc_github.sync_arc(arc["id"], gh=gh)

    assert [e["pr"] for e in recorded] == [11, 13]
    first, second = recorded
    assert first["author"] == arcs.GITHUB and first["kind"] == "DONE"
    assert first["reviewers"] == ["assay", "derrick"] and first["verified"] is True
    assert first["lane"] == TENON and first["head"] == HEAD
    assert first["merge_sha"] == "040eaad231"
    assert second["verified"] is False and second["lane"] == ATELIER
    # The search names the tag; the tag must be on its own line in the body.
    assert f'"{arcs.arc_tag(arc["id"])}" in:body' in gh.calls[0]

    calls = len(gh.calls)
    assert arc_github.sync_arc(arc["id"], gh=gh) == []
    # A PR already on the board is not fetched again: one list call, no views.
    assert [c[:2] for c in gh.calls[calls:]] == [["pr", "list"]]
    assert [e["pr"] for e in arcs.read_entries(arc["id"])] == [11, 13]


def test_an_arc_without_a_repo_is_not_synced():
    arc = _arc(repo="")
    assert arc_github.sync_arc(arc["id"], gh=lambda args: pytest.fail("called gh")) == []


def test_repo_must_be_owner_slash_name():
    with pytest.raises(arcs.ArcError):
        _arc(repo="not a repo")


def test_no_session_can_write_as_github():
    arc = _arc()
    with pytest.raises(arcs.ArcError):
        arcs.add_member(arc["id"], "github", "impostor")
    with pytest.raises(arcs.ArcAccessDenied):
        arcs.append_entry(arc["id"], "github", {"kind": "FACT", "text": "x"})


def test_github_is_not_a_lane_in_idle_metrics():
    from datetime import datetime, timezone

    from kiro_crew import arc_metrics

    arc = _arc()
    arc_github.sync_arc(arc["id"], gh=FakeGh(arc["id"]))
    report = arc_metrics.board_report(arc["id"], datetime.now(timezone.utc))
    assert arcs.GITHUB not in report["idle_gaps"]["per_member"]
    assert report["merged_prs"] == [11, 13] and report["verified_prs"] == [11]


# ── coordinator sign-off ──


def test_coordinator_signoff_advances_the_milestone_atomically():
    arc = _arc()
    arcs.update_settings(arc["id"], coordinator=BATON)
    entry = arcs.append_entry(
        arc["id"],
        BATON,
        {"kind": "FACT", "text": "Owner via Baton: M1 signed off", "signoff": True},
    )
    assert entry["signoff"] is True and entry["milestone"] == 0
    assert arcs.read_arc(arc["id"])["milestone_index"] == 1
    # A replay aimed at the old milestone does not skip M2.
    with pytest.raises(arcs.ArcConflict):
        arcs.append_entry(
            arc["id"], BATON, {"kind": "FACT", "text": "again", "signoff": True, "milestone": 0}
        )
    assert arcs.read_arc(arc["id"])["milestone_index"] == 1


def test_only_the_coordinator_can_sign_off():
    arc = _arc()
    with pytest.raises(arcs.ArcAccessDenied):
        arcs.append_entry(arc["id"], BATON, {"kind": "FACT", "text": "x", "signoff": True})
    arcs.update_settings(arc["id"], coordinator=BATON)
    with pytest.raises(arcs.ArcAccessDenied):
        arcs.append_entry(arc["id"], TENON, {"kind": "FACT", "text": "x", "signoff": True})
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(
            arc["id"], BATON, {"kind": "DONE", "text": "x", "link": "https://x", "signoff": True}
        )
    assert arcs.read_arc(arc["id"])["milestone_index"] == 0


def test_the_coordinator_must_be_a_member():
    arc = _arc()
    with pytest.raises(arcs.ArcError):
        arcs.update_settings(arc["id"], coordinator="chat-9-9")
    assert arcs.update_settings(arc["id"], coordinator="")["coordinator"] == ""


# ── ASK targets, short ids, FACT length, the brief ──


def test_ask_by_role_or_coordinator():
    arc = _arc()
    assert (
        arcs.append_entry(arc["id"], TENON, {"kind": "ASK", "text": "q", "to": "baton"})["to"]
        == BATON
    )
    assert (
        arcs.append_entry(arc["id"], TENON, {"kind": "ASK", "text": "q", "to": "Atelier"})["to"]
        == ATELIER
    )
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc["id"], TENON, {"kind": "ASK", "text": "q", "to": "coordinator"})
    arcs.update_settings(arc["id"], coordinator=BATON)
    assert (
        arcs.append_entry(arc["id"], TENON, {"kind": "ASK", "text": "q", "to": "coordinator"})["to"]
        == BATON
    )
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc["id"], TENON, {"kind": "ASK", "text": "q", "to": "nobody"})


def test_reply_to_accepts_the_short_id():
    arc = _arc()
    ask = arcs.append_entry(arc["id"], TENON, {"kind": "ASK", "text": "q", "to": "owner"})
    reply = arcs.append_entry(
        arc["id"], BATON, {"kind": "FACT", "text": "a", "reply_to": ask["id"][:11]}
    )
    assert reply["reply_to"] == ask["id"]
    assert arcs.open_asks(arc["id"]) == []
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(
            arc["id"], BATON, {"kind": "FACT", "text": "a", "reply_to": "ae_ffffffff"}
        )
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc["id"], BATON, {"kind": "FACT", "text": "a", "reply_to": "ae_1"})


def test_fact_carries_a_ruling_up_to_300_characters():
    arc = _arc()
    arcs.append_entry(arc["id"], BATON, {"kind": "FACT", "text": "r" * 300})
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(arc["id"], BATON, {"kind": "FACT", "text": "r" * 301})
    with pytest.raises(arcs.ArcError):
        arcs.append_entry(
            arc["id"], BATON, {"kind": "CLAIM", "text": "r" * 101, "step": "s", "checked": ["x"]}
        )


def test_brief_names_the_coordinator_and_shows_short_ids():
    arc = _arc()
    entry = arcs.append_entry(arc["id"], TENON, {"kind": "FACT", "text": "found it"})
    assert "Questions for the owner" not in arc_brief.render_brief(
        arcs.read_arc(arc["id"]), ATELIER
    )
    arcs.update_settings(arc["id"], coordinator=BATON)
    brief = arc_brief.render_brief(arcs.read_arc(arc["id"]), ATELIER)
    assert "ASK to=coordinator (Baton)" in brief
    assert arc_brief.entry_line(arc, entry).startswith(f"[FACT {entry['id'][:11]}]")
