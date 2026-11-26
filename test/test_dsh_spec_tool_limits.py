"""A named agent's spec ``tools`` list is enforced on DeepSeek Harness built-ins.

Parity with the Claude seats (#112): a kiro-cli row the spec leaves out denies the
dsh built-ins that do its job; unmapped dsh tools (web, teams, subagents, MCP) stay.
The set travels to the dsh child in ``KIROCREW_DSH_DENIED_TOOLS``, the gate plugin
denies each name and reports the set it read, and the read-back refuses a session
whose plugin reports anything else.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from kiro_crew import acp_tool_gate as gate
from kiro_crew.acp_backends import ACP_BACKEND_DEEPSEEK
from kiro_crew.agent_sdk.dsh_spec_tools import (
    DSH_BUILTINS_BY_KIRO_TOOL,
    dsh_agent_denied_builtin_tools,
    dsh_session_denied_builtin_tools,
)

#: The tools list every DeepSeek seat spec carries today.
CURRENT_DEEPSEEK_SPEC = ["execute_bash", "fs_read", "fs_write", "grep", "glob", "@kirocrew-core"]


# ── the mapping ────────────────────────────────────────────────────────────────


def test_the_current_deepseek_spec_denies_nothing() -> None:
    """Web search, web fetch, writes and teams all survive on today's seats."""
    assert dsh_agent_denied_builtin_tools(CURRENT_DEEPSEEK_SPEC) == ()


def test_web_team_and_subagent_tools_are_never_mapped() -> None:
    mapped = {tool for tools in DSH_BUILTINS_BY_KIRO_TOOL.values() for tool in tools}
    for name in ("web_search", "web_fetch", "subagent", "subagent_fork", "send_message",
                 "wait_agent", "list_agents", "team_task_create", "skill", "todo_write"):
        assert name not in mapped, name
    # A spec with neither web row nor use_subagent still keeps all of them.
    denied = dsh_agent_denied_builtin_tools(["fs_read"])
    assert not set(denied) & {"web_search", "web_fetch", "subagent", "subagent_fork"}


def test_a_spec_without_fs_write_denies_write_and_edit() -> None:
    spec = ["execute_bash", "fs_read", "grep", "glob", "@kirocrew-core"]
    assert dsh_agent_denied_builtin_tools(spec) == ("edit", "write")


def test_a_read_only_spec_denies_shell_and_writes_but_keeps_glob() -> None:
    """kiro-cli's fs_read also lists a directory, so it keeps glob (Claude's grant extra)."""
    assert dsh_agent_denied_builtin_tools(["fs_read"]) == (
        "bash", "edit", "grep", "job_kill", "job_list", "job_output", "write",
    )


@pytest.mark.parametrize("tools", [None, [], ["*"], ["@builtin"], "fs_read", {"fs_read": 1}])
def test_unrestricted_or_malformed_lists_deny_nothing(tools: object) -> None:
    assert dsh_agent_denied_builtin_tools(tools) == ()


def test_kas_tags_map_like_their_rows() -> None:
    assert dsh_agent_denied_builtin_tools(["read", "write"]) == (
        "bash", "job_kill", "job_list", "job_output",
    )


def test_no_spec_read_denies_nothing_and_is_not_no_tools() -> None:
    """A spec that could not be read (an alias with no file of its own) never denies all."""
    assert dsh_session_denied_builtin_tools("crew-deepseek-atlas", None, "dashboard:chat-1") == ()


def test_crewmate_workers_and_crew_written_agents_are_carved_out(monkeypatch) -> None:
    from kiro_crew.agent_sdk import spec_builtin_tools
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    narrow = {"tools": ["fs_read"]}
    assert dsh_session_denied_builtin_tools(MATE_WORKER_AGENT, narrow, "k") == ()
    monkeypatch.setattr(spec_builtin_tools, "_crew_written_agents", lambda: frozenset({"crew-x"}))
    assert dsh_session_denied_builtin_tools("crew-x", narrow, "k") == ()
    assert dsh_session_denied_builtin_tools("crew-y", narrow, "k") != ()


# ── the projection: an alias takes its base seat's spec ───────────────────────


def _projection(monkeypatch, specs: dict[str, dict], agent: str):
    from kiro_crew.providers.mirrors import deepseek as mirror

    def fake_session_mcp_projection(name, **_kw):
        return SimpleNamespace(
            disabled_tools=(), disabled_servers=frozenset(), zero_tools=False,
            derived_spec_snapshot=None, agent_spec=specs.get(name),
        )

    monkeypatch.setattr(mirror, "session_mcp_projection", fake_session_mcp_projection)
    monkeypatch.setattr(mirror, "place_single_binary_array", lambda *a, **k: [])
    return mirror.deepseek_projection(agent, session_key="dashboard:chat-1")


def test_the_projection_carries_the_spec_denials(monkeypatch) -> None:
    specs = {"crew-narrow": {"tools": ["execute_bash", "fs_read", "grep", "glob"]}}
    assert _projection(monkeypatch, specs, "crew-narrow").denied_builtin_tools == ("edit", "write")


def test_a_workspace_alias_uses_its_base_seats_spec(monkeypatch) -> None:
    """crew-deepseek-atlas has no spec file of its own. Session selection hands the
    mirror the alias's kiro_agent (crew-deepseek), so the base spec decides; and when a
    caller passes the raw alias name, the missing spec denies nothing rather than
    everything, so the session still launches (the read-back then expects ``()``)."""
    specs = {"crew-deepseek": {"tools": list(CURRENT_DEEPSEEK_SPEC)}}
    assert _projection(monkeypatch, specs, "crew-deepseek").denied_builtin_tools == ()
    assert _projection(monkeypatch, specs, "crew-deepseek-atlas").denied_builtin_tools == ()


