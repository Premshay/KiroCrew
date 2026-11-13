"""Backend-aware slash command menus for the agents facade."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        _BLOCKED_SLASH_COMMANDS,
        _MAX_COMMAND_DESCRIPTION_CHARS,
        _SLASH_COMMANDS,
        ACP_BACKEND_PROMPT_HANDLER_COMMANDS,
        ACP_BACKENDS_LOCAL_HARNESS_COMMANDS,
        SLASH_COMMAND_DESCRIPTIONS,
        KiroCrewConfig,
        capabilities_for,
        is_claude_code,
        local_harness_menu,
        menu_backend,
        menu_with_session_commands,
    )


def _menu_description(raw: str) -> str:
    lines = raw.strip().splitlines()
    return lines[0][:_MAX_COMMAND_DESCRIPTION_CHARS] if lines else ""


async def _menu_rows(request: web.Request, rows: list, blocked: frozenset) -> list:
    result = await menu_with_session_commands(request, rows, blocked)
    return [{**row, "description": _menu_description(row["description"])} for row in result]


async def api_slash_commands(request: web.Request) -> web.Response:
    """GET /api/slash-commands — list available slash commands (provider-aware)."""
    cfg = KiroCrewConfig.load()
    # The ``?slot=`` chat's backend decides the base list, the one the slash gate
    # resolves for that chat, so a chat pinned off the configured default lists
    # what runs there. With no slot it is the configured default. ``?slot=`` then
    # adds what that chat's live session advertised.
    backend = await menu_backend(request, cfg.agent.acp_backend)
    blocked_commands = _BLOCKED_SLASH_COMMANDS
    if capabilities_for(backend).supports_native_todos:
        blocked_commands = blocked_commands - {"/todos"}
    # A harness that runs its own commands gets its own list, not kiro-cli's.
    if capabilities_for(backend).runs_own_slash_commands or is_claude_code(cfg.agent.provider):
        cc_commands = [
            "compact",
            "clear",
            "context",
            "help",
            "init",
            "review",
            "security-review",
            "todos",
            "usage",
        ]
        result = [
            {"name": f"/{c}", "description": SLASH_COMMAND_DESCRIPTIONS.get(f"/{c}", "")}
            for c in cc_commands
            if f"/{c}" not in blocked_commands
        ]
        # The commands the dashboard answers before any harness sees the turn.
        for command in ("/agent", "/goal", "/prompts", "/side", "/workflow"):
            if not any(item["name"] == command for item in result):
                result.append(
                    {"name": command, "description": SLASH_COMMAND_DESCRIPTIONS.get(command, "")}
                )
        return web.json_response(await _menu_rows(request, result, blocked_commands))

    # Blocked commands stay in _SLASH_COMMANDS (typing one still gets the
    # explicit "not available in the dashboard" rejection in chat_runner), but
    # the suggestion payload must not advertise them: a menu entry that only
    # ever produces a warning is an inert affordance.
    offered = _SLASH_COMMANDS - blocked_commands
    # The same holds for a harness with no command channel: the slash gate answers
    # its commands with a notice, so list only what the dashboard serves there
    # (local_harness_menu, which /help prints too).
    if backend in ACP_BACKENDS_LOCAL_HARNESS_COMMANDS:
        offered = offered & local_harness_menu(backend)
    # A harness whose prompt handler runs only some harness commands gets the
    # notice for the rest too, so list what the dashboard serves and that row.
    runs_from_prompt = ACP_BACKEND_PROMPT_HANDLER_COMMANDS.get(backend)
    if runs_from_prompt is not None:
        offered = offered & (local_harness_menu(backend) | runs_from_prompt)
    rows = [
        {"name": c, "description": SLASH_COMMAND_DESCRIPTIONS.get(c, "")} for c in sorted(offered)
    ]
    return web.json_response(await _menu_rows(request, rows, blocked_commands))
