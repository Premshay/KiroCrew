"""Named Claude agents deny tools from omitted Kiro rows on session start."""

from __future__ import annotations

from pathlib import Path

import pytest
from acp_session_mcp_helpers import agents_dir_fixture  # noqa: F401
from acp_session_mcp_helpers import _write_spec

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_sdk.claude_worker_tools import CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL
from kiro_crew.providers.mirrors.registry import MIRRORS, mirror_for

_FORBIDDEN = {"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "PowerShell"}
_MAPPED_TOOLS = set().union(*CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL.values())


async def _session_new_options(client: AcpClient, tmp_path: Path, monkeypatch) -> dict:
    """Drive the real ``session/new`` call site and return the options it sent."""
    sent: list[dict] = []

    async def _work_dir():
        return str(tmp_path)

    async def _send(_method, params):
        sent.append(params)
        return 1

    async def _wait(_rid, **_kw):
        return {"sessionId": "s-1"}

    monkeypatch.setattr(client, "_session_work_dir", _work_dir)
    monkeypatch.setattr(client, "_send_request", _send)
    monkeypatch.setattr(client, "_wait_for_response", _wait)
    await client._new_session_following_substitution()
    [params] = sent
    return params["_meta"]["claudeCode"]["options"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools, kept",
    [
        (["read"], {"Read", "Grep", "Glob"}),
        (["fs_read", "@kirocrew-core"], {"Read", "Glob"}),
        (["@builtin/fs_read"], {"Read", "Glob"}),
        (
            ["fs_read", "execute_bash"],
            {"Read", "Glob", *CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL["execute_bash"]},
        ),
        (["read", "use_subagent"], {"Read", "Grep", "Glob", "Task", "Agent"}),
        (["fs_read", "claude_artifact", "claude_design_sync"], {"Read", "Glob"}),
        (["claude_design_sync"], set()),
        ([*CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL, "claude_artifact"], _MAPPED_TOOLS),
        (["use_aws", "@kirocrew-core"], set()),
    ],
)
async def test_a_named_agents_list_is_the_sessions_built_ins(
    tmp_path, agents_dir, monkeypatch, tools, kept
):
    _write_spec(agents_dir, servers={}, tools=tools, name="probe")
    client = AcpClient(work_dir=tmp_path, agent="probe", acp_backend=ACP_BACKEND_CLAUDE)
    options = await _session_new_options(client, tmp_path, monkeypatch)
    assert "tools" not in options
    expected_denied = _MAPPED_TOOLS - kept
    assert set(options.get("disallowedTools", ())) == expected_denied
    if "execute_bash" not in tools:
        assert _FORBIDDEN <= expected_denied


@pytest.mark.asyncio
async def test_an_agent_an_app_publishes_is_held_to_its_list(tmp_path, agents_dir, monkeypatch):
    _write_spec(agents_dir, servers={}, tools=["read"], name="someapp--reader")
    client = AcpClient(work_dir=tmp_path, agent="someapp--reader", acp_backend=ACP_BACKEND_CLAUDE)
    options = await _session_new_options(client, tmp_path, monkeypatch)
    assert "tools" not in options
    assert set(options["disallowedTools"]) == _MAPPED_TOOLS - {"Read", "Grep", "Glob"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent, tools",
    [
        ("probe", ["*"]),
        ("probe", ["read", "*"]),
        ("probe", ["@builtin"]),
        ("probe", ["@builtin/"]),
        ("probe", ["@builtin/*"]),
        ("probe", list(CLAUDE_WORKER_BUILTINS_BY_KIRO_TOOL)),
        ("probe", None),
        ("kirocrew", ["read"]),
        ("kirocrew-conductor", ["read"]),
    ],
)
async def test_a_list_that_limits_nothing_keeps_the_whole_preset(
    tmp_path, agents_dir, monkeypatch, agent, tools
):
    _write_spec(agents_dir, servers={}, tools=tools, name=agent)
    client = AcpClient(work_dir=tmp_path, agent=agent, acp_backend=ACP_BACKEND_CLAUDE)
    options = await _session_new_options(client, tmp_path, monkeypatch)
    assert "tools" not in options
    assert "disallowedTools" not in options


@pytest.mark.parametrize("backend", sorted(set(MIRRORS) - {ACP_BACKEND_CLAUDE}))
def test_no_other_backend_projects_built_ins(agents_dir, backend):
    _write_spec(
        agents_dir,
        servers={},
        tools=["read", "claude_artifact", "claude_design_sync"],
        name="probe",
    )
    mirror = mirror_for(backend)
    assert mirror is not None
    projection = mirror.session_projection("probe", permission_surface_owned=True)
    assert projection.denied_builtin_tools is None


def test_the_claude_mirror_projects_them_on_a_withheld_array_too(agents_dir):
    _write_spec(agents_dir, servers={}, tools=["read"], name="probe")
    mirror = mirror_for(ACP_BACKEND_CLAUDE)
    assert mirror is not None
    for owned in (True, False):
        projection = mirror.session_projection("probe", permission_surface_owned=owned)
        assert set(projection.denied_builtin_tools or ()) == _MAPPED_TOOLS - {
            "Read",
            "Grep",
            "Glob",
        }


@pytest.mark.parametrize("tools", [None, "read", [], [1]])
def test_a_value_that_is_not_a_list_of_tools_limits_nothing(tools):
    from kiro_crew.agent_sdk.claude_worker_tools import claude_agent_denied_builtin_tools

    assert claude_agent_denied_builtin_tools(tools) is None
