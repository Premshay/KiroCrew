"""Companion execution-seat aliases use their factory without becoming ACP backends."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from kiro_crew.execution_context import execution_for_store
from kiro_crew.execution_seats import (
    declared_execution_seats,
    validate_member_execution_seat,
)
from test_spawn_backend import TestFollowUps as _FollowUps
from test_spawn_backend import _mgr, _real_request, _run
from test_spawn_backend import selectable as _selectable_fixture

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.fixture
def selectable(monkeypatch):
    return _selectable_fixture.__wrapped__(monkeypatch)


@pytest.fixture(autouse=True)
def _close_managers(close_subagent_managers):
    pass


@pytest.fixture
def seats(monkeypatch):
    rows = {
        "antigravity": {
            "id": "antigravity",
            "runtime_agent": "crew-antigravity",
            "runtime": "Antigravity",
        }
    }
    monkeypatch.setattr("kiro_crew.execution_seats.declared_execution_seats", lambda: rows)
    return rows


@pytest.mark.asyncio
async def test_http_spawn_selects_companion_seat(selectable, seats):
    from kiro_crew.dashboard.handlers.messaging import api_spawn

    _request, manager = _real_request({})
    state = _request.app["state"]

    @web.middleware
    async def internal(request, handler):
        request["internal_auth"] = True
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[internal])
    app["state"] = state
    app.router.add_post("/api/spawn", api_spawn)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/spawn", json={"task": "hello", "backend": "antigravity"})
        assert response.status == 200, await response.text()
        receipt = await response.json()
    info = manager.get(receipt["id"])
    assert info.runtime_agent == "crew-antigravity"
    assert info.backend is None
    assert receipt["execution_seat"] == "antigravity"
    assert receipt["runtime_agent"] == "crew-antigravity"
    assert "Execution seat" in receipt["runtime_note"]


def test_seat_reaches_provider_factory_without_native_override():
    captured, shared = _run(None, runtime_agent="crew-antigravity")
    shared.assert_not_called()
    assert captured["runtime_agent"] == "crew-antigravity"
    assert "backend_override" not in captured


@pytest.mark.parametrize("explicit,expected", [("", ""), ("gemini-explicit", "gemini-explicit")])
def test_seat_does_not_inherit_native_model_or_effort_defaults(monkeypatch, explicit, expected):
    from kiro_crew import subagent
    from kiro_crew.subagent import SubagentInfo

    manager = _mgr()
    monkeypatch.setattr(subagent, "_subagent_default_model", lambda _cfg: "claude-native-only")
    monkeypatch.setattr(subagent, "_subagent_default_effort", lambda _cfg: "high")
    info = SubagentInfo(
        id="seat-model",
        task="hello",
        runtime_agent="crew-antigravity",
        model=explicit,
        execution_context=execution_for_store(""),
    )
    plan = manager._sharing_plan(info)
    assert plan.eff_model == expected
    assert plan.eff_effort == ""
    assert plan.shared is False


@pytest.mark.asyncio
async def test_unknown_backend_refusal_lists_declared_seats(selectable, seats):
    from kiro_crew.dashboard.handlers.messaging import api_spawn

    request, manager = _real_request({"task": "hello", "backend": "missing-seat"})
    response = await api_spawn(request)
    assert response.status == 400
    assert json.loads(response.text)["execution_seats"] == ["antigravity"]
    assert not manager._agents


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [[], {}])
async def test_non_string_backend_is_refused_before_seat_lookup(seats, backend):
    from kiro_crew.dashboard.handlers.messaging import api_spawn

    request, manager = _real_request({"task": "hello", "backend": backend})
    response = await api_spawn(request)
    assert response.status == 400
    assert not manager._agents


def test_continuation_and_persistence_preserve_seat(monkeypatch, tmp_path):
    from kiro_crew import subagent_persistence
    from kiro_crew.subagent import SubagentInfo

    monkeypatch.setattr(subagent_persistence, "_subagents_dir", lambda: tmp_path)
    manager = _mgr()
    info = SubagentInfo(
        id="seat-run",
        task="hello",
        runtime_agent="crew-antigravity",
        execution_context=execution_for_store(""),
    )
    manager._agents[info.id] = info
    manager._log_spawned(info)
    assert subagent_persistence.read_state(info.id)["runtime_agent"] == "crew-antigravity"
    assert (
        _FollowUps()._continue(manager, monkeypatch, info.id)["runtime_agent"] == "crew-antigravity"
    )


def test_continuation_after_restart_reads_recorded_seat(monkeypatch):
    manager = _mgr()
    monkeypatch.setattr(
        "kiro_crew.subagent.read_state",
        lambda _id: {"runtime_agent": "crew-antigravity"},
    )
    monkeypatch.setattr(manager, "_inherited_memory_store", lambda _id: "")
    monkeypatch.setattr(manager._continuation._persistence, "read_run_app", lambda _id: "")
    assert (
        _FollowUps()._continue(manager, monkeypatch, "finished-run")["runtime_agent"]
        == "crew-antigravity"
    )


def test_queued_spawn_retains_execution_seat():
    manager = _mgr()
    manager._should_stagger_queue = MagicMock(return_value=(True, False))
    manager.spawn("hello", runtime_agent="crew-antigravity")
    assert manager._queue[0]["runtime_agent"] == "crew-antigravity"
    captured = {}
    manager.spawn = lambda **kwargs: captured.update(kwargs)
    manager._max_concurrent = 4
    manager._running_count = 0
    manager._spawn_stagger_secs = 0
    manager._drain_queue()
    assert captured["runtime_agent"] == "crew-antigravity"


def test_native_backend_and_seat_cannot_both_be_selected():
    manager = _mgr()
    with pytest.raises(ValueError, match="both"):
        manager.spawn("hello", backend="claude", runtime_agent="crew-antigravity")


def _member_execution(worker=False):
    from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

    return ExecutionContext(
        "member-owner",
        MemoryStoreRef("member-owner", "member-owner"),
        "template",
        "kirocrew-mate-worker" if worker else "member-template",
    )


def _seat_policy(monkeypatch, policy):
    lookup = MagicMock(return_value=policy)
    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: SimpleNamespace(providers=SimpleNamespace(agent_runtime_policy=lookup)),
    )
    return lookup


@pytest.fixture
def member_seat_config(monkeypatch):
    from kiro_crew.config import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agents["crew-antigravity"] = SimpleNamespace(member_id="", kiro_agent="agy-template")
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    return cfg


@pytest.mark.parametrize(
    "policy",
    [
        {"backend": "claude"},
        {"backend": "codex"},
        {"backend": "antigravity_headless", "member_capabilities": True},
    ],
)
def test_verified_member_seats_keep_memory_and_template(monkeypatch, member_seat_config, policy):
    lookup = _seat_policy(monkeypatch, policy)
    execution = _member_execution()
    validate_member_execution_seat("crew-antigravity", execution)
    lookup.assert_called_once_with("agy-template")
    assert execution.member_id == "member-owner"
    assert execution.template_id == "member-template"


@pytest.mark.parametrize("worker_support", [True, False])
def test_worker_requires_its_own_kit_support(monkeypatch, member_seat_config, worker_support):
    _seat_policy(
        monkeypatch,
        {
            "backend": "antigravity_headless",
            "member_capabilities": True,
            "member_worker_kit": worker_support,
        },
    )
    if worker_support:
        validate_member_execution_seat("crew-antigravity", _member_execution(worker=True))
    else:
        with pytest.raises(ValueError, match="runtime_member_capabilities_unverified"):
            validate_member_execution_seat("crew-antigravity", _member_execution(worker=True))


@pytest.mark.asyncio
async def test_member_seat_refuses_before_run_registration(monkeypatch, member_seat_config):
    manager = _mgr()
    monkeypatch.setattr(
        type(manager._admission), "resolve_spawn_execution", lambda _self, **kw: _member_execution()
    )
    _seat_policy(monkeypatch, None)
    run = manager.spawn("hello", runtime_agent="crew-antigravity")
    assert run.done
    assert run.error.startswith("runtime_member_capabilities_unverified")
    assert not manager._agents
    assert not manager._queue


def test_member_bound_run_reaches_selected_seat(monkeypatch):
    import test_spawn_backend

    execution = _member_execution()
    published = MagicMock()
    monkeypatch.setattr(test_spawn_backend, "execution_for_store", lambda store: execution)
    monkeypatch.setattr("kiro_crew.execution_context.bind_session_execution", published)
    monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", lambda *a, **kw: None)
    _seat_policy(monkeypatch, {"member_capabilities": True})
    captured, shared = _run(
        None,
        runtime_agent="crew-antigravity",
        agents={"crew-antigravity": SimpleNamespace(member_id="", kiro_agent="agy-template")},
    )
    assert captured["runtime_agent"] == "crew-antigravity"
    shared.assert_not_called()
    assert published.call_args.args[1] == execution


@pytest.mark.parametrize(
    "name,member,expected",
    [
        ("antigravity", "", True),
        ("claude", "", False),
        ("antigravity", "private-member", False),
    ],
)
def test_declarations_require_non_member_alias_and_cannot_shadow_native(
    monkeypatch, name, member, expected
):
    provider = SimpleNamespace(
        execution_seats=lambda _cfg: [{"id": name, "runtime_agent": "seat-template"}]
    )
    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: SimpleNamespace(providers=provider),
    )
    cfg = SimpleNamespace(agents={"seat-template": SimpleNamespace(member_id=member)})
    if expected:
        assert declared_execution_seats(cfg)[name]["runtime_agent"] == "seat-template"
    else:
        with pytest.raises(ValueError):
            declared_execution_seats(cfg)
