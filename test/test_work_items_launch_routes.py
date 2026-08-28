"""Route and MCP wiring contracts for launched-subagent dispatch (Slice 3)."""

from __future__ import annotations

import hashlib
import json
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_items as wi

COORDINATOR = "chat-route-1"
AGENT = "crew-codex"
CANDIDATE = hashlib.sha256(AGENT.encode("utf-8")).hexdigest()[:16]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


def _request(
    method: str,
    path: str,
    *,
    body: Any = ...,
    match_info: dict[str, str] | None = None,
    session_key: str = COORDINATOR,
    manager: Any = None,
) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.subagents = manager
    app["state"] = state
    request = make_mocked_request(
        method,
        path,
        app=app,
        headers={"X-Session-Key": session_key} if session_key is not None else {},
        match_info=match_info or {},
    )
    if body is not ...:
        request.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return request


class QueuedManager:
    """Accepted-once manager: spawn succeeds once, run stays queued."""

    def __init__(self):
        self.spawned: list[dict] = []
        self.cancelled: list[str] = []
        self.run_states: dict[str, str] = {}

    def add_event_hook(self, hook) -> None:
        pass

    def spawn(self, contract, parent_session_key="", agent="", _preassigned_id=""):
        self.spawned.append(
            {"agent": agent, "parent_session_key": parent_session_key, "run_id": _preassigned_id}
        )
        self.run_states[_preassigned_id] = "queued"
        return MagicMock(error=None)

    def run_state(self, run_id: str) -> str:
        return self.run_states.get(run_id, "unknown")

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


@pytest.fixture()
def routes(monkeypatch):
    from kiro_crew import work_dispatch
    from kiro_crew.dashboard.handlers import session_ledger, work_items

    async def _recognized(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_ledger, "_recognize_session", _recognized)
    monkeypatch.setattr(session_ledger, "_is_restricted_session", lambda *a: False)
    monkeypatch.setattr(
        work_dispatch, "list_agents", lambda: [types.SimpleNamespace(name=AGENT)]
    )
    return work_items


async def _create_item(routes) -> dict:
    opened = await routes.api_work_cycle_open(
        _request("POST", "/api/work-items/cycle/open", body={"goal": "route cycle", "next_action": "launch"})
    )
    assert opened.status == 200
    created = await routes.api_work_item_create(
        _request(
            "POST",
            "/api/work-items/items",
            body={
                "title": "route item",
                "acceptance": {"kind": "file", "path": "/tmp/proof", "exists": True},
                "next_action": "evaluate it",
            },
        )
    )
    assert created.status == 200
    return json.loads(created.text)["result"]


@pytest.mark.asyncio
async def test_launch_candidates_route_returns_stable_handles(routes):
    response = await routes.api_work_item_launch_candidates(
        _request("GET", "/api/work-items/launch/candidates")
    )
    assert response.status == 200
    payload = json.loads(response.text)
    candidates = payload["result"]["candidates"]
    assert [c["agent"] for c in candidates] == [AGENT]
    assert len(candidates[0]["id"]) == 16


@pytest.mark.asyncio
async def test_launch_route_arms_and_reports_queued(routes):
    item = await _create_item(routes)
    manager = QueuedManager()
    response = await routes.api_work_item_launch(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/launch",
            match_info={"item_id": item["id"]},
            body={"candidate_id": CANDIDATE},
            manager=manager,
        )
    )
    assert response.status == 200
    result = json.loads(response.text)["result"]
    assert result["code"] == "launch_queued"
    assignment = result["item"]["assignment"]
    assert assignment["status"] == wi.ASSIGNMENT_LAUNCH_QUEUED
    assert manager.spawned[0]["run_id"] == assignment["worker_run_id"]
    assert manager.spawned[0]["agent"] == AGENT


@pytest.mark.asyncio
async def test_launch_route_requires_a_candidate_id(routes):
    item = await _create_item(routes)
    response = await routes.api_work_item_launch(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/launch",
            match_info={"item_id": item["id"]},
            body={},
            manager=QueuedManager(),
        )
    )
    assert response.status == 400
    assert "validation" in json.loads(response.text)["code"]


@pytest.mark.asyncio
async def test_launch_route_maps_unknown_candidate_to_400(routes):
    item = await _create_item(routes)
    response = await routes.api_work_item_launch(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/launch",
            match_info={"item_id": item["id"]},
            body={"candidate_id": "f" * 16},
            manager=QueuedManager(),
        )
    )
    assert response.status == 400
    assert json.loads(response.text)["code"] == "worker_target_unavailable"
    read = wi.read_item(COORDINATOR, item["id"])
    assert read["assignment"] is None


