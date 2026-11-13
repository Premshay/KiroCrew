"""Owner closure stops turn context while preserving the durable board."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import pytest

from kiro_crew import arc_brief, arcs, cli_commands_arc, cli_parser_agent

MEMBER = "chat-close-builder"
PEER = "chat-close-reviewer"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path):
    with pytest.MonkeyPatch.context() as patches:
        patches.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        yield


@pytest.fixture()
def arc():
    made = arcs.create_arc(
        {"title": "Closure pilot", "milestones": [{"name": "Ship", "criteria": "verified"}]},
        [{"session_key": MEMBER, "role": "builder"}, {"session_key": PEER, "role": "reviewer"}],
    )
    arcs.append_entry(made["id"], MEMBER, {"kind": "FACT", "text": "Evidence stays"})
    return made


def test_close_stops_next_prefix_for_every_member_without_changing_history(arc, monkeypatch):
    arc_id = arc["id"]
    arcs.accept_milestone(arc_id)
    for key in (MEMBER, PEER):
        assert "[Arc board: Closure pilot]" in arc_brief.arc_turn_prefix(key)
    arcs.append_entry(arc_id, MEMBER, {"kind": "ASK", "text": "Retain this ask?", "to": PEER})
    before = arcs.read_arc(arc_id)
    directory = arcs._arc_dir(arc_id)
    evidence = {name: (directory / name).read_bytes() for name in ("entries.jsonl", "cursors.json")}
    now = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(arcs, "_now", lambda: now)

    closed = arcs.close_arc(arc_id)

    assert closed == {**before, "closed_at": now.isoformat(timespec="seconds")}
    for key in (MEMBER, PEER):
        assert arc_brief.arc_turn_prefix(key) == ""
        assert arcs.arcs_for_member(key) == [closed]
        assert "Closure pilot" not in arc_brief.arc_turn_prefix(f"dashboard:{key}")
    assert "Retain this ask?" in arc_brief.brief_for(closed, PEER)
    assert len(arcs.open_asks(arc_id)) == 1
    assert all((directory / name).read_bytes() == value for name, value in evidence.items())


def test_close_is_idempotent_and_does_not_accept_a_milestone(arc, monkeypatch):
    first = arcs.close_arc(arc["id"])
    assert first["milestone_index"] == 0

    def unexpected_write(*args, **kwargs):
        pytest.fail("closing an already closed arc must not rewrite it")

    monkeypatch.setattr(arcs, "_write_json", unexpected_write)
    assert arcs.close_arc(arc["id"]) == first


def test_unknown_arc_close_does_not_create_state():
    with pytest.raises(arcs.ArcNotFound):
        arcs.close_arc("arc_" + "0" * 32)
    assert arcs.list_arcs() == []


def test_old_arcs_without_a_closed_timestamp_still_inject(arc):
    arc.pop("closed_at")
    arcs._write_json(arcs._arc_dir(arc["id"]) / "arc.json", arc)
    assert "[Arc board: Closure pilot]" in arc_brief.arc_turn_prefix(MEMBER)


def test_closed_arcs_do_not_consume_the_active_arcs_brief_budget(arc):
    arcs.close_arc(arc["id"])
    active = arcs.create_arc(
        {"title": "Active work", "goal": "G" * 2_000},
        [{"session_key": MEMBER, "role": "builder"}],
    )
    entry = arcs.append_entry(active["id"], arcs.OWNER, {"kind": "FACT", "text": "New work"})
    prefix = arc_brief.arc_turn_prefix(MEMBER)
    assert "Closure pilot" not in prefix
    assert "[Arc board: Active work]" in prefix
    assert "New work" in prefix
    assert len(prefix) <= arc_brief.PREFIX_CAP
    assert arcs.unseen_entries(active["id"], MEMBER) == []
    assert arcs.seen_by(active["id"], entry["id"]) == [MEMBER]


def run_cli(*args):
    parser = argparse.ArgumentParser()
    cli_parser_agent.register_arc_parser(parser.add_subparsers(dest="command"))
    cli_commands_arc._arc(parser.parse_args(["arc", *args]))


def test_cli_close_and_show_preserve_readable_entries(arc, capsys):
    run_cli("close", arc["id"])
    assert f"Closed {arc['id']}" in capsys.readouterr().out
    assert arc_brief.arc_turn_prefix(MEMBER) == ""
    run_cli("show", arc["id"])
    shown = capsys.readouterr().out
    assert "Closed:" in shown
    assert "Evidence stays" in shown


def test_cli_remove_member_only_detaches_that_session(arc):
    run_cli("remove-member", arc["id"], f"dashboard:{MEMBER}")
    assert arc_brief.arc_turn_prefix(MEMBER) == ""
    assert "[Arc board: Closure pilot]" in arc_brief.arc_turn_prefix(PEER)
    assert not arcs.read_arc(arc["id"]).get("closed_at")
    assert arcs.read_entries(arc["id"])[0]["text"] == "Evidence stays"


def test_last_accept_suggests_close_but_does_not_close(arc, capsys):
    run_cli("accept", arc["id"])
    assert f"kirocrew arc close {arc['id']}" in capsys.readouterr().out
    assert not arcs.read_arc(arc["id"]).get("closed_at")


@pytest.mark.parametrize("command,tail", [("close", []), ("remove-member", [MEMBER])])
def test_cli_unknown_arc_exits_nonzero(command, tail, capsys):
    with pytest.raises(SystemExit) as exc:
        run_cli(command, "arc_" + "0" * 32, *tail)
    assert exc.value.code == 1
    assert "was not found" in capsys.readouterr().err
