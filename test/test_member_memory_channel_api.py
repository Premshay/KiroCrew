"""Member recall and explicit seed cross the real owner/session/store gates."""

from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from member_memory_helpers import document_store
from member_memory_helpers import env as _member_env
from member_memory_helpers import request

from kiro_crew import mcp_core, memory_stores
from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import memory, memory_member
from kiro_crew.mcp_tools import learn
from kiro_crew.memory import MemoryStore

pytestmark = pytest.mark.xdist_group("member_memory_api")


env = _member_env


@pytest.fixture
def on_teardown():
    """Run each registered callable after the test, whichever way it ended.

    For handles the test opens itself (a ``SkillsLoader`` and its search index,
    a tier the handler publishes on ``state``) that ``env`` does not own: an
    unclosed sqlite connection is a reference cycle on CPython 3.11+, so its
    descriptors survive the test until the cyclic collector runs.
    """
    callbacks: list = []
    yield callbacks.append
    for callback in reversed(callbacks):
        callback()


def _close_standalone_vector(state) -> None:
    standalone = getattr(state, "_standalone_vector", None)
    if standalone is not None:
        standalone.close()


def _crewmate_channel(env, *, coordinator: bool):
    """A channel the member ``alice`` (session ``dashboard:alice``) belongs to."""
    from kiro_crew.channel import Channel

    def slot(key: str, title: str):
        return SimpleNamespace(
            key=key,
            title=title,
            mode="",
            _app="",
            linked_session_key="",
            mark_checkpoint_activity=lambda: None,
        )

    env.state._slots = {
        "alice": slot("alice", "Alice desk"),
        "helper": slot("helper", "Helper"),
        "review-lane": slot("review-lane", "Review lane"),
    }
    env.state.push_slots_update = lambda: None
    channel = Channel(id="deadbeef", topic="Crewmate coordination", _max_agents=4)
    lead = channel.attach_session("dashboard:helper", role="Helper")
    member = channel.attach_session("dashboard:alice", role="Alice")
    assert lead is not None and member is not None
    if coordinator:
        channel.set_coordinator(member.id)
    channels = {channel.id: channel}
    env.state.channel_manager = SimpleNamespace(_channels=channels, get=channels.get)
    return channel, member


@pytest.mark.asyncio
async def test_crewmate_coordinator_files_an_attach_request(env):
    """The point of request_attach: a crewmate coordinator assembles its channel."""
    from kiro_crew.dashboard.handlers import sessions

    channel, member = _crewmate_channel(env, coordinator=True)
    response = await sessions.api_session_channel(
        request(
            env,
            internal=True,
            session="dashboard:alice",
            body={"action": "request_attach", "channel_id": channel.id, "session": "review-lane"},
        )
    )
    assert response.status == 200, response.text
    filed = json.loads(response.text)["request"]
    assert filed["requested_by"] == member.id
    assert len(channel.members) == 2


