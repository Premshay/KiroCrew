"""An attached channel member must not hold its dashboard slot's session.

``run_channel_agent`` acquires the key's per-session semaphore through
``get_or_create`` and holds it for the life of the channel. For a provider-backed
member that key is the worker's own; for an attached member it is a dashboard
slot, and holding it wedges every later turn on that slot inside ``get_or_create``
with no log line and no live turn for Stop to cancel.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from kiro_crew.channel import Channel, ChannelAgent, run_channel_agent
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.session import SessionManager


def _attached_channel() -> tuple[Channel, ChannelAgent]:
    agent = ChannelAgent(
        id="a1",
        role="chat-1925",
        agent_name="crew-claude",
        task="",
        session_key="dashboard:chat-1925",
        state="listening",
        attached_session=True,
    )
    channel = Channel(id="c1", topic="arc", orchestrator_id="a1", members={"a1": agent})
    return channel, agent


def _factory(session_key=None, agent=None, channel_id=None, **kwargs):
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.is_alive.return_value = True
    provider.is_process_alive.return_value = True
    return provider


@pytest.mark.asyncio
async def test_attached_member_never_claims_a_session():
    channel, agent = _attached_channel()
    sessions = AsyncMock()
    await run_channel_agent(agent, channel, sessions)
    sessions.get_or_create.assert_not_awaited()
    assert agent.state == "listening"


@pytest.mark.asyncio
async def test_dashboard_turn_is_not_blocked_by_the_channel_worker():
    """The slot's own turn must still be able to acquire its session."""
    cfg = KiroCrewConfig()
    cfg.agent.provider = "acp"
    cfg.session.timeout_secs = 2
    sessions = SessionManager(cfg, provider_factory=_factory)
    channel, agent = _attached_channel()

    worker = asyncio.create_task(run_channel_agent(agent, channel, sessions))
    await asyncio.sleep(0.05)
    try:
        # Stands in for the slot's next dashboard turn. Before the guard, the
        # worker held this key's semaphore and this wait never returned.
        await asyncio.wait_for(sessions.get_or_create("dashboard:chat-1925"), timeout=2)
    finally:
        sessions.release("dashboard:chat-1925")
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        await sessions.close_all()


@pytest.mark.asyncio
async def test_provider_backed_member_still_runs():
    """The guard is scoped to attached members; ordinary workers are untouched."""
    agent = ChannelAgent(
        id="a2",
        role="Researcher",
        agent_name="crew-claude",
        task="dig",
        session_key="channel:c1:a2",
    )
    channel = Channel(id="c1", topic="arc", members={"a2": agent})
    sessions = AsyncMock()
    sessions.get_or_create.return_value = (AsyncMock(), True, False)

    worker = asyncio.create_task(run_channel_agent(agent, channel, sessions))
    await asyncio.sleep(0.05)
    sessions.get_or_create.assert_awaited_once()
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
