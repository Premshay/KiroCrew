"""Member routes and MCP tools for the arc board."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

MEMBER = "chat-arc-member"
PEER = "chat-arc-peer"
OUTSIDER = "chat-arc-outsider"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


@pytest.fixture()
def routes(monkeypatch):
    from kiro_crew.dashboard.handlers import arcs as arc_routes
    from kiro_crew.dashboard.handlers import session_ledger

    async def _recognized(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_ledger, "_recognize_session", _recognized)
    monkeypatch.setattr(session_ledger, "_is_restricted_session", lambda *args: False)
    return arc_routes


@pytest.fixture()
def arc():
    from kiro_crew import arcs

    made = arcs.create_arc(
        {"title": "Pilot", "goal": "ship", "milestones": [{"name": "m1"}], "rules": ["be brief"]}
    )
    arcs.add_member(made["id"], MEMBER, "builder")
    arcs.add_member(made["id"], PEER, "reviewer")
    return made


def _request(
    method: str,
    path: str,
    *,
    body: Any = ...,
    match_info: dict[str, str] | None = None,
    session_key: str = MEMBER,
) -> web.Request:
    app = web.Application()
    app["state"] = MagicMock()
    request = make_mocked_request(
        method,
        path,
        app=app,
        headers={"X-Session-Key": session_key},
        match_info=match_info or {},
    )
    if body is not ...:
        request.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return request


def _post(arc_id: str, body: dict[str, Any], key: str = MEMBER) -> web.Request:
    return _request(
        "POST",
        f"/api/arcs/{arc_id}/entries",
        body=body,
        match_info={"arc_id": arc_id},
        session_key=key,
    )


def _get(arc_id: str, suffix: str, key: str = MEMBER) -> web.Request:
    return _request(
        "GET", f"/api/arcs/{arc_id}/{suffix}", match_info={"arc_id": arc_id}, session_key=key
    )


@pytest.mark.asyncio
async def test_mine_lists_only_member_arcs(routes, arc):
    mine = await routes.api_arcs_mine(_request("GET", "/api/arcs/mine"))
    assert json.loads(mine.text)["arcs"] == [
        {"id": arc["id"], "title": "Pilot", "goal": "ship", "milestone": "m1"}
    ]
    other = await routes.api_arcs_mine(_request("GET", "/api/arcs/mine", session_key=OUTSIDER))
    assert json.loads(other.text)["arcs"] == []


@pytest.mark.asyncio
async def test_member_reads_brief_and_entries(routes, arc):
    from kiro_crew import arcs

    entry = arcs.append_entry(arc["id"], PEER, {"kind": "FACT", "text": "peer fact"})
    brief = await routes.api_arc_brief(_get(arc["id"], "brief"))
    assert brief.status == 200
    assert "ship" in json.loads(brief.text)["brief"]
    read = await routes.api_arc_entries_read(_get(arc["id"], "entries"))
    assert read.status == 200
    payload = json.loads(read.text)
    assert [e["id"] for e in payload["entries"]] == [entry["id"]]
    assert payload["open_asks"] == []
    assert [e["id"] for e in arcs.unseen_entries(arc["id"], MEMBER)] == [entry["id"]]


@pytest.mark.asyncio
async def test_entries_grep_and_after(routes, arc):
    from kiro_crew import arcs

    first = arcs.append_entry(arc["id"], PEER, {"kind": "FACT", "text": "alpha thing"})
    second = arcs.append_entry(arc["id"], PEER, {"kind": "FACT", "text": "beta thing"})
    request = _request(
        "GET",
        f"/api/arcs/{arc['id']}/entries?q=beta",
        match_info={"arc_id": arc["id"]},
    )
    hits = json.loads((await routes.api_arc_entries_read(request)).text)["entries"]
    assert [e["id"] for e in hits] == [second["id"]]
    request = _request(
        "GET",
        f"/api/arcs/{arc['id']}/entries?after={first['id']}",
        match_info={"arc_id": arc["id"]},
    )
    after = json.loads((await routes.api_arc_entries_read(request)).text)["entries"]
    assert [e["id"] for e in after] == [second["id"]]


@pytest.mark.asyncio
async def test_entries_read_signals_truncation_and_pages(routes, arc, monkeypatch):
    from kiro_crew import arcs

    monkeypatch.setattr(routes, "_PAGE", 2)
    ids = [
        arcs.append_entry(arc["id"], PEER, {"kind": "FACT", "text": f"fact {i}"})["id"]
        for i in range(3)
    ]
    first = json.loads((await routes.api_arc_entries_read(_get(arc["id"], "entries"))).text)
    assert [e["id"] for e in first["entries"]] == ids[:2]
    assert first["truncated"] is True
    assert first["next_after"] == ids[1]
    request = _request(
        "GET",
        f"/api/arcs/{arc['id']}/entries?after={first['next_after']}",
        match_info={"arc_id": arc["id"]},
    )
    rest = json.loads((await routes.api_arc_entries_read(request)).text)
    assert [e["id"] for e in rest["entries"]] == ids[2:]
    assert "truncated" not in rest and "next_after" not in rest


@pytest.mark.asyncio
async def test_entry_text_is_redacted(routes, arc):
    from kiro_crew import arcs

    arcs.append_entry(arc["id"], PEER, {"kind": "FACT", "text": "key sk-ant-api03-" + "A" * 40})
    read = await routes.api_arc_entries_read(_get(arc["id"], "entries"))
    assert "sk-ant-api03-AAAA" not in read.text


@pytest.mark.asyncio
async def test_non_member_refused(routes, arc):
    for handler, request in (
        (routes.api_arc_brief, _get(arc["id"], "brief", OUTSIDER)),
        (routes.api_arc_entries_read, _get(arc["id"], "entries", OUTSIDER)),
        (
            routes.api_arc_entries_write,
            _post(arc["id"], {"kind": "FACT", "text": "x"}, OUTSIDER),
        ),
    ):
        response = await handler(request)
        assert response.status == 403
        assert json.loads(response.text)["code"] == "arc_access_denied"


@pytest.mark.asyncio
async def test_unknown_arc_is_404(routes):
    missing = "arc_" + "0" * 32
    response = await routes.api_arc_brief(_get(missing, "brief"))
    assert response.status == 404
    assert json.loads(response.text)["code"] == "arc_not_found"


@pytest.mark.asyncio
async def test_write_records_verified_caller_as_author(routes, arc):
    response = await routes.api_arc_entries_write(_post(arc["id"], {"kind": "FACT", "text": "hi"}))
    assert response.status == 200
    from kiro_crew.session_ledger import ledger_key

    assert json.loads(response.text)["entry"]["author"] == ledger_key(MEMBER)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["author", "session_key"])
async def test_write_rejects_identity_fields(routes, arc, field):
    response = await routes.api_arc_entries_write(
        _post(arc["id"], {"kind": "FACT", "text": "hi", field: PEER})
    )
    assert response.status == 400


@pytest.mark.asyncio
async def test_write_returns_unseen_for_caller(routes, arc):
    from kiro_crew import arcs

    arcs.append_entry(arc["id"], PEER, {"kind": "FACT", "text": "calm fact"})
    fail = arcs.append_entry(arc["id"], PEER, {"kind": "FAIL", "text": "it broke"})
    response = await routes.api_arc_entries_write(
        _post(arc["id"], {"kind": "FACT", "text": "mine"})
    )
    unseen = json.loads(response.text)["unseen"]
    assert [e["id"] for e in unseen] == [fail["id"]]
    assert len(arcs.unseen_entries(arc["id"], MEMBER)) == 2


@pytest.mark.asyncio
async def test_write_leaves_cursor_to_turn_prefix(routes, arc):
    from kiro_crew import arc_brief, arcs

    for kind, text in (("FACT", "first fact"), ("FAIL", "it broke"), ("FACT", "last fact")):
        arcs.append_entry(arc["id"], PEER, {"kind": kind, "text": text})
    response = await routes.api_arc_entries_write(
        _post(arc["id"], {"kind": "FACT", "text": "mine"})
    )
    assert [e["text"] for e in json.loads(response.text)["unseen"]] == ["it broke"]
    await routes.api_arc_entries_read(_get(arc["id"], "entries"))
    prefix = arc_brief.arc_turn_prefix(MEMBER)
    for text in ("first fact", "it broke", "last fact"):
        assert text in prefix


@pytest.mark.asyncio
async def test_write_returns_reply_to_own_ask_and_claim_collision(routes, arc):
    from kiro_crew import arcs

    ask = json.loads(
        (
            await routes.api_arc_entries_write(
                _post(arc["id"], {"kind": "ASK", "text": "which?", "to": PEER})
            )
        ).text
    )["entry"]
    claim_body = {"kind": "CLAIM", "text": "taking it", "step": "wire api", "checked": ["ADR-1"]}
    await routes.api_arc_entries_write(_post(arc["id"], claim_body))
    answer = arcs.append_entry(
        arc["id"], PEER, {"kind": "FACT", "text": "this", "reply_to": ask["id"]}
    )
    rival = arcs.append_entry(arc["id"], PEER, {**claim_body, "kind": "CLAIM"})
    arcs.append_entry(arc["id"], PEER, {"kind": "FACT", "text": "noise"})
    response = await routes.api_arc_entries_write(_post(arc["id"], {"kind": "FACT", "text": "go"}))
    ids = [e["id"] for e in json.loads(response.text)["unseen"]]
    assert ids == [answer["id"], rival["id"]]


@pytest.mark.asyncio
async def test_write_rejects_bad_entry(routes, arc):
    response = await routes.api_arc_entries_write(_post(arc["id"], {"kind": "DONE", "text": "x"}))
    assert response.status == 400
    assert json.loads(response.text)["code"] == "arc_validation"


def test_tools_have_no_identity_arguments():
    from kiro_crew.mcp_tools import arcs as arc_tools

    tools = {t["name"]: t for t in arc_tools.schemas()}
    assert set(tools) == {"arc_brief", "arc_board_read", "arc_board_grep", "arc_board_write"}
    assert set(tools) == set(arc_tools.HANDLERS)
    for tool in tools.values():
        props = tool["inputSchema"]["properties"]
        assert not any("session" in p or "author" in p for p in props)
    assert tools["arc_board_write"]["inputSchema"]["properties"]["kind"]["enum"] == [
        "CLAIM",
        "FACT",
        "FAIL",
        "ASK",
        "DONE",
    ]


def test_tool_calls_use_strict_identity_and_routes(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import arcs as arc_tools

    calls: list[tuple] = []
    monkeypatch.setattr(mcp_core, "require_strict_session_key", lambda message: ("chat-x", ""))
    monkeypatch.setattr(
        mcp_core,
        "_get",
        lambda path, session_key=None, **kw: calls.append(("GET", path, session_key))
        or {"entries": []},
    )
    monkeypatch.setattr(
        mcp_core,
        "_post",
        lambda path, body, session_key=None, **kw: calls.append(("POST", path, body, session_key))
        or {"entry": {}},
    )
    arc_id = "arc_" + "1" * 32
    arc_tools.HANDLERS["arc_board_read"]("arc_board_read", {"arc_id": arc_id, "after": "ae_1"})
    arc_tools.HANDLERS["arc_board_grep"]("arc_board_grep", {"arc_id": arc_id, "query": "a b"})
    arc_tools.HANDLERS["arc_board_write"](
        "arc_board_write", {"arc_id": arc_id, "kind": "FACT", "text": "t"}
    )
    assert calls[0] == ("GET", f"/api/arcs/{arc_id}/entries?after=ae_1", "chat-x")
    assert calls[1] == ("GET", f"/api/arcs/{arc_id}/entries?q=a+b", "chat-x")
    assert calls[2] == (
        "POST",
        f"/api/arcs/{arc_id}/entries",
        {"kind": "FACT", "text": "t"},
        "chat-x",
    )
