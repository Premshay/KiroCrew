"""Tests for GET /api/slash-commands descriptions.

Pins the contract that every dashboard slash command carries a non-empty,
human-readable description sourced from the single SLASH_COMMAND_DESCRIPTIONS
map (no blank descriptions in the autocomplete menu), and guards that the map
stays in sync with _SLASH_COMMANDS.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat_utils import (
    _BLOCKED_SLASH_COMMANDS,
    _SLASH_COMMANDS,
    SLASH_COMMAND_DESCRIPTIONS,
)


def _fake_config(provider: str):
    return SimpleNamespace(agent=SimpleNamespace(provider=provider))


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers.agents import api_slash_commands

    app = web.Application()
    app.router.add_get("/api/slash-commands", api_slash_commands)
    return app


async def _get(provider: str, *, state=None, claude_providers=()):
    """Fetch the menu, optionally against a fake live session.

    *claude_providers* names which of ``state``'s providers the Claude-backend
    check should accept, so a test can put a kiro session and a Claude session
    in the same map and pin which one the handler reads.
    """
    app = _make_app()
    if state is not None:
        app["state"] = state
    with (
        patch(
            "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
            return_value=_fake_config(provider),
        ),
        patch(
            "kiro_crew.providers.acp.is_claude_backend",
            side_effect=lambda p: p in claude_providers,
        ),
    ):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/slash-commands")
            assert resp.status == 200
            return await resp.json()


def _fake_provider(commands):
    return SimpleNamespace(slash_commands=list(commands))


def _fake_state(providers, by_key=None):
    by_key = by_key or {}
    return SimpleNamespace(
        sessions=SimpleNamespace(
            active_providers=lambda: list(providers),
            get_provider=lambda key: by_key[key],
        )
    )


def test_description_map_covers_slash_commands():
    """DRY guard: a command added to _SLASH_COMMANDS without a description
    (or vice-versa) fails the build here rather than shipping a blank menu row."""
    missing = set(_SLASH_COMMANDS) - set(SLASH_COMMAND_DESCRIPTIONS)
    assert not missing, f"commands missing a description: {sorted(missing)}"
    for name, desc in SLASH_COMMAND_DESCRIPTIONS.items():
        assert desc.strip(), f"empty description for {name}"


class TestApiSlashCommands:
    @pytest.mark.asyncio
    async def test_default_provider_returns_described_commands(self):
        payload = await _get("kiro")
        by_name = {item["name"]: item["description"] for item in payload}

        # Default path returns the _SLASH_COMMANDS set minus the blocked
        # commands: a blocked command only ever produces a rejection message,
        # so advertising it in the autocomplete would be an inert suggestion.
        assert set(by_name) == set(_SLASH_COMMANDS - _BLOCKED_SLASH_COMMANDS)
        # Every command has a non-empty description matching the shared map.
        for name, desc in by_name.items():
            assert desc, f"blank description for {name}"
            assert desc == SLASH_COMMAND_DESCRIPTIONS[name]
        # KiroCrew-local commands read meaningfully.
        assert "side" in by_name["/side"].lower()

    @pytest.mark.asyncio
    async def test_blocked_commands_absent_from_suggestions(self):
        """Regression guard: no blocked command may appear in the suggestion
        payload. /tangent regressed this way once — present in _SLASH_COMMANDS
        (so the menu offered it) but rejected at execution time."""
        payload = await _get("kiro")
        names = {item["name"] for item in payload}
        leaked = names & _BLOCKED_SLASH_COMMANDS
        assert not leaked, f"blocked commands advertised in menu: {sorted(leaked)}"
        assert "/tangent" not in names

    @pytest.mark.asyncio
    async def test_claude_code_provider_filters_blocked_commands(self):
        """The provider-reported path applies the same gate: a harness that
        reports a blocked command must not have it forwarded to the menu."""
        provider = _fake_provider(
            [
                {"name": "compact", "description": "Compact"},
                {"name": "tangent", "description": "Unavailable"},
                {"name": "quit", "description": "Unavailable"},
                {"name": "help", "description": "Help"},
            ]
        )
        payload = await _get("acp", state=_fake_state([provider]), claude_providers=(provider,))
        names = {item["name"] for item in payload}
        assert "/compact" in names and "/help" in names and "/side" in names
        assert "/tangent" not in names
        assert "/quit" not in names

class TestAdvertisedCommands:
    """The Claude backend forwards the CLI's own registry; the menu shows it.

    Which backend a session runs is per-agent routing, so these all use the
    ``acp`` provider — the global setting under which the routed Claude seam
    actually runs, and under which the menu previously showed only kiro's set.
    """

    @pytest.mark.asyncio
    async def test_live_claude_commands_replace_the_static_set(self):
        provider = _fake_provider(
            [
                {"name": "design", "description": "Grant or revoke Design access"},
                {"name": "code-review", "description": "Review the current diff"},
            ]
        )
        payload = await _get("acp", state=_fake_state([provider]), claude_providers=(provider,))
        by_name = {item["name"]: item["description"] for item in payload}
        assert by_name["/design"] == "Grant or revoke Design access"
        assert "/code-review" in by_name
        # chat_runner intercepts these, so no backend advertises them and
        # adopting the CLI registry must not drop them from the menu.
        for dashboard_only in ("/side", "/goal", "/prompts"):
            assert dashboard_only in by_name
            assert by_name[dashboard_only] == SLASH_COMMAND_DESCRIPTIONS[dashboard_only]
        # kiro-only commands are not runnable on this backend.
        assert "/experiment" not in by_name

    @pytest.mark.asyncio
    async def test_kiro_backend_keeps_the_curated_set(self):
        """kiro-cli advertises too, but its list would restore blocked commands."""
        provider = _fake_provider([{"name": "design", "description": "d"}])
        payload = await _get("acp", state=_fake_state([provider]), claude_providers=())
        assert {item["name"] for item in payload} == set(_SLASH_COMMANDS - _BLOCKED_SLASH_COMMANDS)

    @pytest.mark.asyncio
    async def test_slot_query_picks_that_slots_backend(self):
        kiro = _fake_provider([{"name": "wrong", "description": ""}])
        claude = _fake_provider([{"name": "design", "description": "d"}])
        state = _fake_state([kiro], by_key={"dashboard:slot-b": claude})
        app = _make_app()
        app["state"] = state
        with (
            patch(
                "kiro_crew.dashboard.handlers.agents.KiroCrewConfig.load",
                return_value=_fake_config("acp"),
            ),
            patch(
                "kiro_crew.providers.acp.is_claude_backend",
                side_effect=lambda p: p is claude,
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.get("/api/slash-commands?slot=slot-b")
                payload = await resp.json()
        assert "/design" in {item["name"] for item in payload}

    @pytest.mark.asyncio
    async def test_multiline_description_collapses_to_one_row(self):
        """A skill's body would otherwise ship kilobytes to render an ellipsis."""
        provider = _fake_provider(
            [{"name": "dataviz", "description": "First line.\n" + "x" * 5000}]
        )
        payload = await _get("acp", state=_fake_state([provider]), claude_providers=(provider,))
        desc = next(i["description"] for i in payload if i["name"] == "/dataviz")
        assert desc == "First line."

    @pytest.mark.asyncio
    async def test_overlong_single_line_is_capped(self):
        provider = _fake_provider([{"name": "claude-api", "description": "y" * 5000}])
        payload = await _get("acp", state=_fake_state([provider]), claude_providers=(provider,))
        desc = next(i["description"] for i in payload if i["name"] == "/claude-api")
        assert len(desc) == 200

    @pytest.mark.asyncio
    async def test_no_live_session_falls_back_to_the_static_set(self):
        payload = await _get("acp", state=_fake_state([]), claude_providers=())
        assert {item["name"] for item in payload} == set(_SLASH_COMMANDS - _BLOCKED_SLASH_COMMANDS)

    @pytest.mark.asyncio
    async def test_claude_code_provider_still_answers_before_any_handshake(self):
        """Cold start: nothing advertised yet, so the SDK baseline stands in."""
        payload = await _get("claude_code", state=_fake_state([]), claude_providers=())
        names = {item["name"] for item in payload}
        assert "/compact" in names and "/security-review" in names
        assert {"/side", "/goal", "/prompts"} <= names