@pytest.mark.asyncio
async def test_launch_route_refusal_keeps_item_open_with_named_code(routes):
    item = await _create_item(routes)
    manager = QueuedManager()
    manager.spawn = lambda *a, **k: MagicMock(error="at capacity: queue is full")  # type: ignore[method-assign]
    response = await routes.api_work_item_launch(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/launch",
            match_info={"item_id": item["id"]},
            body={"candidate_id": CANDIDATE},
            manager=manager,
        )
    )
    assert response.status == 200
    result = json.loads(response.text)["result"]
    assert result["code"] == "dispatch_delivery_failed"
    assert result["item"]["assignment"]["failure_code"] == "launch_capacity"
    assert result["item"]["state"] == wi.STATE_PROPOSED


@pytest.mark.asyncio
async def test_launch_route_manager_unavailable_is_503(routes):
    item = await _create_item(routes)
    response = await routes.api_work_item_launch(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/launch",
            match_info={"item_id": item["id"]},
            body={"candidate_id": CANDIDATE},
            manager=None,
        )
    )
    assert response.status == 503
    assert json.loads(response.text)["code"] == "subagent_manager_unavailable"


@pytest.mark.asyncio
async def test_dispatch_retry_route_maps_stale_assignment_to_409(routes):
    item = await _create_item(routes)
    manager = QueuedManager()
    launched = await routes.api_work_item_launch(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/launch",
            match_info={"item_id": item["id"]},
            body={"candidate_id": CANDIDATE},
            manager=manager,
        )
    )
    assert launched.status == 200
    revoked = await routes.api_work_item_revoke_assignment(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/revoke-assignment",
            match_info={"item_id": item["id"]},
            body={},
            manager=manager,
        )
    )
    assert revoked.status == 200
    retry = await routes.api_work_item_dispatch_retry(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/dispatch-retry",
            match_info={"item_id": item["id"]},
            body={},
            manager=manager,
        )
    )
    assert retry.status == 409
    assert json.loads(retry.text)["code"] == "assignment_stale"


@pytest.mark.asyncio
async def test_revoke_route_cancels_only_queued_runs(routes):
    item = await _create_item(routes)
    manager = QueuedManager()
    launched = await routes.api_work_item_launch(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/launch",
            match_info={"item_id": item["id"]},
            body={"candidate_id": CANDIDATE},
            manager=manager,
        )
    )
    assert launched.status == 200
    run_id = json.loads(launched.text)["result"]["item"]["assignment"]["worker_run_id"]
    revoked = await routes.api_work_item_revoke_assignment(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/revoke-assignment",
            match_info={"item_id": item["id"]},
            body={},
            manager=manager,
        )
    )
    assert revoked.status == 200
    assert manager.cancelled == [run_id]
    read = wi.read_item(COORDINATOR, item["id"])
    assert read["assignment"]["status"] == wi.ASSIGNMENT_REVOKED
    assert read["state"] == wi.STATE_PROPOSED


@pytest.mark.asyncio
async def test_worker_routes_reject_non_subagent_keys(routes):
    for key in ("chat-route-1", "dashboard:ui", None):
        response = await routes.api_work_item_assigned_list(
            _request("GET", "/api/work-items/assigned", session_key=key)
        )
        assert response.status == 403
        assert json.loads(response.text)["code"] == "worker_identity_invalid"


@pytest.mark.asyncio
async def test_worker_routes_accept_strict_subagent_shape(routes):
    response = await routes.api_work_item_assigned_list(
        _request("GET", "/api/work-items/assigned", session_key="subagent:deadbeef")
    )
    assert response.status == 200
    assert json.loads(response.text)["result"] == []


async def _deliver(routes, item) -> tuple[str, QueuedManager]:
    manager = QueuedManager()
    launched = await routes.api_work_item_launch(
        _request(
            "POST",
            f"/api/work-items/items/{item['id']}/launch",
            match_info={"item_id": item["id"]},
            body={"candidate_id": CANDIDATE},
            manager=manager,
        )
    )
    assert launched.status == 200
    run_id = json.loads(launched.text)["result"]["item"]["assignment"]["worker_run_id"]
    # The manager's child-start receipt arrives through the store directly.
    wi.record_launch_delivered(COORDINATOR, run_id)
    return run_id, manager


