"""Backend-aware slash command menus for the agents facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        _BLOCKED_SLASH_COMMANDS,
        _DASHBOARD_COMMANDS,
        _MAX_COMMAND_DESCRIPTION_CHARS,
        _SLASH_COMMANDS,
        SLASH_COMMAND_DESCRIPTIONS,
        DashboardState,
        KiroCrewConfig,
        _history_key_for,
        capabilities_for,
        capabilities_of,
        is_claude_code,
    )


def _menu_description(raw: str) -> str:
    """Reduce a backend-supplied description to what one menu row can show."""
    lines = raw.strip().splitlines()
    return lines[0][:_MAX_COMMAND_DESCRIPTION_CHARS] if lines else ""


def _advertised_commands(
    state: DashboardState | None, slot: str | None, blocked_commands: frozenset[str]
) -> list[dict[str, str]]:
    """Slash commands the live Claude ACP session advertised, "/"-prefixed.

    claude-agent-acp forwards the Claude Code CLI's own registry over
    ``available_commands_update`` — built-ins, skills and MCP prompts — and
    the CLI expands any of them from prompt text, so what it advertises is
    exactly what this session can run.

    The Claude backend only. kiro-cli advertises its own registry, but the
    dashboard's static ``_SLASH_COMMANDS`` deliberately differs from it: the
    commands ``_BLOCKED_SLASH_COMMANDS`` refuses are absent by design, and
    adopting the backend list wholesale would offer them again.

    Which backend a session runs is a per-agent routing decision, not a global
    one — ``agent.provider`` stays ``acp`` while individual agents are bound to
    the Claude seam -- so eligibility is read off the live provider.
    """
    if state is None:
        return []
    providers = []
    if slot:
        try:
            provider = state.sessions.get_provider(_history_key_for(slot))
        except (KeyError, AttributeError):
            provider = None
        if provider is not None:
            providers = [provider]
    if not providers:
        providers = state.sessions.active_providers()

    for provider in providers:
        if not is_claude_code(capabilities_of(provider).provider_seam):
            continue
        commands = getattr(provider, "slash_commands", [])
        if not commands:
            continue
        return [
            {"name": f"/{c['name']}", "description": _menu_description(c["description"])}
            for c in commands
            if f"/{c['name']}" not in blocked_commands
        ]
    return []


async def api_slash_commands(request: web.Request) -> web.Response:
    """GET /api/slash-commands — list available slash commands (provider-aware).

    An optional ``?slot=`` names the chat slot whose backend to ask, matching
    :func:`api_effort_levels`: concurrent slots can sit on different backends,
    and the commands one advertises are not the commands another accepts.
    """
    cfg = KiroCrewConfig.load()
    # This global endpoint has no session/member key; the configured default
    # backend is its capability authority, independent of the provider label.
    blocked_commands = _BLOCKED_SLASH_COMMANDS
    if capabilities_for(cfg.agent.acp_backend).supports_native_todos:
        blocked_commands = blocked_commands - {"/todos"}
    # `.get`, not `[...]`: nothing here needs dashboard state to answer, and a
    # menu that 500s where it could serve the static list is the worse failure.
    state: DashboardState | None = request.app.get("state")

    result = _advertised_commands(state, request.query.get("slot"), blocked_commands)
    if not result and is_claude_code(cfg.agent.provider):
        # Cold start: no session has handshaked yet, so nothing has been
        # advertised. These are the SDK-supported commands every Claude Code
        # session carries.
        result = [
            {"name": f"/{c}", "description": SLASH_COMMAND_DESCRIPTIONS.get(f"/{c}", "")}
            for c in (
                "compact",
                "clear",
                "context",
                "help",
                "init",
                "review",
                "security-review",
                "usage",
            )
            if f"/{c}" not in blocked_commands
        ]

    if result:
        advertised = {c["name"] for c in result}
        result.extend(
            {"name": name, "description": SLASH_COMMAND_DESCRIPTIONS.get(name, "")}
            for name in _DASHBOARD_COMMANDS
            if name not in advertised
        )
        return web.json_response(result)

    # Blocked commands stay in _SLASH_COMMANDS (typing one still gets the
    # explicit "not available in the dashboard" rejection in chat_runner), but
    # the suggestion payload must not advertise them: a menu entry that only
    # ever produces a warning is an inert affordance.
    return web.json_response(
        [
            {"name": c, "description": SLASH_COMMAND_DESCRIPTIONS.get(c, "")}
            for c in sorted(_SLASH_COMMANDS - blocked_commands)
        ]
    )
