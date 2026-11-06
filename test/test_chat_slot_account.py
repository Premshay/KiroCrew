import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.platform.defaults import DefaultProviderRegistry


def test_default_provider_registry_session_account_seam_is_noop() -> None:
    registry = DefaultProviderRegistry()
    assert registry.session_account_state("dashboard:chat-1", "claude-engine") is None
    assert registry.set_session_account("dashboard:chat-1", "atlomy", "claude-engine") == {
        "ok": False,
        "available": False,
    }


@pytest.fixture
def account_state(monkeypatch):
    slot = _ChatSlot("chat-account")
    slot.agent = "derrick"
    slot.mode = "member"
    slot.messages = [{"role": "user", "text": "hello"}]
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.get_provider.return_value = None

    cfg = KiroCrewConfig.load()
    cfg.agents = {
        "derrick": KiroCrewAgentConfig(kiro_agent="crew-derrick", member_id="derrick"),
        "codex": KiroCrewAgentConfig(kiro_agent="codex-engine"),
    }
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)

    store = {"current": "personal", "default": "personal"}
    calls: list[tuple[str, tuple]] = []

    def _policy(name: str):
        if name in ("derrick", "crew-derrick"):
            return {"engine": "claude-max", "backend": "claude", "priority": 0}
        if name in ("codex", "codex-engine"):
            return {"engine": "codex", "backend": "codex", "priority": 1}
        return None

    def _session_account_state(session_key: str, engine_identity: str = ""):
        calls.append(("state", (session_key, engine_identity)))
        eligible = engine_identity in ("derrick", "crew-derrick")
        return {
            "available": True,
            "eligible": eligible,
            "reason": "" if eligible else "Not a Claude subscription seat (codex)",
            "current": store["current"],
            "default": store["default"],
            "default_label": "Personal" if store["default"] == "personal" else "Atlomy",
            "accounts": [
                {"id": "personal", "label": "Personal"},
                {"id": "atlomy", "label": "Atlomy"},
            ],
        }

    def _set_session_account(session_key: str, account: str, engine_identity: str = ""):
        calls.append(("set", (session_key, account, engine_identity)))
        store["current"] = account
        return _session_account_state(session_key, engine_identity)

    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: SimpleNamespace(
            providers=SimpleNamespace(
                agent_runtime_policy=_policy,
                session_account_state=_session_account_state,
                set_session_account=_set_session_account,
            )
        ),
    )

    reloaded = AsyncMock(
        return_value=web.json_response({"ok": True, "slot": slot.key, "reloaded": True})
    )
    monkeypatch.setattr(chat_handlers, "reload_slot_session", reloaded)
    return state, slot, store, calls, reloaded


def _account_app(state: DashboardState) -> web.Application:
    app = as_owner(web.Application())
    app["state"] = state
    app.router.add_route(
        "GET", "/api/chat/slots/{slot}/account", chat_handlers.api_chat_slot_account
    )
    app.router.add_route(
        "POST", "/api/chat/slots/{slot}/account", chat_handlers.api_chat_slot_account
    )
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude", "codex"])
async def test_native_account_read_and_write_receive_effective_harness(
    account_state, monkeypatch, backend
):
    from kiro_crew.platform.context import current_context

    state, slot, store, calls, reloaded = account_state
    providers = current_context().providers
    observed = []

    def read(key, identity="", *, backend=None):
        observed.append(("read", backend))
        return {
            "available": True,
            "eligible": backend == "claude",
            "current": store["current"],
            "accounts": [{"id": "personal"}, {"id": "atlomy"}],
        }

    def write(key, account, identity="", *, backend=None):
        observed.append(("write", backend))
        store["current"] = account
        return read(key, identity, backend=backend)

    providers.session_account_state = read
    providers.set_session_account = write
    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context", lambda: SimpleNamespace(providers=providers)
    )
    monkeypatch.setattr(
        "kiro_crew.crewmate_harness.crewmate_backend_pin",
        lambda *args: SimpleNamespace(backend=backend),
    )
    async with TestClient(TestServer(_account_app(state))) as client:
        response = await client.get(f"/api/chat/slots/{slot.key}/account")
        assert response.status == 200
        assert (await response.json())["eligible"] == (backend == "claude")
        response = await client.post(
            f"/api/chat/slots/{slot.key}/account", json={"account": "atlomy"}
        )
        assert response.status == (200 if backend == "claude" else 409)
    assert ("read", backend) in observed
    assert (("write", backend) in observed) == (backend == "claude")
    assert reloaded.await_count == (1 if backend == "claude" else 0)


