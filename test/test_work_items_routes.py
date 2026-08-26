"""Route and MCP identity contracts for coordinator work items."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _request(
    method: str,
    path: str,
    *,
    body: Any = ...,
    match_info: dict[str, str] | None = None,
    session_key: str = "chat-route-1",
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


@pytest.fixture()
def _open_routes(monkeypatch):
    from kiro_crew.dashboard.handlers import session_ledger, work_items

    async def _recognized(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_ledger, "_recognize_session", _recognized)
    monkeypatch.setattr(session_ledger, "_is_restricted_session", lambda *args: False)
    return work_items


@pytest.mark.asyncio
async def test_routes_open_create_evaluate_and_reject_unknown_fields(_open_routes, tmp_path):
    routes = _open_routes
    opened = await routes.api_work_cycle_open(
        _request(
            "POST",
            "/api/work-items/cycle/open",
            body={"goal": "route cycle", "next_action": "create an item"},
        )
    )
    assert opened.status == 200
    target = tmp_path / "proof"
    target.write_text("ok", encoding="utf-8")
    created = await routes.api_work_item_create(
        _request(
            "POST",
            "/api/work-items/items",
            body={
                "title": "route item",
                "acceptance": {"kind": "file", "path": str(target), "exists": True},
                "next_action": "evaluate it",
            },
        )
    )
    item_id = json.loads(created.text)["result"]["id"]
    evaluated = await routes.api_work_item_evaluate(
        _request("POST", "/api/work-items/items/evaluate", body={"item_ids": [item_id]})
    )
    assert evaluated.status == 200
    assert json.loads(evaluated.text)["result"][0]["verdict"] == "pass"
    invalid = await routes.api_work_item_update(
        _request(
            "POST",
            f"/api/work-items/items/{item_id}/update",
            match_info={"item_id": item_id},
            body={"state": "accepted"},
        )
    )
    assert invalid.status == 400
    assert "unknown field" in json.loads(invalid.text)["error"]


@pytest.mark.asyncio
async def test_routes_refuse_restricted_session(monkeypatch, _open_routes):
    from kiro_crew import work_items
    from kiro_crew.dashboard.handlers import session_ledger

    monkeypatch.setattr(session_ledger, "_is_restricted_session", lambda *args: True)
    response = await _open_routes.api_work_items_list(_request("GET", "/api/work-items"))
    assert response.status == 403
    assert not work_items.coordinator_dir("chat-route-1").exists()


@pytest.mark.asyncio
async def test_routes_cannot_cross_coordinator_identity(_open_routes):
    routes = _open_routes
    opened = await routes.api_work_cycle_open(
        _request(
            "POST",
            "/api/work-items/cycle/open",
            body={"goal": "owned by one", "next_action": "hold it"},
            session_key="chat-owner-1",
        )
    )
    assert opened.status == 200
    other = await routes.api_work_items_list(
        _request("GET", "/api/work-items", session_key="chat-other-1")
    )
    assert json.loads(other.text)["result"]["active_cycle"] is None


@pytest.mark.asyncio
async def test_transition_without_required_event_is_rejected_before_storage(_open_routes):
    item_id = "wi_" + "0" * 32
    response = await _open_routes.api_work_item_transition(
        _request(
            "POST",
            f"/api/work-items/items/{item_id}/transition",
            match_info={"item_id": item_id},
            body={"state": "waiting"},
        )
    )
    assert response.status == 400
    assert "required" in json.loads(response.text)["error"]


def test_work_item_routes_are_strict_internal_only():
    from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

    assert "/api/work-items" in _STRICT_INTERNAL_API_PATHS


def test_mcp_tools_refuse_unverified_identity_without_transport(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import work_items

    transport = MagicMock()
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    monkeypatch.setattr(mcp_core, "_get", transport)
    monkeypatch.setattr(mcp_core, "_post", transport)
    assert "could not be verified" in work_items.work_item_list("x", {})
    assert "could not be verified" in work_items.work_cycle_open(
        "x", {"goal": "g", "next_action": "n"}
    )
    transport.assert_not_called()


def test_mcp_tools_pass_the_verified_key_to_each_transport(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import work_items

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-v-1")
    get = MagicMock(return_value={"result": {"active_cycle": None}})
    post = MagicMock(return_value={"result": {"id": "wc_" + "0" * 32}})
    monkeypatch.setattr(mcp_core, "_get", get)
    monkeypatch.setattr(mcp_core, "_post", post)
    work_items.work_item_list("x", {})
    get.assert_called_once_with("/api/work-items", session_key="chat-v-1")
    work_items.work_cycle_open("x", {"goal": "g", "next_action": "n"})
    post.assert_called_once_with(
        "/api/work-items/cycle/open", {"goal": "g", "next_action": "n"}, session_key="chat-v-1"
    )
