"""Measure native Codex ACP frames on a disposable Linux CI runner without secrets."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import tempfile
import threading
import time


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'{"data":[],"models":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            {"type": "response.created", "response": {"id": "resp_stub"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": "msg_stub",
                    "content": [{"type": "output_text", "text": "Measured acknowledgement."}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_stub",
                    "usage": {
                        "input_tokens": 100,
                        "input_tokens_details": None,
                        "output_tokens": 5,
                        "output_tokens_details": None,
                        "total_tokens": 105,
                    },
                },
            },
        ]
        for event in events:
            self.wfile.write((f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n').encode())
            self.wfile.flush()


class Agent:
    def __init__(self, entry, root, port):
        home, codex, self.work = root / "home", root / "codex", root / "work"
        for directory in (home, codex, self.work):
            directory.mkdir()
        (codex / "config.toml").write_text(
            'model = "gpt-5-codex"\nmodel_provider = "stub"\n'
            '[model_providers.stub]\nname = "stub"\n'
            f'base_url = "http://127.0.0.1:{port}/v1"\nwire_api = "responses"\n'
            'env_key = "KIROCREW_STUB_MODEL_KEY"\n'
            "request_max_retries = 0\nstream_max_retries = 0\n",
            encoding="utf-8",
        )
        (codex / "auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "sk-not-a-real-key-" + "0" * 24}), encoding="utf-8"
        )
        self.frames, self.serial, self.lines = [], 0, queue.Queue()
        self.stderr = (root / "stderr.log").open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            ["node", str(entry)],
            cwd=self.work,
            env={
                "PATH": os.environ["PATH"],
                "HOME": str(home),
                "CODEX_HOME": str(codex),
                "NO_BROWSER": "1",
                "KIROCREW_STUB_MODEL_KEY": "stub-key",
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            encoding="utf-8",
            bufsize=1,
            start_new_session=True,
        )
        self.reader = threading.Thread(target=self.pump, daemon=True)
        self.reader.start()

    def pump(self):
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def write(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method, params, timeout=90):
        self.serial += 1
        rid = self.serial
        self.write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"ACP {method} exceeded {timeout}s")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"ACP {method} produced no reply") from exc
            if line is None:
                raise RuntimeError(f"ACP exited during {method}; inspect diagnostic artifact")
            message = json.loads(line)
            self.frames.append(message)
            if "method" in message and "id" in message:
                if message["method"] == "session/request_permission":
                    answer = {"result": {"outcome": {"outcome": "cancelled"}}}
                else:
                    answer = {"error": {"code": -32601, "message": "Unsupported probe request"}}
                self.write({"jsonrpc": "2.0", "id": message["id"], **answer})
            elif message.get("id") == rid:
                if "error" in message:
                    raise RuntimeError(f"ACP {method} failed: {message['error']}")
                return message["result"]

    def close(self):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                if sig == signal.SIGKILL:
                    raise
        self.reader.join(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()
        self.stderr.close()


def capture(entry, output, notices_only):
    label = "native-notice-live" if notices_only else "native-compaction-live"
    capabilities = {"notices": {}}
    if not notices_only:
        capabilities["compaction"] = {}
    with tempfile.TemporaryDirectory(prefix="codex-native-", dir=output) as temporary:
        root = Path(temporary)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        agent = None
        try:
            agent = Agent(entry, root, server.server_address[1])
            identity = agent.call(
                "initialize",
                {"protocolVersion": 1, "clientCapabilities": {"session": capabilities}},
            )
            info = identity.get("agentInfo", {})
            if info.get("version") != "2.3.0" or "codex" not in info.get("name", "").lower():
                raise RuntimeError(f"Unexpected adapter identity: {info}")
            session = agent.call("session/new", {"cwd": str(agent.work), "mcpServers": []})
            sid = session["sessionId"]
            for text in ("Reply with one acknowledgement.", "/compact"):
                agent.call(
                    "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}
                )
            updates = [
                frame
                for frame in agent.frames
                if frame.get("method") == "session/update"
                and frame.get("params", {}).get("update", {}).get("sessionUpdate")
                in {"compaction_update", "notice"}
            ]
            kinds = {frame["params"]["update"]["sessionUpdate"] for frame in updates}
            needed = "notice" if notices_only else "compaction_update"
            if needed not in kinds:
                raise RuntimeError(f"No native {needed} observed; inspect diagnostic artifact")
            updates = json.loads(json.dumps(updates).replace(sid, "codex-measured-session-1"))
            ids = {}
            for frame in updates:
                update = frame["params"]["update"]
                if "compactionId" in update:
                    old = update["compactionId"]
                    update["compactionId"] = ids.setdefault(
                        old, f"codex-measured-compaction-{len(ids)+1}"
                    )
            metadata = {
                "_meta": {
                    "backend": "codex",
                    "recorded": "live",
                    "agent_version": info["version"],
                    "agent_name": info["name"],
                    "note": "Real adapter and Codex0.164.0 on clean Linux CI; private homes; loopback model stub; "
                    f"session capabilities {json.dumps(capabilities)}. IDs normalized; unrelated frames omitted.",
                }
            }
            with (output / f"{label}.jsonl").open("w", encoding="utf-8") as target:
                for frame in [metadata, {"jsonrpc": "2.0", "id": 1, "result": identity}, *updates]:
                    target.write(json.dumps(frame) + "\n")
            print(f"{label}: measured {len(updates)} native frames", flush=True)
        finally:
            try:
                if agent is not None:
                    agent.close()
                    raw = json.dumps(agent.frames).replace(str(root), "<probe-root>")
                    (output / f"{label}.diagnostic.json").write_text(raw, encoding="utf-8")
                    stderr = (root / "stderr.log").read_text(encoding="utf-8")
                    (output / f"{label}.stderr.log").write_text(
                        stderr.replace(str(root), "<probe-root>"), encoding="utf-8"
                    )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "posix" or os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("This diagnostic requires a clean Linux GitHub Actions runner")
    args.output.mkdir(parents=True, exist_ok=True)
    for notices_only in (False, True):
        capture(args.entry.resolve(), args.output.resolve(), notices_only)


if __name__ == "__main__":
    main()
