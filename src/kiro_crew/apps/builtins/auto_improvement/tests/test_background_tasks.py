"""Long Setup tasks outlive the page and announce how they ended."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from .. import profiles
from ..backend import notify, routes, runner, store
from .request_helpers import owner_request


def _request(body: dict):
    req = owner_request("POST", "/environment/check")
    req._read_bytes = json.dumps(body).encode("utf-8")
    return req


async def _status() -> dict:
    return json.loads((await routes._handle_environment_check_status(_request({}))).text)


@pytest.fixture
def check_route(tmp_path, monkeypatch):
    """A configured repository whose profile check is supplied by each test."""

    async def free(_):
        return None

    store.write_json_atomic(
        store.config_path(),
        {"clone": str(tmp_path), "branch": "main", "target_display": "acme/widgets"},
    )
    monkeypatch.setattr(routes, "_refuse_while_running", free)
    monkeypatch.setattr(routes, "_run_is_active", lambda: False)
    monkeypatch.setattr(routes.clone_setup, "_repository_is_safe", lambda path: True)
    monkeypatch.setattr(routes.clone_setup, "_push_disabled", lambda path: True)
    monkeypatch.setattr(
        routes.clone_setup, "checkout_branch", lambda path, branch, *, config: (True, "")
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notify, "task_finished", lambda title, body, *, group_key: sent.append((title, body))
    )

    def use(check):
        monkeypatch.setattr(
            profiles,
            "build_profile",
            lambda config: SimpleNamespace(
                environment=SimpleNamespace(identity={"kind": "gateway"}),
                isolation=SimpleNamespace(push_disabled=lambda: True),
                check_environment=check,
            ),
        )
        return sent

    return use


@pytest.mark.asyncio
async def test_a_failed_check_is_kept_and_announced(check_route):
    sent = check_route(
        lambda: {"ok": False, "diagnostic": {"stage": "collection", "stderr": "ImportError: x"}}
    )
    response = await routes._handle_environment_check(_request({"fingerprint": "draft-1"}))
    assert response.status == 202
    routes._check_thread.join(10)

    record = await _status()
    assert record["state"] == "blocked"
    assert record["fingerprint"] == "draft-1"
    assert record["result"]["diagnostic"]["stage"] == "collection"
    assert sent == [
        ("Environment check failed · acme/widgets", "Stopped at collection: ImportError: x")
    ]


@pytest.mark.asyncio
async def test_a_passing_check_reports_its_test_count(check_route):
    sent = check_route(lambda: {"ok": True, "tests_collected": 8253})
    await routes._handle_environment_check(_request({}))
    routes._check_thread.join(10)

    assert (await _status())["state"] == "ready"
    assert sent == [("Environment ready · acme/widgets", "8253 tests collected.")]


@pytest.mark.asyncio
async def test_a_second_check_waits_for_the_first(check_route):
    release = threading.Event()

    def slow():
        release.wait(10)
        return {"ok": True, "tests_collected": 1}

    check_route(slow)
    first = await routes._handle_environment_check(_request({}))
    second = await routes._handle_environment_check(_request({}))
    release.set()
    routes._check_thread.join(10)

    assert first.status == 202
    assert second.status == 409
    assert json.loads(second.text)["code"] == "check_in_progress"


@pytest.mark.asyncio
async def test_a_check_cut_off_by_a_restart_reads_as_interrupted(monkeypatch):
    monkeypatch.setattr(routes, "_check_thread", None)
    store.write_json_atomic(routes._environment_check_path(), {"state": "checking"})
    assert (await _status())["state"] == "interrupted"


@pytest.mark.asyncio
async def test_no_check_yet_reads_as_none():
    assert (await _status()) == {"state": "none"}


@pytest.mark.asyncio
async def test_an_oversized_fingerprint_is_refused(check_route):
    check_route(lambda: {"ok": True})
    response = await routes._handle_environment_check(_request({"fingerprint": "x" * 20000}))
    assert response.status == 400


class _Bus:
    def __init__(self):
        self.pushed = []
        self.channels = {}

    def is_registered(self, channel):
        return channel in self.channels

    def register_channel(self, channel, priority):
        self.channels[channel] = priority

    def push(self, payload):
        self.pushed.append(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared,allowed,expected", [(True, True, 1), (False, True, 0), (True, False, 0)]
)
async def test_notes_pass_the_producer_gates(monkeypatch, declared, allowed, expected):
    from kiro_crew.dashboard.handlers import notifications_push

    bus = _Bus()
    state = SimpleNamespace(
        notification_bus=bus,
        notification_rate_limiter=SimpleNamespace(allow=lambda app: allowed),
    )
    monkeypatch.setattr(
        notifications_push,
        "_resolve_app_channels",
        lambda app: {notify.CHANNEL: "normal"} if declared else {"other": "normal"},
    )
    monkeypatch.setattr(notify, "_bound", None)  # restored after the test
    notify.bind(SimpleNamespace(app={"state": state}))

    await asyncio.to_thread(notify.task_finished, "Run finished · x", "Kept 1.", group_key="run")
    await asyncio.sleep(0)

    assert len(bus.pushed) == expected
    if expected:
        assert bus.pushed[0].channel == f"{store.APP_NAME}.{notify.CHANNEL}"
        assert bus.pushed[0].url == "/auto-improvement"


@pytest.mark.parametrize(
    "status,ruler,title",
    [
        (runner.STATUS_DONE, "calibrated", "Calibration finished · acme/widgets"),
        (runner.STATUS_DONE, "uncalibrated", "Calibration stopped · acme/widgets"),
        (runner.STATUS_ERROR, "canary_failed", "Calibration failed · acme/widgets"),
    ],
)
def test_calibration_end_is_announced(monkeypatch, status, ruler, title):
    store.write_json_atomic(store.config_path(), {"target_display": "acme/widgets"})
    store.write_json_atomic(store.ruler_dir() / "ruler.json", {"status": ruler})
    sent = []
    monkeypatch.setattr(
        notify, "task_finished", lambda t, body, *, group_key: sent.append((t, body))
    )
    supervisor = runner.RunSupervisor()
    supervisor._state.status = status
    supervisor._state.error = "canary did not clear the noise band" if status == "error" else ""

    supervisor._notifying(lambda: None, "Calibration")()

    assert sent and sent[0][0] == title


@pytest.mark.asyncio
async def test_starting_a_run_clears_the_last_check(monkeypatch):
    store.write_json_atomic(routes._environment_check_path(), {"state": "ready"})

    class Supervisor:
        def stop_seq(self):
            return 0

        def start(self, config, *, admitted_at):
            return {"status": "running"}

    monkeypatch.setattr(routes.runner, "get_supervisor", lambda: Supervisor())
    monkeypatch.setattr(routes, "is_app_enabled", lambda name: True)
    req = owner_request("POST", "/run/start")
    req._read_bytes = b"{}"
    response = await routes._handle_run_start(req)

    assert response.status == 200, response.text
    assert (await _status()) == {"state": "none"}
