"""The shipped DeepSeek Harness gate plugin, run under Node against a stand-in context.

Covers the two plugin-side halves of the PTC refusal: the ``tools/pre-execute``
listener denies the ``run_code`` transport and any call carrying a PTC ``parent``
token, and the load marker names the root executor by its class, so only the stock
``NativeToolExecutor`` reads as ``native``.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from kiro_crew import acp_tool_gate as gate
from kiro_crew.acp_backends import ACP_BACKEND_DEEPSEEK

_PLUGIN = (
    pathlib.Path(__file__).resolve().parent.parent
    / "src/kiro_crew/agent_sdk/gate_extensions/deepseek/kiro_crew_tool_gate.mjs"
)

_NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(_NODE is None, reason="node is not installed")

#: Loads the plugin into a stand-in Cordis context, drives its listener, waits for
#: the marker, and prints both as one JSON line. argv: plugin URL, marker path,
#: executor class name ("" = no executor selected).
_DRIVER = r"""
const [pluginUrl, marker, executorName] = process.argv.slice(2);
process.env.KIROCREW_DSH_GATE_MARKER = marker;
process.env.KIROCREW_DSH_GATE_SESSION = "n1";
const plugin = await import(pluginUrl);
const listeners = {};
const Executor = executorName ? { [executorName]: class {} }[executorName] : undefined;
const ctx = {
  on(event, fn) { listeners[event] = fn; },
  appReady: { onReady(cb) { cb(); } },
  subprocess: { spawn() { throw new Error("no subprocess in this test"); } },
  tools: {
    selectExecutor() {
      if (!Executor) throw new Error("TOOL_EXECUTOR_UNAVAILABLE");
      return new Executor();
    },
  },
  get(name) { return name === "loader" ? { entries: () => [] } : undefined; },
  root: { events: { _hooks: {} } },
  approval: { config: { policy: "ask" } },
};
plugin.apply(ctx);
const pre = listeners["tools/pre-execute"];
const allow = async () => ({ kind: "allow" });
const deny = async () => ({ kind: "deny", reason: "harness policy" });
const calls = {
  run_code: await pre({ name: "run_code" }, allow),
  sub_dispatch: await pre({ name: "read", parent: Symbol("token") }, allow),
  native: await pre({ name: "read" }, allow),
  native_denied_downstream: await pre({ name: "bash" }, deny),
};
const fs = await import("node:fs");
for (let i = 0; i < 200 && !fs.existsSync(marker); i++) {
  await new Promise((r) => setTimeout(r, 10));
}
const body = JSON.parse(fs.readFileSync(marker, "utf8"));
process.stdout.write(JSON.stringify({ calls, tools: body.tools }));
"""


def _drive(tmp_path: pathlib.Path, executor: str) -> dict:
    driver = tmp_path / "driver.mjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    marker = tmp_path / "marker.json"
    assert _NODE is not None
    done = subprocess.run(
        [_NODE, str(driver), _PLUGIN.as_uri(), str(marker), executor],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_the_listener_denies_ptc_and_asks_for_every_native_call(tmp_path) -> None:
    calls = _drive(tmp_path, "NativeToolExecutor")["calls"]
    assert calls["run_code"]["kind"] == "deny"
    assert "run_code transport" in calls["run_code"]["reason"]
    assert calls["sub_dispatch"]["kind"] == "deny"
    assert "dispatched from a run_code program" in calls["sub_dispatch"]["reason"]
    # A native call still asks, and a harness deny is kept as a deny.
    assert calls["native"]["kind"] == "ask"
    assert calls["native_denied_downstream"] == {"kind": "deny", "reason": "harness policy"}


@pytest.mark.parametrize(
    ("executor", "mode"),
    [
        ("NativeToolExecutor", "native"),
        ("PtcExecutor", "executor:PtcExecutor"),
        # A renamed or minified class fails closed: it is not reported as native.
        ("a", "executor:a"),
        ("", None),
    ],
)
def test_the_marker_names_the_root_executor_by_class(tmp_path, executor: str, mode) -> None:
    """Only the stock native class reads as ``native``; Kiro Crew refuses the rest."""
    tools = _drive(tmp_path, executor)["tools"]
    assert tools == {"mode": mode}
    marker = {
        "plugin": "kiro-crew-tool-gate",
        "nonce": "n1",
        "module": "/sealed/gate.mjs",
        "approval": {
            "policy": "ask",
            "answerers": [
                {"entry": "include:acp", "module": "@deepseek-ai/dsh-acp", "plugin": "acp"}
            ],
        },
        "tools": tools,
        "composition": {"entries": [], "tools": None},
        "child_env": {
            "version": "0.2.2-rc.5",
            "names": [],
            "parent_missing": [],
            "child_visible": [],
            "error": None,
        },
    }
    issue = gate.gate_marker_issue(ACP_BACKEND_DEEPSEEK, marker, "/sealed/gate.mjs", "n1")
    if mode == "native":
        assert issue == ""
    else:
        assert "tool presentation" in issue
