"""Tests for the subscription-backed per-crew model discovery endpoint."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewAgentConfig


def _config():
    return SimpleNamespace(
        agents={"codex": KiroCrewAgentConfig(kiro_agent="codex")},
        default_agent="codex",
    )


class _Sessions:
    def __init__(self, provider):
        self.provider = provider
        self.release = MagicMock()
        self.destroy = AsyncMock()

    async def get_or_create(self, key, agent=None):
        self.key = key
        self.agent = agent
        return self.provider, True, False

    def has_session(self, key):
        return key == getattr(self, "key", None)


def _app(policy, sessions):
    from kiro_crew.dashboard.handlers.agents import api_kirocrew_agent_models

    app = web.Application()
    app["state"] = SimpleNamespace(sessions=sessions)
    app["platform_context"] = SimpleNamespace(
        providers=SimpleNamespace(agent_runtime_policy=lambda _name: policy)
    )
    app.router.add_get("/api/agents/{name}/models", api_kirocrew_agent_models)
    return app


@pytest.mark.asyncio
async def test_live_model_discovery_returns_advertised_subscription_models():
    provider = SimpleNamespace(
        available_models=lambda: [
            {"modelId": "gpt-5.6-sol", "name": "GPT-5.6", "description": "Subscription"}
        ],
        get_valid_effort_levels=lambda: ["low", "high"],
    )
    sessions = _Sessions(provider)
    policy = {"model": "selectable", "effort": "selectable"}

    with patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", return_value=_config()):
        async with TestClient(TestServer(_app(policy, sessions))) as client:
            response = await client.get("/api/agents/codex/models")
            assert response.status == 200
            assert await response.json() == {
                "models": [
                    {
                        "modelId": "gpt-5.6-sol",
                        "name": "GPT-5.6",
                        "description": "Subscription",
                    }
                ],
                "effort_levels": ["low", "high"],
            }

    assert sessions.agent == "codex"
    sessions.release.assert_called_once_with("dashboard:model-discovery:codex")
    sessions.destroy.assert_awaited_once_with("dashboard:model-discovery:codex")


@pytest.mark.asyncio
async def test_managed_runtime_does_not_start_a_discovery_session():
    sessions = SimpleNamespace(get_or_create=AsyncMock())
    policy = {"model": "managed", "effort": "managed"}

    with patch("kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load", return_value=_config()):
        async with TestClient(TestServer(_app(policy, sessions))) as client:
            response = await client.get("/api/agents/codex/models")
            assert response.status == 200
            assert await response.json() == {"models": [], "effort_levels": []}

    sessions.get_or_create.assert_not_awaited()