# ── the read-back ──────────────────────────────────────────────────────────────


def _marker(denied: object) -> dict:
    marker = {
        "plugin": "kiro-crew-tool-gate",
        "nonce": "n1",
        "module": "/sealed/gate.mjs",
        "approval": {"policy": "ask", "answerers": [
            {"entry": "include:acp", "module": "@deepseek-ai/dsh-acp", "plugin": "acp"}]},
        "tools": {"mode": "native"},
        "composition": {"entries": [], "tools": None},
        "child_env": {"version": "0.2.2-rc.5", "names": [], "parent_missing": [],
                      "child_visible": [], "error": None},
    }
    if denied is not _MISSING:
        marker["denied_tools"] = denied
    return marker


_MISSING = object()


def _issue(marker: dict, expected: tuple[str, ...]) -> str:
    return gate.gate_marker_issue(
        ACP_BACKEND_DEEPSEEK, marker, "/sealed/gate.mjs", "n1", denied_tools=expected
    )


def test_a_matching_denied_set_is_accepted() -> None:
    assert _issue(_marker(["edit", "write"]), ("write", "edit")) == ""
    assert _issue(_marker([]), ()) == ""


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (["write"], ("edit", "write")),  # the plugin denies less than the spec asks
        (["bash", "edit", "write"], ("edit", "write")),  # or more
        ([], ("edit", "write")),
    ],
)
def test_a_different_denied_set_is_refused(reported, expected) -> None:
    assert "denied built-in tools" in _issue(_marker(reported), expected)


@pytest.mark.parametrize("reported", [_MISSING, None, "write", [1]])
def test_a_missing_or_malformed_denied_set_is_refused(reported) -> None:
    assert "denied built-in tools" in _issue(_marker(reported), ())


# ── the launch hands the set to the child ─────────────────────────────────────


def test_the_session_child_env_carries_the_set() -> None:
    from kiro_crew.acp.harness.deepseek import _ENV_DSH_DENIED_TOOLS, DeepseekLaunch

    launch = DeepseekLaunch()
    launch._denied_tools = ("edit", "write")
    env: dict[str, str] = {_ENV_DSH_DENIED_TOOLS: "stale"}
    launch.apply_spawn_env(env)
    assert env[_ENV_DSH_DENIED_TOOLS] == "edit:write"

    unrestricted = DeepseekLaunch()
    env = {_ENV_DSH_DENIED_TOOLS: "write"}  # an inherited value never survives
    unrestricted.apply_spawn_env(env)
    assert env[_ENV_DSH_DENIED_TOOLS] == ""


def test_the_set_name_is_reserved_from_operator_env() -> None:
    from kiro_crew.acp.harness import deepseek

    assert deepseek._ENV_DSH_DENIED_TOOLS in deepseek._DEEPSEEK_ENV_CREW_OWNED_NAMES


# ── the plugin, under Node ─────────────────────────────────────────────────────

_PLUGIN = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src/kiro_crew/agent_sdk/gate_extensions/deepseek/kiro_crew_tool_gate.mjs"
)
_NODE = shutil.which("node")

_DRIVER = r"""
const [pluginUrl, marker, denied] = process.argv.slice(2);
process.env.KIROCREW_DSH_GATE_MARKER = marker;
process.env.KIROCREW_DSH_GATE_SESSION = "n1";
process.env.KIROCREW_DSH_DENIED_TOOLS = denied;
const plugin = await import(pluginUrl);
const listeners = {};
class NativeToolExecutor {}
const ctx = {
  on(event, fn) { listeners[event] = fn; },
  appReady: { onReady(cb) { cb(); } },
  subprocess: { spawn() { throw new Error("no subprocess in this test"); } },
  tools: { selectExecutor() { return new NativeToolExecutor(); } },
  get(name) { return name === "loader" ? { entries: () => [] } : undefined; },
  root: { events: { _hooks: {} } },
  approval: { config: { policy: "ask" } },
};
plugin.apply(ctx);
const pre = listeners["tools/pre-execute"];
const allow = async () => ({ kind: "allow" });
const calls = {
  write: await pre({ name: "write" }, allow),
  edit: await pre({ name: "edit" }, allow),
  read: await pre({ name: "read" }, allow),
  web_search: await pre({ name: "web_search" }, allow),
};
const fs = await import("node:fs");
for (let i = 0; i < 200 && !fs.existsSync(marker); i++) await new Promise((r) => setTimeout(r, 10));
const body = JSON.parse(fs.readFileSync(marker, "utf8"));
process.stdout.write(JSON.stringify({ calls, denied_tools: body.denied_tools }));
"""


@pytest.mark.skipif(_NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("env", "denied", "reported"),
    [
        ("write:edit", {"write", "edit"}, ["edit", "write"]),
        ("", set(), []),
    ],
)
def test_the_plugin_denies_the_set_and_reports_it(tmp_path, env, denied, reported) -> None:
    driver = tmp_path / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    marker = tmp_path / "marker.json"
    assert _NODE is not None
    done = subprocess.run(
        [_NODE, str(driver), _PLUGIN.as_uri(), str(marker), env],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    for name, verdict in out["calls"].items():
        if name in denied:
            assert verdict["kind"] == "deny", name
            assert "not in this agent's tool list" in verdict["reason"]
        else:
            assert verdict["kind"] == "ask", name
    assert out["denied_tools"] == reported