@pytest.mark.asyncio
async def test_explicit_execution_seat_keeps_account_eligibility(account_state, monkeypatch):
    state, slot, store, calls, reloaded = account_state
    slot.runtime_agent = "codex"
    pin_read = MagicMock(side_effect=AssertionError("an explicit seat must not read the bot pin"))
    monkeypatch.setattr("kiro_crew.crewmate_harness.crewmate_backend_pin", pin_read)
    async with TestClient(TestServer(_account_app(state))) as client:
        response = await client.get(f"/api/chat/slots/{slot.key}/account")
        assert response.status == 200
        assert not (await response.json())["eligible"]
    pin_read.assert_not_called()


@pytest.mark.asyncio
async def test_runtime_label_uses_native_bot_pin(account_state, monkeypatch):
    state, slot, store, calls, reloaded = account_state
    monkeypatch.setattr(
        "kiro_crew.crewmate_harness.crewmate_backend_pin",
        lambda *args: SimpleNamespace(backend="codex"),
    )
    app = _account_app(state)
    app.router.add_get("/api/chat/slots/{slot}/runtime", chat_handlers.api_chat_slot_runtime)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/chat/slots/{slot.key}/runtime")
        assert response.status == 200
        result = await response.json()
        assert result["effective_backend"] == "codex"
        assert result["effective_runtime_agent"] == ""


