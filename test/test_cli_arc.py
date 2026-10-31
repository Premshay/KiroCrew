"""``kirocrew arc``: the owner CLI and the deliver route."""

from __future__ import annotations

import argparse
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import arcs
from kiro_crew import cli_commands_arc as cli_arc


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.fixture()
def arc():
    made = arcs.create_arc(
        {
            "title": "Pilot",
            "goal": "ship",
            "milestones": [{"name": "m1", "criteria": "it ships"}, {"name": "m2"}],
        }
    )
    arcs.add_member(made["id"], "chat-a", "builder")
    arcs.add_member(made["id"], "chat-b", "reviewer")
    return made


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def test_create_from_file_prints_id(tmp_path, capsys):
    spec = tmp_path / "arc.json"
    spec.write_text(json.dumps({"title": "T", "goal": "g", "milestones": [{"name": "m"}]}))
    cli_arc._arc(_ns(arc_action="create", file=str(spec)))
    arc_id = capsys.readouterr().out.strip()
    assert arcs.read_arc(arc_id)["title"] == "T"


def test_create_invalid_exits_1(tmp_path, capsys):
    spec = tmp_path / "arc.json"
    spec.write_text(json.dumps({"goal": "no title"}))
    with pytest.raises(SystemExit) as exc:
        cli_arc._arc(_ns(arc_action="create", file=str(spec)))
    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("Error:")


def test_add_member_and_accept(arc):
    cli_arc._arc(_ns(arc_action="add-member", arc_id=arc["id"], session_key="chat-c", role="qa"))
    assert arcs.read_arc(arc["id"])["members"]["chat-c"]["role"] == "qa"
    cli_arc._arc(_ns(arc_action="accept", arc_id=arc["id"]))
    assert arcs.read_arc(arc["id"])["milestone_index"] == 1


def _ask(arc):
    return arcs.append_entry(
        arc["id"], "chat-a", {"kind": "ASK", "text": "which key?", "to": arcs.OWNER}
    )


def test_answer_closes_ask_and_requests_delivery(arc):
    ask = _ask(arc)
    with patch.object(cli_arc, "_request_delivery", return_value=None) as deliver:
        cli_arc._arc(_ns(arc_action="answer", arc_id=arc["id"], ask_id=ask["id"], text="staging"))
    assert arcs.open_asks(arc["id"]) == []
    last = arcs.read_entries(arc["id"])[-1]
    assert (last["author"], last["kind"], last["reply_to"]) == (arcs.OWNER, "FACT", ask["id"])
    deliver.assert_called_once_with(arc["id"], last["id"])


def test_answer_with_gateway_down_still_stores(arc, capsys):
    ask = _ask(arc)
    with (
        patch.object(cli_arc.KiroCrewConfig, "load", return_value=MagicMock()),
        patch.object(cli_arc, "parse_dashboard_url", return_value=("127.0.0.1", 1)),
        patch.object(cli_arc, "_internal_secret", return_value="s"),
        patch.object(cli_arc, "_gateway_send", side_effect=ConnectionRefusedError("down")),
    ):
        cli_arc._arc(_ns(arc_action="answer", arc_id=arc["id"], ask_id=ask["id"], text="staging"))
    assert arcs.open_asks(arc["id"]) == []
    assert "Warning" in capsys.readouterr().err


def test_answer_too_long_exits_1(arc, capsys):
    ask = _ask(arc)
    with pytest.raises(SystemExit) as exc:
        cli_arc._arc(_ns(arc_action="answer", arc_id=arc["id"], ask_id=ask["id"], text="x" * 101))
    assert exc.value.code == 1
    assert len(arcs.open_asks(arc["id"])) == 1


def test_answer_to_unknown_or_non_ask_id_exits_1_and_writes_nothing(arc, capsys):
    ask = _ask(arc)
    fact = arcs.append_entry(arc["id"], "chat-a", {"kind": "FACT", "text": "note"})
    before = len(arcs.read_entries(arc["id"]))
    for bad in (ask["id"] + "x", fact["id"]):
        with pytest.raises(SystemExit) as exc:
            cli_arc._arc(_ns(arc_action="answer", arc_id=arc["id"], ask_id=bad, text="staging"))
        assert exc.value.code == 1
        assert "is not an ASK" in capsys.readouterr().err
    assert len(arcs.read_entries(arc["id"])) == before
    assert len(arcs.open_asks(arc["id"])) == 1


def test_show_lists_open_asks_with_age(arc, capsys):
    ask = _ask(arc)
    arcs.append_entry(
        arc["id"],
        "chat-b",
        {"kind": "CLAIM", "step": "review it", "text": "on it", "checked": ["adr-1"]},
    )
    arcs.mark_seen(arc["id"], "chat-b", ask["id"])
    cli_arc._arc(_ns(arc_action="show", arc_id=arc["id"]))
    out = capsys.readouterr().out
    assert "Milestone 1/2: m1 — it ships" in out
    assert "reviewer: review it" in out
    assert "builder -> owner" in out and "which key?" in out
    assert "seen by: reviewer" in out
    assert "[ASK] builder (" in out


def _deliver_request(arc_id, entry_id, *, internal):
    app = {"state": MagicMock()}
    request = make_mocked_request(
        "POST",
        f"/api/arcs/{arc_id}/deliver/{entry_id}",
        app=MagicMock(get=lambda k, d=None: app.get(k, d)),
        match_info={"arc_id": arc_id, "entry_id": entry_id},
    )
    if internal:
        request["internal_auth"] = True
    return request


@pytest.mark.asyncio
async def test_deliver_route_requires_internal_auth(arc):
    from kiro_crew.dashboard.handlers import arcs as routes

    entry = _ask(arc)
    with patch.object(routes, "deliver_entry", new=AsyncMock()) as deliver:
        response = await routes.api_arc_deliver(
            _deliver_request(arc["id"], entry["id"], internal=False)
        )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "internal_auth_required"
    deliver.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_route_delivers_stored_entry_and_404s(arc):
    from kiro_crew.dashboard.handlers import arcs as routes

    entry = _ask(arc)
    with patch.object(routes, "deliver_entry", new=AsyncMock(return_value={"outcome": "ok"})) as d:
        ok = await routes.api_arc_deliver(_deliver_request(arc["id"], entry["id"], internal=True))
        missing = await routes.api_arc_deliver(_deliver_request(arc["id"], "nope", internal=True))
    assert json.loads(ok.text) == {"delivery": {"outcome": "ok"}}
    assert d.await_args.args[2]["id"] == entry["id"]
    assert missing.status == 404
    assert json.loads(missing.text)["code"] == "arc_entry_not_found"


def test_parser_and_help_know_arc():
    from kiro_crew import cli_help

    assert "arc" in cli_help.SUMMARIES
    parser = argparse.ArgumentParser()
    from kiro_crew import cli_parser_agent

    cli_parser_agent.register_arc_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["arc", "answer", "a", "e", "text"])
    assert (args.arc_action, args.ask_id, args.text) == ("answer", "e", "text")
