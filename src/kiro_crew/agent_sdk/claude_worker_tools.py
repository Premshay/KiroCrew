"""Map Kiro tool limits to Claude Code's native deny rules.

A worker's excludes and a named agent's omitted rows deny the mapped Claude
tools. Claude Code supplies the rest of its native preset.
:func:`excluded_builtin_rows` is also what decides, before spawn, whether such an
entry is carried at all (``mate_worker.uncarried_tool_limits_of``).

Importing it loads no ACP module; the one ACP-layer read (KAS's tool tags)
happens when :func:`excluded_builtin_rows` is called.
"""

from __future__ import annotations

from collections.abc import Iterable

from kiro_crew.platform.tool_names import _ALL_BUILTINS_REFS, canonical_kiro_cli_tool_name

__all__ = [
    "CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL",
    "claude_agent_denied_builtin_tools",
    "claude_mcp_server_rule",
    "claude_worker_denied_builtin_tools",
    "excluded_builtin_rows",
]


def claude_mcp_server_rule(server: str) -> str:
    """The claude permission rule naming every tool of MCP *server* (``mcp__<server>``)."""
    from kiro_crew.acp.claude_mcp_names import claude_mcp_server_rule as _rule

    return _rule(server)


#: kiro-cli built-in -> every claude built-in that does its job. Kept apart from
#: ``acp.harness_tool_names.CLAUDE_TOOL_IDS_BY_KIRO_TOOL`` (hook naming) because this
#: one is pinned by the capture below, and its shell row lists every built-in that
#: runs or stops a command. Captured on Claude Code
#: 2.1.319: ``test/fixtures/acp_frames/claude/worker-native-tool-limits-live.jsonl``.
CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL: dict[str, tuple[str, ...]] = {
    "fs_read": ("Read",),
    "grep": ("Grep",),
    "glob": ("Glob",),
    "web_fetch": ("WebFetch",),
    "web_search": ("WebSearch",),
    "use_subagent": ("Task", "Agent"),
    "fs_write": ("Write", "Edit", "MultiEdit", "NotebookEdit"),
    "execute_bash": ("Bash", "Monitor", "PowerShell", "TaskStop"),
}


def excluded_builtin_rows(ref: str) -> tuple[str, ...]:
    """The :data:`CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL` rows an ``excludedTools`` *ref* takes out,
    or a ``tools`` *ref* grants.

    A tag KAS reads (``read``, ``write``, ``shell``, ``web``, ``subagent``) takes
    out every tool it selects; any other entry is read under Crew's name for it
    (``@builtin/shell`` and ``executeCmd`` are ``execute_bash``). Empty for an
    entry no row covers (``use_aws``, ``@builtin``, a glob), which no ``tools``
    list can carry.
    """
    from kiro_crew.acp.kas_permissions import SPEC_HOOK_TOOL_TAGS

    ref = ref.strip()
    names = SPEC_HOOK_TOOL_TAGS.get(ref) or (canonical_kiro_cli_tool_name(ref),)
    return tuple(name for name in names if name in CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL)


def claude_worker_denied_builtin_tools(excluded: Iterable[str]) -> tuple[str, ...] | None:
    """Deny the native tools of a worker's excluded Kiro rows."""
    out = {row for ref in excluded for row in excluded_builtin_rows(ref)}
    if not out:
        return None
    denied = (
        tool
        for row, tools in CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL.items()
        if row in out
        for tool in tools
    )
    return tuple(dict.fromkeys(denied))


#: Built-ins a named agent's granted row keeps beyond its worker row: kiro-cli's
#: ``fs_read`` also lists a directory, which claude's ``Read`` cannot.
_AGENT_GRANT_EXTRAS: dict[str, tuple[str, ...]] = {"fs_read": ("Glob",)}


def claude_agent_denied_builtin_tools(tools: object) -> tuple[str, ...] | None:
    """Deny mapped tools omitted by a named spec. The zero-tool path handles ``[]``."""
    entries = [t.strip() for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
    if not entries or any(t == "*" or t in _ALL_BUILTINS_REFS for t in entries):
        return None
    granted = {row for entry in entries for row in excluded_builtin_rows(entry)}
    kept = {
        tool
        for row, row_tools in CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL.items()
        if row in granted
        for tool in (*row_tools, *_AGENT_GRANT_EXTRAS.get(row, ()))
    }
    denied = (
        tool
        for row_tools in CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL.values()
        for tool in row_tools
        if tool not in kept
    )
    return tuple(dict.fromkeys(denied)) or None
