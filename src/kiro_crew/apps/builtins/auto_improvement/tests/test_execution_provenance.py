from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.platform.app_execution import (
    authenticated_app_execution,
    capture_app_execution,
    current_app_execution,
)

from .. import profiles
from ..backend import clone_setup, routes, runner, store


def request_for(user="operator", app="", dashboard=True):
    request = make_mocked_request("POST", "/")
    request["user"] = user
    request["app"] = app
    request["is_dashboard_user"] = dashboard
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["start", "calibrate"])
@pytest.mark.parametrize("authenticated", [True, False])
async def test_route_provenance_reaches_real_worker(monkeypatch, method, authenticated):
    supervisor = runner.RunSupervisor()
    seen = []
    monkeypatch.setattr(runner, "get_supervisor", lambda: supervisor)
    monkeypatch.setattr(store, "read_json", lambda *args: {"clone": "fixture", "app": "forged"})

    def observe(*args):
        seen.append(current_app_execution())

    monkeypatch.setattr(supervisor, "_build_driver", observe)
    monkeypatch.setattr(supervisor, "_run_loop", observe)
    monkeypatch.setattr(supervisor, "_calibrate_loop", observe)
    request = request_for() if authenticated else make_mocked_request("POST", "/")
    handler = routes._handle_run_start if method == "start" else routes._handle_calibrate
    response = await handler(request)
    assert response.status == 200
    assert supervisor._thread is not None
    await asyncio.to_thread(supervisor._thread.join, 2)
    assert not supervisor._thread.is_alive()
    assert len(seen) == (2 if method == "start" else 1)
    if authenticated:
        assert all(
            item is not None and item.app == store.APP_NAME and item.user == "operator"
            for item in seen
        )
    else:
        assert seen == [None] * len(seen)
    assert current_app_execution() is None


@pytest.mark.parametrize("method", ["start", "calibrate"])
def test_direct_supervisor_config_cannot_supply_provenance(monkeypatch, method):
    supervisor = runner.RunSupervisor()
    seen = []
    monkeypatch.setattr(supervisor, "_build_driver", lambda config: None)
    monkeypatch.setattr(
        supervisor, "_run_loop", lambda driver: seen.append(current_app_execution())
    )
    monkeypatch.setattr(
        supervisor, "_calibrate_loop", lambda config: seen.append(current_app_execution())
    )
    getattr(supervisor, method)(
        {"app": store.APP_NAME, "user": "operator", "session_key": "dashboard:fake"}
    )
    assert supervisor._thread is not None
    supervisor._thread.join(2)
    assert not supervisor._thread.is_alive()
    assert seen == [None]


@pytest.mark.asyncio
async def test_environment_check_carries_app_identity(monkeypatch):
    seen = []
    monkeypatch.setattr(routes, "_json_body", AsyncMock(return_value={}))
    monkeypatch.setattr(routes, "_refuse_while_running", AsyncMock(return_value=None))
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    monkeypatch.setattr(store, "read_json", lambda *args: {"clone": "fixture", "app": "forged"})
    monkeypatch.setattr(clone_setup, "_repository_is_safe", lambda root: True)
    monkeypatch.setattr(clone_setup, "_push_disabled", lambda root: True)
    monkeypatch.setattr(clone_setup, "checkout_branch", lambda *args, **kwargs: (True, ""))

    def check():
        seen.append(current_app_execution())
        return {"ok": True}

    monkeypatch.setattr(
        profiles,
        "build_profile",
        lambda config: SimpleNamespace(
            environment=SimpleNamespace(identity={"kind": "gateway"}),
            isolation=SimpleNamespace(push_disabled=lambda: True),
            check_environment=check,
        ),
    )
    response = await routes._handle_environment_check(
        request_for(app=store.APP_NAME, dashboard=False)
    )
    assert response.status == 200
    assert seen[0] is not None
    assert seen[0].app == store.APP_NAME
    assert seen[0].user == "operator"
    assert current_app_execution() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unverified_request",
    [
        {},
        {"user": "operator"},
        {"user": "operator", "app": "other", "is_dashboard_user": False},
        {"user": "operator", "app": "", "is_dashboard_user": False},
    ],
)
async def test_unverified_request_has_no_authority(unverified_request):
    @authenticated_app_execution(store.APP_NAME)
    async def handler(request):
        return await asyncio.to_thread(current_app_execution)

    assert await handler(unverified_request) is None


@pytest.mark.asyncio
async def test_snapshot_is_immutable_secret_free_and_scoped(monkeypatch):
    from kiro_crew.dashboard import token_auth

    claims_calls = []

    def claims(token, keys):
        claims_calls.append((token, keys))
        return {"session_key": "slack:verified"}

    monkeypatch.setattr(token_auth, "extract_claims_from_token", claims)
    request = request_for()
    request["auth_token"] = "credential-not-to-retain"
    captured = []

    @authenticated_app_execution(store.APP_NAME)
    async def handler(request):
        context = current_app_execution()
        assert context is not None
        captured.append(context)
        with pytest.raises(FrozenInstanceError):
            setattr(context, "app", "other")
        request["user"] = "changed"

        def fail():
            assert current_app_execution() is context
            raise RuntimeError("worker failed")

        return capture_app_execution(fail)

    worker = await handler(request)
    assert current_app_execution() is None
    with pytest.raises(RuntimeError, match="worker failed"):
        await asyncio.to_thread(worker)
    assert await asyncio.to_thread(current_app_execution) is None
    assert captured[0].user == "operator"
    assert captured[0].session_key == "slack:verified"
    assert "credential-not-to-retain" not in repr(captured[0])
    assert claims_calls == [("credential-not-to-retain", ("session_key",))]


@pytest.mark.asyncio
async def test_concurrent_routes_do_not_share_principals():
    arrived = asyncio.Event()
    count = 0

    @authenticated_app_execution(store.APP_NAME)
    async def handler(request):
        nonlocal count
        count += 1
        if count == 2:
            arrived.set()
        await asyncio.wait_for(arrived.wait(), timeout=2)
        return await asyncio.to_thread(current_app_execution)

    first, second = await asyncio.gather(
        handler(request_for(user="first")), handler(request_for(user="second"))
    )
    assert (first.user, second.user) == ("first", "second")
    assert current_app_execution() is None


@pytest.mark.asyncio
async def test_headers_do_not_supply_a_session():
    request = make_mocked_request("POST", "/", headers={"X-Session-Key": "dashboard:forged"})
    request["user"] = "operator"
    request["app"] = ""
    request["is_dashboard_user"] = True

    @authenticated_app_execution(store.APP_NAME)
    async def handler(request):
        return current_app_execution()

    assert (await handler(request)).session_key == ""