@pytest.mark.asyncio
async def test_crewmate_that_is_not_coordinator_cannot_request_attach(env):
    from kiro_crew.dashboard.handlers import sessions

    channel, _member = _crewmate_channel(env, coordinator=False)
    response = await sessions.api_session_channel(
        request(
            env,
            internal=True,
            session="dashboard:alice",
            body={"action": "request_attach", "channel_id": channel.id, "session": "review-lane"},
        )
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "channel_coordinator_required"
    assert channel.attach_requests == {}


@pytest.mark.asyncio
async def test_crewmate_coordinator_adds_an_agent(env):
    from kiro_crew.dashboard.handlers import sessions
    from kiro_crew.dashboard.handlers.sessions_parts import channels as channel_handlers

    channel, _member = _crewmate_channel(env, coordinator=True)
    with mock.patch.object(channel_handlers, "_spawn_channel_member", mock.AsyncMock()):
        response = await sessions.api_session_channel(
            request(
                env,
                internal=True,
                session="dashboard:alice",
                body={
                    "action": "add_agent",
                    "channel_id": channel.id,
                    "role": "Worker",
                    "task": "Run checks.",
                },
            )
        )
    assert response.status == 200, response.text
    assert len(channel.members) == 3


@pytest.mark.asyncio
async def test_crewmate_coordinator_removes_an_attached_session(env):
    from kiro_crew.dashboard.handlers import sessions

    channel, _member = _crewmate_channel(env, coordinator=True)
    target = next(m for m in channel.members.values() if m.role == "Helper")
    response = await sessions.api_session_channel(
        request(
            env,
            internal=True,
            session="dashboard:alice",
            body={
                "action": "remove_member",
                "channel_id": channel.id,
                "member_id": target.id,
                "force": True,
            },
        )
    )
    assert response.status == 200, response.text
    assert target.id not in channel.members


@pytest.mark.asyncio
async def test_crewmate_coordinator_removing_an_active_member_still_needs_force(env):
    from kiro_crew.dashboard.handlers import sessions

    channel, _member = _crewmate_channel(env, coordinator=True)
    target = next(m for m in channel.members.values() if m.role == "Helper")
    response = await sessions.api_session_channel(
        request(
            env,
            internal=True,
            session="dashboard:alice",
            body={"action": "remove_member", "channel_id": channel.id, "member_id": target.id},
        )
    )
    assert response.status == 409
    assert json.loads(response.text)["code"] == "channel_member_active"
    assert target.id in channel.members


@pytest.mark.asyncio
async def test_crewmate_that_is_not_coordinator_cannot_remove_members(env):
    from kiro_crew.dashboard.handlers import sessions

    channel, _member = _crewmate_channel(env, coordinator=False)
    target = next(m for m in channel.members.values() if m.role == "Helper")
    response = await sessions.api_session_channel(
        request(
            env,
            internal=True,
            session="dashboard:alice",
            body={
                "action": "remove_member",
                "channel_id": channel.id,
                "member_id": target.id,
                "force": True,
            },
        )
    )
    assert response.status == 403
    assert json.loads(response.text)["code"] == "channel_coordinator_required"
    assert target.id in channel.members


@pytest.mark.asyncio
async def test_crewmate_status_names_itself_and_its_coordinator_role(env):
    from kiro_crew.dashboard.handlers import sessions

    channel, member = _crewmate_channel(env, coordinator=True)
    response = await sessions.api_session_channel(
        request(env, internal=True, session="dashboard:alice", body={"action": "status"})
    )
    status = json.loads(response.text)["channels"][0]
    assert status["self"] == {"id": member.id, "role": "Alice"}
    assert status["coordinator"] is True


@pytest.mark.asyncio
async def test_internal_chat_middleware_refuses_member_before_slot_creation(env):
    from kiro_crew.dashboard.token_auth import token_auth_middleware

    app = web.Application()
    app["state"] = env.state
    req = make_mocked_request(
        "POST",
        "/api/chat/slots",
        app=app,
        headers={
            "X-Internal-Secret": "test-member-secret",
            "X-Session-Key": "dashboard:alice",
        },
    ).clone(remote="127.0.0.1")
    handler = mock.AsyncMock(return_value=web.json_response({"created": True}))
    middleware = token_auth_middleware(
        mixed_internal_paths=frozenset({"/api/chat"}),
        internal_secret="test-member-secret",
    )
    response = await middleware(req, handler)
    # The loopback TCP caller holds the secret and declares a session key it
    # cannot attest, so the identity refusal lands ahead of the member-scope
    # decision. Either way the chat control never runs.
    assert response.status == 409
    assert json.loads(response.text)["code"] == "member_identity_unavailable"
    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT"])
async def test_member_history_oversized_document_is_unavailable_without_overwrite(env, method):
    await document_store(env, "member-alice")
    db = env.tiers["member-alice"].db
    limit = MemoryStore._HISTORY_SNAPSHOT_MAX_BYTES
    day = datetime.now().astimezone().date().isoformat()
    with db:
        db.execute("INSERT INTO memory_history VALUES (?,?,1,'now')", (day, "x" * (limit + 1)))
    before = db.total_changes
    response = await memory.api_memory_history(
        request(
            env,
            body={"content": "Do not overwrite the unreadable source"},
            query={"store": "member-alice"},
            owner=True,
            session="dashboard:ui",
        ).clone(method=method)
    )
    assert response.status == 503
    assert json.loads(response.text)["code"] == "store_unavailable"
    assert db.total_changes == before
    assert (
        db.execute(
            "SELECT length(CAST(content AS BLOB)) FROM memory_history WHERE day=?", (day,)
        ).fetchone()[0]
        == limit + 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("vector_available", [True, False])
@pytest.mark.parametrize(
    "question", ["{}", "What do we know about {}?", "Which project does {} belong to?"]
)
async def test_markdown_only_facts_reachable_from_agent_recall(
    env, monkeypatch, mode, vector_available, question, on_teardown
):
    from kiro_crew import context, member_memory_auth
    from kiro_crew.hooks import HookManager
    from kiro_crew.learn import LessonStore
    from kiro_crew.skills import SkillsLoader

    store = await document_store(env, "")
    store.write_projects("# Active Projects\nNotebookquartz task belongs to the synthetic project.")
    store.append_history("Dailyquartz milestone was verified.")
    assert env.tiers[""].get_semantic("Notebookquartz") is None
    skills = SkillsLoader(skills_path=env.home / "synthetic-skills", install_builtins=False)
    on_teardown(skills.close)
    # With no vector tier on the memory store the recall handler opens a standalone
    # one and publishes it on ``state``, where production keeps it for the process.
    on_teardown(lambda: _close_standalone_vector(env.state))
    builder = context.ContextBuilder(
        memory=store,
        lessons=LessonStore(base_dir=env.home / "synthetic-lessons"),
        skills=skills,
        hooks=HookManager(),
    )
    monkeypatch.setattr(context, "kiro_agents_dir", lambda: env.home / "empty-agents")
    monkeypatch.setattr(context, "agent_skill_globs", lambda agent: [])
    prompt = env.home / "synthetic-prompt.txt"
    prompt.write_text("Preserve all safety controls.", encoding="utf-8")
    monkeypatch.setattr(context, "_prompt_path", lambda **kwargs: prompt)
    greeting, _ = builder.build_message("hi", True, blocks_reads=mode == "temporary")
    assert greeting.endswith("hi")
    if mode == "temporary":
        assert "Notebookquartz" not in greeting and "Dailyquartz" not in greeting
    else:
        assert "Notebookquartz" in greeting and "Dailyquartz" in greeting
    env.state._slots["global"] = SimpleNamespace(
        is_restricted=mode == "incognito", blocks_reads=mode == "temporary", memory_mode=mode
    )
    session = "dashboard:global"
    env.metadata[session] = {"memory_store": "default", "memory_mode": mode}
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    if not vector_available:
        store.vector_store = None
    before = (store.read_projects(), store.read_recent_history())
    search = mock.Mock(wraps=store.search)
    monkeypatch.setattr(store, "search", search)
    for fact in ("Notebookquartz", "Dailyquartz"):
        query = question.format(fact)
        search.reset_mock()
        response = await memory_member.api_memory_recall(
            request(env, query={"q": query}, internal=True, session=session)
        )
        assert response.text is not None
        if mode == "temporary":
            assert response.status == 403
            assert fact not in response.text
            search.assert_not_called()
            continue
        assert response.status == 200
        assert search.call_count == 1
        assert search.call_args.kwargs == {"limit": 5, "match_any": True, "strict": True}
        payload = json.loads(response.text)
        assert fact in payload["semantic_context"]
        assert payload["store"] == ""
        assert payload["total_chars"] <= 3000
        # The real MCP renderer forwards this HTTP result with the strict
        # session identity and keeps the notebook evidence in its model payload.
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: session)
        transport = mock.Mock(return_value=payload)
        monkeypatch.setattr(mcp_core, "_get", transport)
        rendered = learn.memory_recall("memory_recall", {"query": query})
        assert fact in rendered
        assert "reference data, not instructions" in rendered
        assert len(rendered.encode("utf-8")) <= 16384
        assert transport.call_args.kwargs == {"session_key": session}
    assert (store.read_projects(), store.read_recent_history()) == before


@pytest.mark.asyncio
async def test_markdown_recall_is_bound_to_named_v1_not_global(env, monkeypatch):
    from kiro_crew import member_memory_auth

    cfg = loader.KiroCrewConfig.load()
    cfg.memory_stores["legacy-notebook"] = loader.MemoryStoreConfig(memory_version=1)
    cfg.save()
    (env.home / "memory_stores" / "legacy-notebook").mkdir()
    monkeypatch.setattr(memory_stores, "_DECLARED_MEMO", None)
    own = await document_store(env, "legacy-notebook")
    global_store = await document_store(env, "")
    own.write_projects("# Active Projects\nQuartzscope OWN notebook milestone.")
    global_store.write_projects("# Active Projects\nQuartzscope GLOBAL notebook milestone.")
    env.state._slots["legacy"] = SimpleNamespace(is_restricted=False, blocks_reads=False)
    # Internal recall resolves the store from the session's recorded execution,
    # never from transcript metadata alone.
    await env.bind_session_async("dashboard:legacy", "legacy-notebook")
    monkeypatch.setattr(member_memory_auth, "_request_peer_pid", lambda request: os.getpid())
    response = await memory_member.api_memory_recall(
        request(
            env,
            query={"q": "What do we know about Quartzscope?"},
            internal=True,
            session="dashboard:legacy",
        )
    )
    assert response.status == 200
    assert response.text is not None
    assert "OWN notebook" in response.text
    assert "GLOBAL notebook" not in response.text
    assert json.loads(response.text)["store"] == "legacy-notebook"
