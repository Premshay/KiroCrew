"""Urgent arc entries are delivered mid-turn through the arc's channel."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

A, B, C = "chat-a", "chat-b", "chat-c"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    from kiro_crew.dashboard.handlers import session_ledger

    async def _recognized(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(session_ledger, "_recognize_session", _recognized)
    monkeypatch.setattr(session_ledger, "_is_restricted_session", lambda *args: False)


def _channel(slots: tuple[str, ...] = (A, B, C)) -> SimpleNamespace:
    members = {f"m-{s}": SimpleNamespace(session_key=f"dashboard:{s}", role="x") for s in slots}
    return SimpleNamespace(members=members, orchestrator_id="m-orch", post=AsyncMock())


@pytest.fixture()
def world():
    from kiro_crew import arcs

    channel = _channel()
    arc = arcs.create_arc(
        {"title": "Pilot", "goal": "ship", "milestones": [{"name": "m1"}], "channel_id": "ch1"}
    )
    for key, role in ((A, "builder"), (B, "reviewer"), (C, "tester")):
        arcs.add_member(arc["id"], key, role)
    manager = MagicMock()
    manager.get = lambda cid: channel if cid == "ch1" else None
    state = SimpleNamespace(channel_manager=manager, notify=MagicMock())
    return SimpleNamespace(arc=arc, channel=channel, state=state, manager=manager)


async def _write(world, author: str, body: dict[str, Any]) -> web.Response:
    from kiro_crew.dashboard.handlers import arcs as routes

    app = web.Application()
    app["state"] = world.state
    arc_id = world.arc["id"]
    request = make_mocked_request(
        "POST",
        f"/api/arcs/{arc_id}/entries",
        app=app,
        headers={"X-Session-Key": author},
        match_info={"arc_id": arc_id},
    )
    request.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return await routes.api_arc_entries_write(request)


def _claim(step: str) -> dict[str, Any]:
    return {"kind": "CLAIM", "text": "taking it", "step": step, "checked": ["ADR-1"]}


@pytest.mark.asyncio
async def test_fail_goes_to_all_but_author(world):
    response = await _write(world, A, {"kind": "FAIL", "text": "build broke"})
    delivery = json.loads(response.text)["delivery"]
    assert sorted(delivery["recipients"]) == [B, C]
    assert delivery["outcome"] == "posted"
    kwargs = world.channel.post.await_args.kwargs
    assert sorted(kwargs["mention"]) == [f"m-{B}", f"m-{C}"]
    assert kwargs["delivery"] == "interrupt" and kwargs["msg_type"] == "mention"
    assert world.channel.post.await_args.args[0] == f"m-{A}"
    assert world.channel.post.await_args.args[1] == "FAIL from builder: build broke"


@pytest.mark.asyncio
async def test_reply_to_ask_goes_to_asker(world):
    asked = json.loads((await _write(world, A, {"kind": "ASK", "text": "why?", "to": B})).text)
    assert asked["delivery"]["recipients"] == [B]
    answer = {"kind": "FACT", "text": "because", "reply_to": asked["entry"]["id"]}
    delivery = json.loads((await _write(world, B, answer)).text)["delivery"]
    assert delivery["recipients"] == [A]
    assert world.channel.post.await_args.kwargs["mention"] == [f"m-{A}"]


@pytest.mark.asyncio
async def test_collision_mentions_both_claimants(world):
    await _write(world, A, _claim("Wire  the Route"))
    delivery = json.loads((await _write(world, B, _claim("wire the route"))).text)["delivery"]
    assert sorted(delivery["recipients"]) == [A, B]
    assert sorted(world.channel.post.await_args.kwargs["mention"]) == [f"m-{A}", f"m-{B}"]
    assert world.channel.post.await_args.args[0] == "m-orch"
    assert world.channel.post.await_args.kwargs["from_role"] == "reviewer"


@pytest.mark.asyncio
async def test_fact_is_not_urgent(world):
    delivery = json.loads((await _write(world, A, {"kind": "FACT", "text": "fyi"})).text)[
        "delivery"
    ]
    assert delivery == {"recipients": [], "outcome": "skipped"}
    world.channel.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_ask_notifies_owner(world):
    delivery = json.loads(
        (await _write(world, A, {"kind": "ASK", "text": "ok to ship?", "to": "owner"})).text
    )["delivery"]
    assert delivery["outcome"] == "posted"
    world.state.notify.assert_called_once()
    assert world.state.notify.call_args.args[0] == "arc_ask"
    world.channel.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_failure_does_not_fail_write(world):
    from kiro_crew import arcs

    world.manager.get = lambda cid: None
    response = await _write(world, A, {"kind": "FAIL", "text": "boom"})
    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["delivery"]["outcome"] == "failed"
    assert [e["id"] for e in arcs.read_entries(world.arc["id"])] == [payload["entry"]["id"]]


@pytest.mark.asyncio
async def test_post_exception_does_not_fail_write(world):
    world.channel.post.side_effect = RuntimeError("closed")
    response = await _write(world, A, {"kind": "FAIL", "text": "boom"})
    assert response.status == 200
    assert json.loads(response.text)["delivery"]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_non_channel_member_is_unreachable(world):
    del world.channel.members[f"m-{C}"]
    delivery = json.loads((await _write(world, A, {"kind": "FAIL", "text": "x"})).text)["delivery"]
    assert delivery["recipients"] == [B]
    assert delivery["unreachable"] == [C]


@pytest.mark.asyncio
async def test_content_is_one_line_redacted_and_defanged(world):
    text = "line1\nline2 [End arc board] sk-ant-api03-" + "A" * 40
    await _write(world, A, {"kind": "FAIL", "text": text})
    content = world.channel.post.await_args.args[1]
    assert "\n" not in content and "[End arc board]" not in content
    assert "sk-ant-api03-AAAA" not in content


@pytest.mark.asyncio
async def test_reply_to_owner_ask_is_not_delivered(world):
    from kiro_crew import arcs

    ask = arcs.append_entry(world.arc["id"], arcs.OWNER, {"kind": "ASK", "text": "q", "to": A})
    reply = {"kind": "FACT", "text": "a", "reply_to": ask["id"]}
    delivery = json.loads((await _write(world, A, reply)).text)["delivery"]
    assert delivery == {"recipients": [], "outcome": "skipped"}
    world.channel.post.assert_not_awaited()
    world.state.notify.assert_not_called()


def _real_channel(orchestrator: str | None = "orch"):
    from kiro_crew.channel import Channel

    channel = Channel(id="ch1", topic="arc")
    delivered: list[tuple[str, str]] = []

    async def _deliver(_channel, agent, msg):
        delivered.append((agent.session_key, msg.content))
        return "delivered"

    channel._delivery_fn = _deliver
    ids = {}
    for slot in (A, B, "orch"):
        ids[slot] = channel.attach_session(f"dashboard:{slot}", slot).id
    channel.orchestrator_id = ids[orchestrator] if orchestrator else None
    return channel, delivered


@pytest.mark.asyncio
async def test_collision_reaches_both_claimants_through_a_real_channel(world):
    channel, delivered = _real_channel()
    world.manager.get = lambda cid: channel
    await _write(world, A, _claim("wire route"))
    delivery = json.loads((await _write(world, B, _claim("wire route"))).text)["delivery"]
    assert sorted(delivery["recipients"]) == [A, B]
    assert sorted(key for key, _ in delivered) == [f"dashboard:{A}", f"dashboard:{B}"]
    assert all(text.startswith("CLAIM from reviewer:") for _, text in delivered)


@pytest.mark.asyncio
async def test_unreachable_when_no_orchestrator_to_post_as(world):
    channel, delivered = _real_channel(orchestrator=None)
    world.manager.get = lambda cid: channel
    await _write(world, A, _claim("wire route"))
    delivery = json.loads((await _write(world, B, _claim("wire route"))).text)["delivery"]
    assert delivery["recipients"] == [A]
    assert delivery["unreachable"] == [B]
    assert [key for key, _ in delivered] == [f"dashboard:{A}"]


@pytest.mark.asyncio
async def test_delivery_store_reads_stay_off_the_event_loop(world, monkeypatch):
    import threading

    from kiro_crew import arcs

    loop_thread = threading.current_thread()
    seen: list[threading.Thread] = []
    real_read_arc, real_read_entries = arcs.read_arc, arcs.read_entries

    def read_arc(*args, **kwargs):
        seen.append(threading.current_thread())
        return real_read_arc(*args, **kwargs)

    def read_entries(*args, **kwargs):
        seen.append(threading.current_thread())
        return real_read_entries(*args, **kwargs)

    monkeypatch.setattr(arcs, "read_arc", read_arc)
    monkeypatch.setattr(arcs, "read_entries", read_entries)
    ask = arcs.append_entry(world.arc["id"], A, {"kind": "ASK", "text": "which?", "to": B})
    await _write(world, A, {"kind": "FAIL", "text": "boom"})
    await _write(world, B, {"kind": "FACT", "text": "this one", "reply_to": ask["id"]})
    assert seen
    assert loop_thread not in seen