@pytest.mark.asyncio
async def test_get_slot_account_returns_state_for_eligible_session(account_state) -> None:
    state, slot, _, calls, _ = account_state
    async with TestClient(TestServer(_account_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{slot.key}/account")
        assert resp.status == 200
        body = await resp.json()
    assert body == {
        "available": True,
        "eligible": True,
        "reason": "",
        "current": "personal",
        "default": "personal",
        "default_label": "Personal",
        "accounts": [
            {"id": "personal", "label": "Personal"},
            {"id": "atlomy", "label": "Atlomy"},
        ],
    }
    assert calls == [("state", (effective_session_key(slot), "crew-derrick"))]


@pytest.mark.asyncio
async def test_get_slot_account_when_edition_contributes_none(account_state, monkeypatch) -> None:
    state, slot, _, _, _ = account_state
    monkeypatch.setattr(
        "kiro_crew.platform.context.current_context",
        lambda: SimpleNamespace(providers=DefaultProviderRegistry()),
    )
    async with TestClient(TestServer(_account_app(state))) as client:
        resp = await client.get(f"/api/chat/slots/{slot.key}/account")
        assert resp.status == 200
        body = await resp.json()
    assert body["available"] is False
    assert body["eligible"] is False
    assert body["accounts"] == []


@pytest.mark.asyncio
async def test_post_slot_account_switches_and_reloads_idle_session(account_state) -> None:
    state, slot, store, calls, reloaded = account_state
    async with TestClient(TestServer(_account_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{slot.key}/account", json={"account": "atlomy"})
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True
    assert body["account"] == "atlomy"
    assert body["current"] == "atlomy"
    assert body["deferred"] is False
    assert store["current"] == "atlomy"
    assert ("set", (effective_session_key(slot), "atlomy", "crew-derrick")) in calls
    reloaded.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_slot_account_defers_reset_when_turn_in_flight(account_state) -> None:
    state, slot, store, _, reloaded = account_state
    reloaded.return_value = web.json_response(
        {"error": "cannot reload session while a turn is running", "code": "turn_in_flight"},
        status=409,
    )
    async with TestClient(TestServer(_account_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{slot.key}/account", json={"account": "atlomy"})
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True
    assert body["account"] == "atlomy"
    assert body["current"] == "atlomy"
    assert body["deferred"] is True
    assert store["current"] == "atlomy"
    assert slot._pending_reset_history_key == effective_session_key(slot)


@pytest.mark.asyncio
async def test_post_slot_account_refuses_ineligible_seat(account_state) -> None:
    state, slot, store, calls, reloaded = account_state
    slot.runtime_agent = "codex"
    async with TestClient(TestServer(_account_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{slot.key}/account", json={"account": "atlomy"})
        assert resp.status == 409
        body = await resp.json()
    assert body["code"] == "session_account_ineligible"
    assert "Not a Claude subscription seat" in body["error"]
    assert store["current"] == "personal"
    assert not any(kind == "set" for kind, _ in calls)
    reloaded.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_slot_account_refuses_unknown_account(account_state) -> None:
    state, slot, store, calls, reloaded = account_state
    async with TestClient(TestServer(_account_app(state))) as client:
        resp = await client.post(
            f"/api/chat/slots/{slot.key}/account", json={"account": "nonexistent"}
        )
        assert resp.status == 400
        body = await resp.json()
    assert body["code"] == "invalid_session_account"
    assert store["current"] == "personal"
    assert not any(kind == "set" for kind, _ in calls)
    reloaded.assert_not_awaited()


@pytest.mark.asyncio
async def test_slot_account_route_is_owner_only(account_state) -> None:
    state, slot, _, calls, reloaded = account_state
    async with TestClient(TestServer(_account_app(state))) as client:
        get_resp = await client.get(
            f"/api/chat/slots/{slot.key}/account", headers={"X-Test-App": "plugin"}
        )
        post_resp = await client.post(
            f"/api/chat/slots/{slot.key}/account",
            data=json.dumps({"account": "atlomy"}),
            headers={"Content-Type": "application/json", "X-Test-App": "plugin"},
        )
        assert get_resp.status == 403
        assert post_resp.status == 403
    assert calls == []
    reloaded.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_menu_follows_the_current_agent_across_switches(account_state) -> None:
    state, slot, store, calls, _ = account_state
    store["current"] = "atlomy"
    seen: list[tuple[bool, str]] = []
    async with TestClient(TestServer(_account_app(state))) as client:
        for agent in ("derrick", "codex", "derrick"):
            slot.agent = agent
            resp = await client.get(f"/api/chat/slots/{slot.key}/account")
            assert resp.status == 200
            body = await resp.json()
            seen.append((body["eligible"], body["current"]))
    assert seen == [(True, "atlomy"), (False, "atlomy"), (True, "atlomy")]
    assert [c[1][1] for c in calls if c[0] == "state"] == [
        "crew-derrick",
        "codex-engine",
        "crew-derrick",
    ]


@pytest.mark.asyncio
async def test_post_slot_account_refused_while_session_is_on_codex(account_state) -> None:
    state, slot, store, calls, reloaded = account_state
    slot.agent = "codex"
    async with TestClient(TestServer(_account_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{slot.key}/account", json={"account": "atlomy"})
        assert resp.status == 409
        body = await resp.json()
    assert body["code"] == "session_account_ineligible"
    assert store["current"] == "personal"
    assert not any(c[0] == "set" for c in calls)
    reloaded.assert_not_awaited()


def test_account_switch_shares_the_agent_switch_lock_order() -> None:
    """Account and agent switches must serialise on the same two locks, in one order."""
    import ast
    import inspect

    from kiro_crew.dashboard.chat_api import agent_switch, session_relaunch

    def _entered(func) -> list[str]:
        tree = ast.parse(inspect.getsource(func))
        names: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "enter_async_context"
                and node.args
            ):
                arg = node.args[0]
                names.append(
                    arg.func.id
                    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                    else ast.unparse(arg)
                )
        return names

    route_src = inspect.getsource(session_relaunch.api_chat_slot_account)
    assert "reload_slot_session(" in route_src
    assert "enter_async_context" not in route_src
    expected = ["slot._lock", "_slot_switch_session_lock"]
    assert _entered(session_relaunch.reload_slot_session)[:2] == expected
    assert _entered(agent_switch.switch_slot_agent)[:2] == expected