@pytest.mark.asyncio
async def test_worker_progress_and_handoff_routes(routes):
    item = await _create_item(routes)
    run_id, _manager = await _deliver(routes, item)
    worker_key = f"subagent:{run_id}"

    progress = await routes.api_work_item_report_progress(
        _request(
            "POST",
            f"/api/work-items/assigned/{item['id']}/progress",
            match_info={"item_id": item["id"]},
            body={"text": "halfway there", "kind": "progress"},
            session_key=worker_key,
        )
    )
    assert progress.status == 200
    view = json.loads(progress.text)["result"]
    assert view["events"][-1]["kind"] == "progress"

    handoff = await routes.api_work_item_submit_handoff(
        _request(
            "POST",
            f"/api/work-items/assigned/{item['id']}/handoff",
            match_info={"item_id": item["id"]},
            body={
                "outcome": "repair complete",
                "next_action": "review",
                "verification": ["suite green"],
            },
            session_key=worker_key,
        )
    )
    assert handoff.status == 200
    handoff_view = json.loads(handoff.text)["result"]
    assert handoff_view["worker_handoff"]["outcome"] == "repair complete"
    assert handoff_view["state"] == wi.STATE_DISPATCHED

    # A different subagent run cannot write to this assignment.
    foreign = await routes.api_work_item_report_progress(
        _request(
            "POST",
            f"/api/work-items/assigned/{item['id']}/progress",
            match_info={"item_id": item["id"]},
            body={"text": "sneaky", "kind": "progress"},
            session_key="subagent:01234567",
        )
    )
    assert foreign.status == 403
    assert json.loads(foreign.text)["code"] == "assignment_access_denied"


@pytest.mark.asyncio
async def test_worker_handoff_route_requires_the_pairing_fields(routes):
    item = await _create_item(routes)
    run_id, _manager = await _deliver(routes, item)
    worker_key = f"subagent:{run_id}"
    response = await routes.api_work_item_submit_handoff(
        _request(
            "POST",
            f"/api/work-items/assigned/{item['id']}/handoff",
            match_info={"item_id": item["id"]},
            body={
                "outcome": "o",
                "next_action": "n",
                "verification": ["v"],
                "blocker": "only half of the pair",
            },
            session_key=worker_key,
        )
    )
    assert response.status in (400, 422) or json.loads(response.text).get("error")


def test_mcp_launch_tools_pass_the_verified_key(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import work_items

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "chat-v-1")
    get = MagicMock(return_value={"result": {"candidates": []}})
    post = MagicMock(return_value={"result": {"code": "launch_queued"}})
    monkeypatch.setattr(mcp_core, "_get", get)
    monkeypatch.setattr(mcp_core, "_post", post)

    work_items.work_item_launch_candidates("x", {})
    get.assert_called_once_with("/api/work-items/launch/candidates", session_key="chat-v-1")
    work_items.work_item_launch("x", {"item_id": "wi_1", "candidate_id": "cd"})
    post.assert_called_once_with(
        "/api/work-items/items/wi_1/launch", {"candidate_id": "cd"}, session_key="chat-v-1"
    )
    work_items.work_item_dispatch_retry("x", {"item_id": "wi_1"})
    work_items.work_item_revoke_assignment("x", {"item_id": "wi_1"})
    work_items.work_item_assigned_list("x", {})
    work_items.work_item_assigned_read("x", {"item_id": "wi_1"})
    work_items.work_item_report_progress("x", {"item_id": "wi_1", "text": "t", "kind": "progress"})
    work_items.work_item_submit_handoff(
        "x", {"item_id": "wi_1", "outcome": "o", "next_action": "n", "verification": ["v"]}
    )
    calls = [call.args for call in post.call_args_list]
    assert calls[1] == ("/api/work-items/items/wi_1/dispatch-retry", {})
    assert calls[2] == ("/api/work-items/items/wi_1/revoke-assignment", {})
    assert calls[3] == ("/api/work-items/assigned/wi_1/progress", {"text": "t", "kind": "progress"})
    assert calls[4][0] == "/api/work-items/assigned/wi_1/handoff"
    assert calls[4][1] == {"outcome": "o", "next_action": "n", "verification": ["v"]}
    get_calls = [call.args for call in get.call_args_list]
    assert get_calls[1] == ("/api/work-items/assigned",)
    assert get_calls[2] == ("/api/work-items/assigned/wi_1",)


def test_mcp_worker_tools_refuse_unverified_identity(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools import work_items

    transport = MagicMock()
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    monkeypatch.setattr(mcp_core, "_get", transport)
    monkeypatch.setattr(mcp_core, "_post", transport)
    assert "could not be verified" in work_items.work_item_assigned_list("x", {})
    assert "could not be verified" in work_items.work_item_launch("x", {"item_id": "wi_1", "candidate_id": "cd"})
    transport.assert_not_called()
