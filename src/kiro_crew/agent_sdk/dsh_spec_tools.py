"""Map a named agent's Kiro tool list to the DeepSeek Harness built-ins it denies.

The dsh counterpart of :mod:`kiro_crew.agent_sdk.claude_worker_tools`'s named-agent
rule (#112): a kiro-cli row the spec's ``tools`` leaves out denies the dsh built-ins
that do its job. Only a row this table maps counts as an omission; every dsh tool
the table does not name stays, as every Claude tool outside its table does.

Two rows Claude maps are deliberately NOT mapped here (operator rulings 2026-11-26):

* ``web_fetch`` / ``web_search``: the DeepSeek seats keep web search, and their specs
  do not list either row.
* ``use_subagent``: stock dsh Agent Teams are on for every DeepSeek bot, and no spec
  lists ``use_subagent``; mapping it would switch teams off everywhere.

So dsh never denies a tool Claude keeps from the same spec; it keeps those two
families where Claude would deny them. Codex carries no per-tool limit at all.

Importing it loads no ACP module.
"""

from __future__ import annotations

from collections.abc import Mapping

from kiro_crew.agent_sdk.claude_worker_tools import excluded_builtin_rows
from kiro_crew.platform.tool_names import _ALL_BUILTINS_REFS

__all__ = ["DSH_BUILTINS_BY_KIRO_TOOL", "dsh_agent_denied_builtin_tools", "dsh_session_denied_builtin_tools"]

#: kiro-cli row -> every dsh 0.2.2 built-in that does its job.
DSH_BUILTINS_BY_KIRO_TOOL: Mapping[str, tuple[str, ...]] = {
    "fs_read": ("read", "read_image"),
    "grep": ("grep",),
    "glob": ("glob",),
    "fs_write": ("write", "edit"),
    "execute_bash": ("bash", "job_kill", "job_list", "job_output"),
}


def dsh_agent_denied_builtin_tools(tools: object) -> tuple[str, ...]:
    """The dsh built-ins a named spec's ``tools`` list leaves out, sorted; ``()`` for none.

    A missing, empty or non-list ``tools``, a wildcard, or a whole ``@builtin`` ref
    denies nothing (``tools: []`` is the zero-tools path's refusal, not this one's).
    """
    entries = [t.strip() for t in tools if isinstance(t, str)] if isinstance(tools, list) else []
    if not entries or any(t == "*" or t in _ALL_BUILTINS_REFS for t in entries):
        return ()
    granted = {row for entry in entries for row in excluded_builtin_rows(entry)}
    # Claude's grant extra (``_AGENT_GRANT_EXTRAS``): kiro-cli's ``fs_read`` also lists a
    # directory, so a granted ``fs_read`` keeps the glob tool as well.
    if "fs_read" in granted:
        granted.add("glob")
    return tuple(
        sorted(
            tool
            for row, row_tools in DSH_BUILTINS_BY_KIRO_TOOL.items()
            if row not in granted
            for tool in row_tools
        )
    )


def dsh_session_denied_builtin_tools(agent: str | None, spec: object, session_key: str) -> tuple[str, ...]:
    """The denied set for one DeepSeek session, with Claude's carve-outs.

    *spec* is the spec the session's projection read. A workspace alias without a
    spec file of its own has already been resolved to its base seat's ``kiro_agent``
    before the session starts (``session_agent_selection``), so its spec is the base
    seat's. No spec read (``None``) denies nothing: it is never read as "no tools".
    Crewmate workers and crew-written agents deny nothing, as on Claude
    (``providers.mirrors.claude_code._session_denied_builtin_tools``).
    """
    from kiro_crew.agent_sdk.spec_builtin_tools import _crew_written_agents
    from kiro_crew.mate_worker import MATE_WORKER_AGENT
    from kiro_crew.members import is_crewmate_worker_session

    if not agent or not isinstance(spec, dict):
        return ()
    if agent == MATE_WORKER_AGENT or agent in _crew_written_agents():
        return ()
    if is_crewmate_worker_session(session_key):
        return ()
    return dsh_agent_denied_builtin_tools(spec.get("tools"))
