"""Coverage for creating channels that attach existing dashboard sessions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.channel import ChannelManager
from kiro_crew.dashboard.handlers_channel import api_channel_create


def _request(manager: ChannelManager, body: dict) -> MagicMock:
    request = MagicMock()
    request.app = {"state": SimpleNamespace(channel_manager=manager)}
    request.json = AsyncMock(return_value=body)
    return request


@pytest.mark.asyncio
async def test_session_only_channel_has_no_provider_owned_agents(tmp_path):
    manager = ChannelManager(channels_dir=tmp_path)

    response = await api_channel_create(
        _request(manager, {"topic": "Multiplex coordination", "agents": [], "session_only": True})
    )

    body = json.loads(response.body)
    assert response.status == 200
    assert body["channel"]["members"] == {}


@pytest.mark.asyncio
async def test_session_only_channel_rejects_provider_owned_agents(tmp_path):
    manager = ChannelManager(channels_dir=tmp_path)

    response = await api_channel_create(
        _request(
            manager,
            {
                "topic": "Multiplex coordination",
                "agents": [{"role": "Orchestrator", "is_orchestrator": True}],
                "session_only": True,
            },
        )
    )

    body = json.loads(response.body)
    assert response.status == 400
    assert body["code"] == "invalid_session_only"
    assert manager.list_channels() == []
