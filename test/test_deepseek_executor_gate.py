"""Exercise the shipped gate against DSH's executor-based tool registry."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from kiro_crew.acp.harness.deepseek import deepseek_gate_extension_path
from kiro_crew.testing.wait import default_timeout

_PROBE = r"""
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';
const [plugin, marker, scenario] = process.argv.slice(2);
process.env.KIROCREW_DSH_GATE_MARKER = marker;
process.env.KIROCREW_DSH_GATE_SESSION = 'test';
process.env.KIROCREW_DSH_GATE_SCRUB_NAMES = '';
const gate = await import(pathToFileURL(plugin).href);
const owner = {
  entry: { id: 'include:tool-executor-native',
           options: { name: '@deepseek-ai/dsh-tool-executor-native' } },
  runtime: { name: 'native-tool-executor' },
};
class NativeToolExecutor { pipeline = { ctx: { fiber: owner } }; }
let executor = new NativeToolExecutor({ fiber: owner, tools: {} });
const tools = { selectExecutor: () => executor };
if (scenario === 'ptc') executor = { constructor: { name: 'PtcToolExecutor' } };
if (scenario === 'foreign') owner.entry.options.name = '/foreign/executor.mjs';
if (scenario === 'wrong-entry') owner.entry.id = 'include:other';
if (scenario === 'wrong-plugin') owner.runtime.name = 'other';
if (scenario === 'missing') executor = undefined;
if (scenario === 'throws') tools.selectExecutor = () => { throw Error('unloaded'); };
if (scenario === 'invalid-selector') {
  tools.selectExecutor = null;
  tools.defaultMode = 'native';
}
if (scenario === 'ptc') tools.defaultMode = 'native';
if (scenario === 'legacy') {
  delete tools.selectExecutor;
  tools.defaultMode = 'native';
}
let ready;
const ctx = {
  tools,
  on() {},
  get() {},
  appReady: { onReady(callback) { ready = callback; } },
  subprocess: { spawn() { return {
    done: Promise.resolve({ exitCode: 0 }),
    collected: { stdout: { readFrom: () => ({ text: '[]', lossy: false }) } },
  }; } },
};
gate.apply(ctx);
ready();
// Flush the marker promise chain without polling or a clock-based wait.
await new Promise(resolve => setImmediate(resolve));
console.log(JSON.stringify(JSON.parse(readFileSync(marker, 'utf8')).tools));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node to load the gate")
@pytest.mark.parametrize(
    ("scenario", "mode"),
    [
        ("native", "native"),
        ("legacy", "native"),
        ("ptc", None),
        ("foreign", None),
        ("wrong-entry", None),
        ("wrong-plugin", None),
        ("missing", None),
        ("throws", None),
        ("invalid-selector", None),
    ],
)
def test_only_the_stock_native_executor_is_admitted(tmp_path, scenario, mode):
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-",
            deepseek_gate_extension_path(),
            str(tmp_path / "marker.json"),
            scenario,
        ],
        input=_PROBE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=default_timeout(),
        cwd=tmp_path,
    )
    assert json.loads(result.stdout) == {"mode": mode}
