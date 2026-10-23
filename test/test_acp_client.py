"""Tests for ACP client."""

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _stop_stderr_drain  # noqa: F401 - re-exported

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    _format_acp_error,
)
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, JSONRPC_METHOD_NOT_FOUND, JsonRpcMessage
from kiro_crew.agent_sdk import host_auth
from kiro_crew.testing.wait import await_until


def _scripted_process(lines, *, returncode=None):
    """Build a mock subprocess whose stdout.readline yields *lines* in order.

    Each entry of *lines* is a dict (serialized to a JSON-RPC line) or a raw
    bytes value. After the list is exhausted, readline blocks-then-returns an
    empty timeout-friendly value so _read_message's wait_for sees nothing new.
    """
    queue = deque(lines)

    async def _readline():
        if queue:
            item = queue.popleft()
            if isinstance(item, (bytes, bytearray)):
                return bytes(item)
            return (json.dumps(item) + "\n").encode()
        # Nothing left — emulate a quiet stream (no more frames this read).
        await asyncio.sleep(0)
        return b""

    proc = MagicMock()
    stdout = AsyncMock()
    stdout.readline = AsyncMock(side_effect=_readline)
    proc.stdout = stdout
    proc.returncode = returncode
    # stdin used by _send_error / _send_response.
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    proc.stdin = stdin
    return proc


class TestWaitForResponseDeferral:
    """F1: _wait_for_response must not spin on inbound server requests or
    foreign-id responses, must not drop them, and must re-inject them."""

    @pytest.mark.asyncio
    async def test_inbound_permission_request_deferred_not_spun(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        # Awaiting response for req_id=7. A server->client permission request
        # arrives first carrying id=7 (colliding namespace), THEN the real
        # response for req 7 arrives.
        perm = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {"sessionId": "s", "options": [], "toolCall": {"title": "x"}},
        }
        resp = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
        client._process = _scripted_process([perm, resp])

        result = await await_until(asyncio.ensure_future(client._wait_for_response(7, timeout=5.0)))
        assert result == {"ok": True}
        # Permission request must be re-injected (not dropped, not spun).
        assert len(client._buffer) == 1
        buffered = client._buffer.popleft()
        assert buffered.method == "session/request_permission"
        assert buffered.id == 7

    @pytest.mark.asyncio
    async def test_foreign_id_response_preserved(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        foreign = {"jsonrpc": "2.0", "id": 99, "result": {"stale": True}}
        resp = {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
        client._process = _scripted_process([foreign, resp])

        result = await await_until(asyncio.ensure_future(client._wait_for_response(3, timeout=5.0)))
        assert result == {"ok": True}
        assert len(client._buffer) == 1
        assert client._buffer.popleft().id == 99

    @pytest.mark.asyncio
    async def test_notification_goes_to_mcp_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        resp = {"jsonrpc": "2.0", "id": 5, "result": {"ok": True}}
        client._process = _scripted_process([notif, resp])

        result = await await_until(asyncio.ensure_future(client._wait_for_response(5, timeout=5.0)))
        assert result == {"ok": True}
        # Notification buffered for drain, NOT re-injected into _buffer.
        assert len(client._buffer) == 0
        assert len(client._mcp_notifications) == 1
        assert client._mcp_notifications[0].method == "session/update"

    @pytest.mark.asyncio
    async def test_deferred_reinjected_in_order_on_timeout(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        first = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {},
        }
        second = {"jsonrpc": "2.0", "id": 88, "result": {"other": True}}
        # No matching response ever arrives -> timeout. Deferred frames must
        # still be re-injected in arrival order.
        client._process = _scripted_process([first, second])

        with pytest.raises(AcpError):
            await await_until(asyncio.ensure_future(client._wait_for_response(7, timeout=1.0)))
        assert len(client._buffer) == 2
        m0 = client._buffer.popleft()
        m1 = client._buffer.popleft()
        assert m0.method == "session/request_permission"
        assert m1.id == 88

    def test_session_timeout_progress_names_missing_failed_and_oauth_servers(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            ),
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_init_failure",
                params={
                    "serverName": "broken",
                    "error": "aws_secret_access_key=supersecret connection failed",
                },
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"}),
        ]

        progress = client._mcp_timeout_progress(
            [{"name": "ready"}, {"name": "broken"}, {"name": "silent"}]
        )

        assert "2/3 session-injected MCP server(s) reported" in progress
        assert "no report from silent" in progress
        assert "failed: broken" in progress
        assert "supersecret" not in progress
        assert "awaiting authorization: oauth" in progress

    def test_session_timeout_progress_empty_roster_keeps_reports(self, tmp_path):
        """An empty ``mcpServers`` roster must not drop the staged reports.

        Mirrors ``AcpRuntime._mcp_init_progress``: with nothing to attribute
        against, the count is reported as ``roster unknown`` instead of the
        timeout reading as a bare budget.
        """
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            ),
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_init_failure",
                params={"serverName": "broken", "error": "connection failed"},
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"}),
        ]

        for expected in ([], None):
            progress = client._mcp_timeout_progress(expected)
            assert "2 MCP server report(s), roster unknown" in progress
            assert "session-injected" not in progress
            assert "failed: broken (connection failed)" in progress
            assert "awaiting authorization: oauth" in progress

    def test_session_timeout_progress_ignores_prior_attempt_frames(self, tmp_path):
        """A timed-out ``session/load``'s frames are not this attempt's progress.

        ``_drain_notifications`` clears the buffer only at the end of
        ``_initialize_session``, so the load -> ``session/new`` fallback still
        holds the previous attempt's ``server_initialized`` frames. The progress
        summary must honour the per-attempt floor ``_begin_session_report`` sets,
        or it asserts the stall is not in those servers on stale evidence.
        """
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        roster = [{"name": "ready"}]
        # Attempt 1 (session/load): the server reported and another asked for
        # authorization, then the load timed out.
        client._begin_session_report(roster)
        client._mcp_notifications.append(
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            )
        )
        client._mcp_notifications.append(
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"})
        )
        # Attempt 2 (session/new fallback): nothing has reported yet.
        client._begin_session_report(roster)

        progress = client._mcp_timeout_progress(roster)

        assert "0/1 session-injected MCP server(s) reported" in progress
        assert "no report from ready" in progress
        assert "not in those servers" not in progress
        # The authorization the failed attempt asked for is still owed by the
        # user, so it is not floored (same rule as _drain_notifications).
        assert "awaiting authorization: oauth" in progress

        # A frame that arrives during attempt 2 is counted.
        client._mcp_notifications.append(
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            )
        )
        progress = client._mcp_timeout_progress(roster)
        assert "1/1 session-injected MCP server(s) reported" in progress


class TestWaitForResponseActivityDeadline:
    """Low-A: a steady stream of notifications keeps _wait_for_response alive
    past the base timeout until the real response arrives."""

    @pytest.mark.asyncio
    async def test_streaming_notifications_extend_deadline(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)

        # Virtual clock so the test is fast and deterministic. Each readline
        # advances time by 0.05s; base timeout is 0.1s. Without activity-based
        # extension the call would die after ~2 reads; with it, the stream of
        # notifications keeps pushing the deadline out until the response lands.
        clock = {"t": 1000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        frames = [notif] * 20 + [{"jsonrpc": "2.0", "id": 4, "result": {"loaded": True}}]
        queue = deque(frames)

        async def _readline():
            if queue:
                clock["t"] += 0.05  # each frame advances the virtual clock
                return (json.dumps(queue.popleft()) + "\n").encode()
            return b""

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", fake_monotonic)

        result = await await_until(asyncio.ensure_future(client._wait_for_response(4, timeout=0.1)))
        assert result == {"loaded": True}
        # All 20 notifications were drained while the deadline kept extending.
        assert len(client._mcp_notifications) == 20

    @pytest.mark.asyncio
    async def test_hard_cap_eventually_times_out(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)
        clock = {"t": 5000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}

        async def _readline():
            # Endless notifications, large time jumps — never a matching
            # response. The absolute hard cap must eventually fire.
            clock["t"] += 30.0
            return (json.dumps(notif) + "\n").encode()

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", fake_monotonic)

        with pytest.raises(AcpError):
            await await_until(asyncio.ensure_future(client._wait_for_response(1, timeout=0.1)))


class TestProcessMessageUnknownServerRequest:
    """F5: unknown server->client requests are classified for a -32601 reply,
    not silently skipped."""

    def test_unknown_server_request_classified(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=12, method="fs/read_text_file", params={"path": "/x"})
        assert client._process_message(msg, req_id=1) == "server_request_unknown"

    def test_terminal_create_classified(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=3, method="terminal/create", params={})
        assert client._process_message(msg, req_id=1) == "server_request_unknown"

    def test_notification_still_skipped(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        # Unknown notification (method, NO id) is not a request -> skip.
        msg = JsonRpcMessage(method="some/unknown_notification", params={})
        assert client._process_message(msg, req_id=1) == "skip"

    def test_known_permission_request_not_unknown(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=2, method="session/request_permission", params={})
        assert client._process_message(msg, req_id=1) == "permission"

    @pytest.mark.asyncio
    async def test_reject_sends_method_not_found_error(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=42, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)

        client._process.stdin.write.assert_called_once()
        written = client._process.stdin.write.call_args[0][0].decode()
        payload = json.loads(written)
        assert payload["id"] == 42
        assert payload["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        assert "terminal/create" in payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_reject_noop_when_no_id(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=None, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)
        client._process.stdin.write.assert_not_called()


class TestFormatAcpError:
    """Tests for _format_acp_error — Bedrock-aware error rewriting.

    Covers the bug filed at task 86089e43: ACP backend errors used
    to be surfaced as the raw JSON-RPC dict (`Prompt error: {'code': -32603,
    ...}`), which dead-ends users when the picker can't expose a valid
    alternative. The helper rewrites known Bedrock failures into actionable
    text while preserving the request_id for support correlation, and scrubs
    embedded credentials / exfiltration URLs as defense-in-depth.
    """

    def test_non_dict_falls_back(self):
        assert _format_acp_error(None) == "Prompt error: None"
        assert _format_acp_error("boom") == "Prompt error: boom"

    def test_unknown_dict_preserves_raw(self):
        """An unrecognised shape must lose no information.

        The fallback now leads with the provider's own text rather than a repr
        of the JSON-RPC dict, but both fields still have to survive: the
        provider detail AND the non-boilerplate JSON-RPC message, which for
        codes other than -32603 is the only summary the error carries.
        """
        err = {"code": -32000, "message": "Something else", "data": "weird"}
        out = _format_acp_error(err)
        assert "Something else" in out
        assert "weird" in out
        # No dict repr: punctuation soup is not a user-facing error message.
        assert "Prompt error: {" not in out

    def test_model_not_available_rewrite(self):
        # With NO advertised list the caller cannot know whether this is an
        # entitlement or a capacity failure, so it keeps the capacity wording.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: The model 'opus' "
                "is not available. Please use '/model' to select a different "
                "model and try again. (request_id: 3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e)"
            ),
        }
        out = _format_acp_error(err)
        assert "unavailable on the backend" in out
        assert "'opus'" in out
        assert "model picker" in out
        # Must NOT point at ~/.claude/settings.json — KiroCrew never reads it;
        # the model lives in config.json / the agent spec.
        assert "settings.json" not in out
        assert "config.json" in out
        # Nor echo the provider's own "use '/model'" advice: that is a kiro-cli
        # TUI command, inert in the dashboard, Slack, and cron.
        assert "/model" not in out
        # Request id is preserved for support correlation.
        assert "3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e" in out
        # Should not leak the raw dict prefix when we have a real rewrite.
        assert "Prompt error: {" not in out

    def test_unentitled_model_names_what_the_account_can_use(self):
        """The free-tier case: say it is an access problem and list the options.

        Upstream sends the SAME string for entitlement and capacity failures,
        so the advertised list is the only thing that distinguishes them.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'opus-4.8-1m' is not available. (request_id: abc-123)",
        }
        out = _format_acp_error(err, ["claude-sonnet-4-6", "claude-haiku-4-5"])
        assert "does not have access" in out
        assert "'opus-4.8-1m'" in out
        # The actionable part: what they CAN pick.
        assert "claude-sonnet-4-6" in out
        assert "claude-haiku-4-5" in out
        # Must NOT blame capacity or tell them to wait — no retry will help.
        assert "capacity" not in out
        assert "wait a minute" not in out
        assert "abc-123" in out

    def test_advertised_model_still_reads_as_capacity(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The model IS on offer, so a rejection really is a transient blip and
        # must keep the capacity wording (and stay retryable).
        err = {"code": -32603, "message": "x", "data": "The model 'opus' is not available."}
        out = _format_acp_error(err, ["opus", "sonnet"])
        assert "unavailable on the backend" in out
        assert "does not have access" not in out
        assert _is_transient_raw_error(err, ["opus", "sonnet"]) is True

    def test_unentitled_model_is_terminal_not_retried(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The retry verdict must move with the wording -- retrying a model the
        # account was never offered just reproduces the same rejection.
        err = {"code": -32603, "message": "x", "data": "The model 'opus' is not available."}
        assert _is_transient_raw_error(err, ["sonnet", "haiku"]) is False
        # Unknown entitlement -> unchanged (transient), never a false accusation.
        assert _is_transient_raw_error(err, None) is True
        assert _is_transient_raw_error(err, []) is True

    def test_unentitled_match_is_case_insensitive(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # An entitled-but-differently-cased model must not be called unentitled.
        err = {"code": -32603, "message": "x", "data": "The model 'Opus' is not available."}
        assert _is_transient_raw_error(err, ["opus"]) is True
        assert "does not have access" not in _format_acp_error(err, ["opus"])

    def test_long_available_list_is_capped(self):
        err = {"code": -32603, "message": "x", "data": "The model 'nope' is not available."}
        out = _format_acp_error(err, [f"model-{i}" for i in range(12)])
        assert "+4 more" in out
        assert "model-7" in out
        assert "model-8" not in out

    def test_throttling_exception_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "ThrottlingException: Too many requests "
                "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
            ),
        }
        out = _format_acp_error(err)
        assert "throttling" in out.lower()
        assert "wait" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out

    def test_too_many_requests_rewrite(self):
        err = {"code": -32603, "message": "x", "data": "TooManyRequestsException: rate limited"}
        out = _format_acp_error(err)
        assert "throttling" in out.lower()

    def test_access_denied_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDeniedException: not authorized to invoke",
        }
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()
        assert "aws sso login" in out.lower()

    def test_expired_token_rewrite(self):
        err = {"code": -32603, "message": "x", "data": "ExpiredToken: signature expired"}
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()

    def test_missing_request_id_omits_suffix(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'sonnet' is not available.",
        }
        out = _format_acp_error(err)
        assert "request_id" not in out
        assert "'sonnet'" in out

    def test_throttle_keyword_in_message(self):
        # Some backends put the trigger word in `message` rather than `data`.
        err = {"code": -32603, "message": "Rate limit exceeded", "data": ""}
        out = _format_acp_error(err)
        assert "throttling" in out.lower()

    def test_credentials_in_data_are_redacted(self):
        """AWS access keys embedded in upstream errors must not leak to the UI.

        Recognized error patterns (auth/throttle/model-unavailable) already drop
        the `data` field when constructing the rewritten message, so the secret
        is gone simply by virtue of the rewrite. The redaction layer is the
        defense-in-depth fallback for the unknown-shape path; this test pins
        the absence guarantee on the recognized-pattern path.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDenied: AKIAIOSFODNN7EXAMPLE not authorized",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_credentials_in_unknown_dict_fallback_are_redacted(self):
        """The fallback path echoes raw dict — must still scrub secrets."""
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        # The scrubbed placeholder proves redaction ran on this path.
        assert "REDACTED" in out
        # The surrounding provider text still reaches the user.
        assert "leak:" in out

    def test_exfiltration_url_in_unknown_dict_fallback_is_redacted(self):
        """The fallback path echoes raw dict — exfil URLs must also be scrubbed.

        Pairs with `test_credentials_in_unknown_dict_fallback_are_redacted` to
        cover the second redaction layer (`redact_exfiltration_urls`).
        """
        # Use a URL whose query carries a base64-blob — matches _EXFIL_PATTERNS
        # and is what real provider error payloads tend to look like when they
        # echo signed callback URLs.
        leaked_blob = "QUtJQUlPU0ZPRE5ON0VYQU1QTEVTRUNSRVRBQ0NFU1NLRVk" + "A" * 30
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": f"callback to https://attacker.example.com/exfil?token={leaked_blob}",
        }
        out = _format_acp_error(err)
        assert leaked_blob not in out, "leaked credential blob must be redacted"
        assert "REDACTED" in out

    def test_sensitive_content_emits_log_warning(self, caplog):
        """When the redaction layer scrubs anything, a warning MUST be logged.

        Silent scrubbing hides upstream-leak signals from security review;
        the warning lets operators notice that a provider echoed sensitive
        content back. The warning intentionally includes only counts — never
        the redacted values.
        """
        import logging

        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            _format_acp_error(err)

        warnings = [r for r in caplog.records if "sensitive content" in r.getMessage()]
        assert warnings, "expected a redaction warning to be logged"
        assert "AKIAIOSFODNN7EXAMPLE" not in warnings[0].getMessage()

    def test_internal_server_error_rewrite(self):
        """A transient 5xx from the backend must

        classify as a momentary backend error rather than dumping the raw
        -32603 JSON-RPC dict at the user.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: "
                "CodewhispererChatResponseStream(ServiceError(ServiceError "
                '{ name: "InternalServerError", ... please try again ... })) '
                "(request_id: 91c99864-7d2e-4f0a-9b1c-2a3b4c5d6e7f)"
            ),
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "retry in a moment" in out.lower()
        assert "5xx" in out
        # Request id preserved for support correlation.
        assert "91c99864-7d2e-4f0a-9b1c-2a3b4c5d6e7f" in out
        # Must not dead-end the user with the raw dict.
        assert "Prompt error: {" not in out

    def test_please_try_again_phrasing_rewrite(self):
        """A bare 'please try again' transient hint (no explicit 5xx token)

        still classifies as transient.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Service hiccup, please try again.",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "Prompt error: {" not in out

    def test_dispatch_failure_rewrite(self):
        """DispatchFailure / connection-reset shapes are transient too."""
        err = {
            "code": -32603,
            "message": "x",
            "data": "DispatchFailure: ConnectionReset (request_id: bbbb2222-cccc-3333-dddd-444455556666)",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "bbbb2222-cccc-3333-dddd-444455556666" in out

    def test_transient_5xx_missing_request_id_omits_suffix(self):
        err = {"code": -32603, "message": "Internal error", "data": "InternalServerError occurred"}
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "request_id" not in out

    def test_transient_branch_does_not_leak_credentials(self):
        """Defense-in-depth on the new transient path: an AWS key embedded in

        the upstream `data` must never reach the UI. The rewrite drops the
        `data` field (so the secret is gone by construction), and the function
        still routes the result through redact_credentials/redact_exfiltration_urls
        at the end — the new branch adds no early return that bypasses it.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "InternalServerError: leaked AKIAIOSFODNN7EXAMPLE please try again",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "transient error" in out.lower()

    def test_throttle_precedence_over_transient(self):
        """A throttle that also says 'please try again' stays a throttle —

        throttling precedence must survive the new transient branch.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ThrottlingException: slow down, please try again",
        }
        out = _format_acp_error(err)
        assert "throttling" in out.lower()
        assert "transient error" not in out.lower()

    def test_non_transient_minus_32603_falls_through_to_fallback(self):
        """A non-transient -32603 whose only 'internal'-ish signal is the

        canonical JSON-RPC message 'Internal error' must NOT be misclassified
        as transient. The transient branch keys off the provider `data` field
        only (never the generic `message`), so a deterministic failure like a
        ValidationException surfaces its real cause instead.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        # The real cause is shown verbatim — stronger than the old assertion,
        # which only required a dict repr containing it somewhere.
        assert "ValidationException: input contains an unsupported field 'foo'" in out
        # The -32603 boilerplate message is dropped as noise next to that.
        assert "Internal error" not in out

    def test_bare_500_token_is_not_treated_as_5xx(self):
        """A standalone numeric (e.g. a token-limit value) must not match the

        5xx branch — the numeric match is anchored to an HTTP/status context,
        not a bare 500/502/503 token.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "max_tokens 500 exceeds the model limit of 200000",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "max_tokens 500 exceeds the model limit of 200000" in out

    def test_http_500_status_context_still_classifies_transient(self):
        """An HTTP/status-anchored 50x token IS a genuine transient signal."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "upstream returned HTTP 503 (request_id: aaaa1111-bbbb-2222-cccc-333344445555)",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out

    def test_kiro_generic_generation_failure_formats_as_transient(self):
        """kiro-cli's pre-stream generation failure wrapper gets the friendly
        retry guidance instead of the raw-dict fallback: data carried no
        request_id and no error class, only the generic wrapper string.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Kiro failed to generate a response",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "Prompt error: {" not in out

    def test_generation_failure_phrase_in_message_only_is_not_transient(self):
        """The generation-failure pattern is scoped to `data` — the phrase
        appearing only in the JSON-RPC `message` field must not trigger the
        friendly transient branch (mirrors the model-unavailable scoping)."""
        err = {
            "code": -32603,
            "message": "Kiro failed to generate a response",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "ValidationException: input contains an unsupported field 'foo'" in out

    def test_kiro_process_failure_formats_as_transient_and_keeps_request_id(self):
        """kiro-cli's post-stream sibling wrapper ("The service failed to
        process the request (request_id: ...)") gets the same retry guidance
        as the generation-failure branch, and the request_id survives the
        rewrite so a support thread can quote it.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "The service failed to process the request "
                "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
            ),
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out
        assert "Prompt error: {" not in out
        # The string fallback in llm_helpers classifies both the raw wrapper
        # and the rewrite, so a path that lost the structured flag still retries.
        from kiro_crew.llm_helpers import is_transient_backend_error

        assert is_transient_backend_error(str(err["data"])) is True
        assert is_transient_backend_error(out) is True

    def test_process_failure_phrase_in_message_only_is_not_transient(self):
        """Scoped to `data` like its sibling: the phrase in the JSON-RPC
        `message` alone must not flip a deterministic failure to transient."""
        err = {
            "code": -32603,
            "message": "The service failed to process the request",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "ValidationException: input contains an unsupported field 'foo'" in out

    def test_session_expired_rewrite(self):
        """An expired session gets actionable sign-in guidance rather than the
        misleading transient-5xx retry advice.

        The sign-in half is the failing harness's own declared remedy, so this
        asserts it is rendered verbatim rather than pinning the wording here: a
        second copy of that wording is what let the message name `kiro-cli login`
        to an operator on a harness that signs in some other way.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "DispatchFailure: session expired",
        }
        out = _format_acp_error(err)
        # The signed-out MESSAGE, not the remedy. This arm reaches the text on real
        # evidence (a 401/403, or prose saying the session expired), so it may assert
        # the state; the remedy is the register for a caveat rendered having measured
        # nothing, and it deliberately makes no state claim.
        assert host_auth.signed_out_message("") in out
        assert "kiro-cli login" in out.lower()
        assert "retry" in out.lower() and "will not help" in out.lower()
        # Must NOT show the misleading 5xx message.
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_session_expired_names_the_failing_harness(self):
        """A harness that signs in elsewhere gets ITS message, not kiro-cli's.

        The regression this guards: the arm hardcoded `kiro-cli login`, so an
        expiry on a harness with its own credential file told the operator to run
        a command that could not fix it. The retry verdict is this arm's own
        finding about the error, so it must survive on every backend.

        Reads ``signed_out_message`` and not ``sign_in_remedy``, and the
        difference is the point: this arm HAS evidence -- a 401, or prose saying
        the session expired -- so it may assert the state. The remedy is the
        register for the places that render a standing line having measured
        nothing, and using it here would drop the diagnosis.
        """
        err = {"code": -32603, "message": "Internal error", "data": "HTTP 401"}
        for backend in ("claude", "codex"):
            out = _format_acp_error(err, backend=backend)
            assert host_auth.signed_out_message(backend) in out
            assert "kiro-cli login" not in out.lower(), backend
            assert "will not help" in out.lower(), backend

    def test_session_expired_by_http_status(self):
        """A bare 401/403 is the shape an expired session actually arrives in:
        the rejection carries no explanatory wording, so status alone must
        drive the classification."""
        for status in ("HTTP 401", "HTTP 403", "status code 401", "status 403"):
            err = {"code": -32603, "message": "Internal error", "data": status}
            out = _format_acp_error(err)
            assert "kiro-cli login" in out.lower(), f"No sign-in guidance for: {status!r}"
            assert "transient error" not in out.lower(), f"Misclassified: {status!r}"

    def test_session_expired_401_with_transport_error(self):
        """The reported failure mode: an aborted request leaves a transport
        error alongside the 401, and the 5xx family must not win and tell the
        user to retry."""
        err = {
            "code": -32603,
            "message": "Encountered an error in the response stream",
            "data": "DispatchFailure ConnectionResetError: HTTP 401",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_invalid_bearer_token_rewrite(self):
        """The account-switch rejection: a credential the running child still
        holds is rejected as invalid, with no status code and no expiry wording,
        so it must still reach the sign-in guidance instead of the raw string."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The bearer token included in the request is invalid.",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "retry" in out.lower() and "will not help" in out.lower()
        # Must NOT show the misleading transient-5xx advice.
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_invalid_bearer_token_wins_over_transport_error(self):
        """An aborted request leaves a transport error beside the rejection; the
        credential branch is checked first so the 5xx family cannot win."""
        err = {
            "code": -32603,
            "message": "Encountered an error in the response stream",
            "data": "DispatchFailure ConnectionResetError: the bearer token is invalid",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "transient error" not in out.lower()

    def test_unrelated_invalid_does_not_read_as_credential_failure(self):
        """The fenced gap must not let an unrelated 'invalid' in a combined
        haystack turn a validation fault into a sign-in prompt."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "ValidationException: refreshed the bearer token successfully. "
                "Field 'temperature' is invalid"
            ),
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" not in out.lower()

    def test_genuine_5xx_still_transient_with_auth_absent(self):
        """The new auth-status branch must not swallow real 5xx errors."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ServiceUnavailableException: HTTP 503",
        }
        out = _format_acp_error(err)
        assert "transient" in out.lower()
        assert "kiro-cli login" not in out.lower()

    def test_session_expired_variants(self):
        """Various kiro-cli session-expiry error shapes are all classified."""
        variants = [
            "not logged in",
            "session has expired",
            "login expired",
            "authentication required",
            "session timed out",
            "not authenticated",
            "login required",
        ]
        for text in variants:
            err = {"code": -32603, "message": "Internal error", "data": text}
            out = _format_acp_error(err)
            assert "transient error" not in out.lower(), f"Failed for: {text!r}"
            assert "kiro-cli login" in out.lower(), f"No login guidance for: {text!r}"

    def test_session_expired_with_5xx_token_wins(self):
        """Session expiry checked before 5xx: a DispatchFailure wrapping a
        session-expired message must surface as auth, not transient."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "DispatchFailure ConnectionResetError: session expired",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "kiro-cli login" in out.lower()


class TestCompactionFailureDetail:
    """`compaction_failure_detail` is what stops the notice collapsing to
    "unknown error": it prefers a named reason and otherwise surfaces the raw
    shape, redacted, because it lands on a chat row."""

    def test_prefers_a_named_reason(self):
        from kiro_crew.acp.client import compaction_failure_detail

        assert (
            compaction_failure_detail({"status": {"type": "failed", "reason": "too large"}})
            == "too large"
        )

    def test_reads_a_nested_error_object(self):
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail(
            {"status": {"type": "failed"}, "error": {"message": "throttled"}}
        )
        assert detail == "throttled"

    def test_falls_back_to_the_raw_shape(self):
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail({"status": {"type": "failed"}})
        assert "no reason reported" in detail
        assert "failed" in detail

    def test_is_bounded_and_redacted(self):
        from kiro_crew.acp.client import (
            _COMPACTION_DETAIL_MAX_CHARS,
            compaction_failure_detail,
        )

        detail = compaction_failure_detail({"status": {"type": "failed", "error": "x" * 5000}})
        assert len(detail) == _COMPACTION_DETAIL_MAX_CHARS

        secret = compaction_failure_detail(
            {"status": {"type": "failed", "error": "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE"}}
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in secret

    def test_rejects_a_placeholder_reason(self):
        """A named reason of "error" is not a reason. KAS's summarization_failed
        frame carried exactly that, which rendered as "Compaction failed: error"
        — no cause on the row and nothing to grep server-side. Falling through
        to the raw shape is strictly more evidence than the word."""
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail({"kind": "summarization_failed", "error": "error"})
        assert detail != "error"
        assert "no reason reported" in detail

    def test_prefers_the_user_facing_sentence_over_the_machine_reason(self):
        """The nested pair is the shape KAS reports a throttle in. The machine
        reason identifies the fault; the sentence is the one that tells the
        reader what to do about it, so the sentence wins."""
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail(
            {
                "kind": "summarization_failed",
                "name": "ModelThrottleError",
                "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE", "httpStatusCode": 500},
                "userFacingSessionErrorMessage": "The model is experiencing high traffic.",
            }
        )
        assert detail == "The model is experiencing high traffic."


class TestKasSummarizationFailureLogging:
    """The KAS branch logs the WHOLE frame so the next failure is debuggable from
    our own logs. That frame carries ``conversationSummary`` — backend-echoed,
    conversation-derived text — so the dump has to be redacted, or a credential a
    user pasted is persisted into gateway.log by the very line added to make the
    failure diagnosable."""

    @staticmethod
    def _bare_handle():
        from kiro_crew.acp.session_handle import AcpSessionHandle

        handle = AcpSessionHandle.__new__(AcpSessionHandle)
        handle._compaction_failed_at = None
        handle.last_compaction_transient = False
        handle._session_id = "sess-test"
        return handle

    def test_the_frame_is_redacted_before_it_reaches_the_log(self, caplog):
        import logging

        handle = self._bare_handle()
        secret = "AKIAIOSFODNN7EXAMPLE"
        update = {
            "sessionUpdate": "session_info_update",
            "_meta": {
                "kiro": {
                    "kind": "summarization_failed",
                    "reason": "MODEL_TEMPORARILY_UNAVAILABLE",
                    "conversationSummary": (
                        f"The user pasted aws_secret_access_key={secret} while debugging."
                    ),
                }
            },
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
            events = handle._handle_kas_session_info(update)

        assert any("KAS summarization failed" in r.getMessage() for r in caplog.records)
        assert secret not in caplog.text
        # The verdict still lands: redacting the log must not cost the classification.
        assert handle.last_compaction_transient is True
        assert events and events[0].text == "failed"


class TestClaudeAutonomousReader:
    """Direct Claude matches the native SDK's autonomous turn boundaries."""

    def test_session_meta_requests_the_raw_cycle_boundaries(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)

        filters = client._claude_session_meta()["claudeCode"]["emitRawSDKMessages"]

        assert {entry["origin"] for entry in filters if entry["type"] == "user"} == {
            "task-notification",
            "peer",
            "coordinator",
            "observer",
            "observer-activity",
        }
        # Every result, origin-less: the end of a between-turn stretch is what
        # flushes its closing prose, and autonomous results still match.
        assert [entry for entry in filters if entry["type"] == "result"] == [{"type": "result"}]
        # The background-task lifecycle feeds the idle sweep.
        assert {entry["subtype"] for entry in filters if entry["type"] == "system"} == {
            "task_started",
            "task_notification",
            "task_updated",
        }
        assert {entry["type"] for entry in filters} == {"user", "assistant", "result", "system"}

    def test_explicit_mcp_profile_excludes_user_setting_source(self, tmp_path):
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        (overlay / "kirocrew.json").write_text(
            '{"name":"kirocrew","mcpServers":{"context7":' '{"command":"context7","args":[]}}}',
            encoding="utf-8",
        )
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            mcp_gateway_claude_servers=["context7"],
            mcp_gateway_overlay=overlay,
        )
        # Seed the permission surface, as the spawn path does before it binds a
        # session. Without it the array is withheld entirely, and a session holding
        # no Crew profile has nothing to exclude the user's sources FOR -- so the
        # unseeded form asserted a state no production caller can reach.
        client._write_claude_local_settings()
        assert client._claude_settings_authored is True

        options = client._claude_session_meta()["claudeCode"]["options"]

        assert options["settingSources"] == ["project", "local"]

    def test_a_wholly_pooled_profile_still_excludes_the_user_registry(self, tmp_path, monkeypatch):
        """The session that was leaking: a real profile, nothing surviving translation.

        When every one of an agent's servers is a pooled broker stub the translation
        comes back empty, yet the session still receives a complete Crew profile as
        stubs. Keying the decision off the translation missed exactly those sessions,
        so they discovered the user's interactive MCP registry on top of the profile
        -- the browser/filesystem/research fleet this option exists to keep out.
        """
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._write_claude_local_settings()
        monkeypatch.setattr(client, "_session_mcp_servers", lambda: [])
        monkeypatch.setattr(client, "_claude_session_mcp_servers", lambda: [])
        monkeypatch.setattr(client, "_pooled_mcp_servers", lambda: [{"name": "kirocrew-core"}])
        client._session_mcp_withheld = False

        assert client._bound_session_mcp_servers()  # a profile does reach the adapter
        options = client._claude_session_meta()["claudeCode"]["options"]
        assert options["settingSources"] == ["project", "local"]

    def test_unavailable_explicit_profile_preserves_user_setting_source(self, tmp_path):
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            mcp_gateway_claude_servers=["context7"],
            mcp_gateway_overlay=tmp_path / "missing",
        )

        assert client._claude_session_meta()["claudeCode"]["options"] == {}

    def test_default_mcp_profile_keeps_existing_setting_sources(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)

        assert client._claude_session_meta()["claudeCode"]["options"] == {}

    @pytest.mark.asyncio
    async def test_explicit_stub_is_not_injected_twice(self, tmp_path, monkeypatch):
        overlay = tmp_path / "overlay"
        overlay.mkdir()
        (overlay / "kirocrew.json").write_text(
            '{"name":"kirocrew","mcpServers":{"context7":' '{"command":"context7","args":[]}}}',
            encoding="utf-8",
        )
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            mcp_gateway_claude_servers=["context7"],
            mcp_gateway_overlay=overlay,
        )
        monkeypatch.setattr(client, "_session_capability_mcp_servers", lambda: [])
        monkeypatch.setattr(client, "_session_mcp_servers", lambda: [])
        monkeypatch.setattr(
            client,
            "_claude_explicit_session_mcp_servers",
            lambda: [{"name": "context7", "command": "context7", "args": [], "env": []}],
        )
        client._session_mcp_withheld = False
        monkeypatch.setattr(
            client,
            "_pooled_mcp_servers",
            lambda: [{"name": "context7", "command": "context7", "args": [], "env": []}],
        )

        assert [entry["name"] for entry in client._bound_session_mcp_servers()] == ["context7"]

    @staticmethod
    def _sdk_message(message: dict) -> "JsonRpcMessage":
        return JsonRpcMessage(
            method=acp_client._CLAUDE_SDK_MESSAGE_METHOD, params={"message": message}
        )

    @staticmethod
    def _text_update(text: str) -> "JsonRpcMessage":
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                }
            },
        )

    @pytest.mark.asyncio
    async def test_autonomous_output_is_delivered_before_the_next_prompt(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._claude_inbox = asyncio.Queue()
        delivered = []

        async def deliver(turn):
            delivered.append(turn)

        await client.set_claude_autonomous_turn_handler(deliver)
        await client._route_claude_frame(
            self._sdk_message(
                {
                    "type": "user",
                    "origin": {"kind": "task-notification"},
                    "timestamp": "2026-08-28T00:00:00Z",
                }
            )
        )
        await client._route_claude_frame(self._text_update("Routine: still working. "))
        await client._route_claude_frame(
            self._sdk_message(
                {
                    "type": "assistant",
                    "timestamp": "2026-08-28T00:00:01Z",
                    "message": {"id": "msg-native-routine"},
                }
            )
        )
        await client._route_claude_frame(
            self._sdk_message({"type": "result", "origin": {"kind": "task-notification"}})
        )

        foreground = JsonRpcMessage(id=44, result={"stopReason": "end_turn"})
        await client._route_claude_frame(foreground)

        assert [(turn.text, turn.origin, turn.message_id) for turn in delivered] == [
            ("Routine: still working. ", "task-notification", "msg-native-routine")
        ]
        assert await client._read_message(timeout=0.01) is foreground

    @pytest.mark.asyncio
    async def test_autonomous_turn_captures_the_slot_that_owned_its_raw_cycle(self, tmp_path):
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            session_key="slot-a",
        )
        delivered = []

        async def deliver(turn):
            delivered.append(turn)

        await client.set_claude_autonomous_turn_handler(deliver)
        await client._route_claude_frame(
            self._sdk_message({"type": "user", "origin": {"kind": "peer"}})
        )
        await client._route_claude_frame(self._text_update("slot A reply"))
        await client._route_claude_frame(
            self._sdk_message({"type": "result", "origin": {"kind": "peer"}})
        )

        assert [turn.session_key for turn in delivered] == ["slot-a"]

    @pytest.mark.asyncio
    async def test_autonomous_permission_is_rejected_without_a_foreground_prompt(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._claude_inbox = asyncio.Queue()
        client._build_permission_event = MagicMock(
            return_value=MagicMock(request_id="permission-1", title="Shell command")
        )
        client.reject_tool = AsyncMock()
        await client._route_claude_frame(
            self._sdk_message({"type": "user", "origin": {"kind": "task-notification"}})
        )

        await client._route_claude_frame(
            JsonRpcMessage(method="session/request_permission", id="permission-1", params={})
        )

        client.reject_tool.assert_awaited_once_with("permission-1")
        assert client._claude_inbox.empty()

    @pytest.mark.asyncio
    async def test_failed_autonomous_delivery_is_queued_for_retry(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        failed = AsyncMock(side_effect=OSError("history offline"))
        turn = acp_client.ClaudeAutonomousTurn(
            text="Routine complete.",
            origin="task-notification",
            timestamp="2026-08-28T00:00:00Z",
            message_id="msg-retry",
        )

        await client.set_claude_autonomous_turn_handler(failed)
        await client._deliver_claude_autonomous_turn(turn)

        assert list(client._pending_claude_autonomous_turns) == [turn]

    @pytest.mark.asyncio
    async def test_rekey_drops_pending_autonomous_output_from_the_prior_slot(self, tmp_path):
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            session_key="old-slot",
        )
        turn = acp_client.ClaudeAutonomousTurn(
            text="Old session routine.",
            origin="task-notification",
            timestamp="2026-08-28T00:00:00Z",
            message_id="msg-old-slot",
            session_key="old-slot",
        )

        await client._deliver_claude_autonomous_turn(turn)
        assert list(client._pending_claude_autonomous_turns) == [turn]

        client.rekey("new-slot")
        delivered = []

        async def deliver(new_turn):
            delivered.append(new_turn)

        await client.set_claude_autonomous_turn_handler(deliver)

        assert delivered == []
        assert list(client._pending_claude_autonomous_turns) == []


class TestSteerLifecycleDispatch:
    """Mid-turn steer frames on the AcpClient path.

    dsh sessions run on AcpClient (``ACP_BACKENDS_ACP_RUNTIME`` excludes
    deepseek), and its adapter reports a steer through the ordinary
    session-update methods. Until this path classified those frames, a
    ``steering_consumed`` was swallowed as a plain update: the slot's ledger
    never settled the steer, teardown requeued it, and the user's message ran a
    second time.
    """

    def _steer_msg(self, discriminant, text="the question", session_id="s1", kiro=False):
        from kiro_crew.acp.types import (
            METHOD_KIRO_SESSION_UPDATE,
            METHOD_SESSION_UPDATE,
            JsonRpcMessage,
        )

        return JsonRpcMessage(
            method=METHOD_KIRO_SESSION_UPDATE if kiro else METHOD_SESSION_UPDATE,
            params={
                "sessionId": session_id,
                "update": {"sessionUpdate": discriminant, "content": text},
            },
        )

    def _complete_msg(self):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(id=1, result={"status": "complete"})

    def test_steer_consumed_classified_before_generic_update(self):
        client = AcpClient()
        assert client._process_message(self._steer_msg("steering_consumed"), req_id=99) == "steer"

    def test_kiro_steer_queued_classified(self):
        client = AcpClient()
        msg = self._steer_msg("steering_queued", kiro=True)
        assert client._process_message(msg, req_id=99) == "steer"

    def test_kas_injected_classified(self):
        client = AcpClient()
        msg = self._steer_msg("AgentExecutionSteeringInjected")
        assert client._process_message(msg, req_id=99) == "steer"

    def test_steer_echo_settles_the_pending_ledger(self):
        """The live defect: an echo frame must settle the slot's ledger.

        Classified as a plain update, the ``steering_consumed`` frame reached
        neither the event kind nor the ledger, teardown requeued a steer the
        backend had already injected, and the user's message ran twice.
        """
        from kiro_crew.dashboard.steer_settle import settle_consumed_steers

        client = AcpClient()
        client._session_id = "s1"
        msg = self._steer_msg(
            "steering_consumed", text="<user_message>\nthe question\n</user_message>"
        )

        assert client._process_message(msg, req_id=99) == "steer"
        events = client._steer_events(msg)
        assert [ev.kind for ev in events] == ["steer_consumed"]
        assert settle_consumed_steers(["the question"], events[0].text) == []

    def test_ordinary_session_update_still_update(self):
        client = AcpClient()
        msg = self._steer_msg("agent_message_chunk")
        assert client._process_message(msg, req_id=99) == "update"

    @pytest.mark.asyncio
    async def test_steer_consumed_reaches_stream_events(self):
        """The echo the ledger settles on must survive the dispatch loop."""
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_STEER_CONSUMED, AcpEvent

        client = AcpClient()
        client._session_id = "s1"
        wrapped = "<user_message>\nthe question\n</user_message>"
        steer_msg = self._steer_msg("steering_consumed", text=wrapped)
        complete_msg = self._complete_msg()

        async def fake_prompt_loop(req_id, timeout):
            yield "steer", steer_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [ev.kind for ev in events] == [EVENT_STEER_CONSUMED, EVENT_COMPLETE]
        assert events[0].text == wrapped

    @pytest.mark.asyncio
    async def test_steer_queued_and_cleared_events_reach_stream_events(self):
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_STEER_CLEARED,
            EVENT_STEER_QUEUED,
            AcpEvent,
        )

        client = AcpClient()
        client._session_id = "s1"

        async def fake_prompt_loop(req_id, timeout):
            yield "steer", self._steer_msg("steering_queued")
            yield "steer", self._steer_msg("steering_cleared")
            yield "complete", self._complete_msg()

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [ev.kind for ev in events] == [
            EVENT_STEER_QUEUED,
            EVENT_STEER_CLEARED,
            EVENT_COMPLETE,
        ]
        assert events[0].text == "the question"

    @pytest.mark.asyncio
    async def test_foreign_session_steer_echo_ignored(self):
        """Only a frame naming this session may settle its steer ledger."""
        from kiro_crew.acp.types import EVENT_COMPLETE, AcpEvent

        client = AcpClient()
        client._session_id = "s1"
        foreign = self._steer_msg("steering_consumed", session_id="other")

        async def fake_prompt_loop(req_id, timeout):
            yield "steer", foreign
            yield "complete", self._complete_msg()

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [ev.kind for ev in events] == [EVENT_COMPLETE]


class TestLocalLaneAdmission503:
    """Local-lane admission responses are retryable, not terminal failures."""

    def test_router_admission_503_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        assert (
            _is_transient_raw_error({"data": 'API Error: 503 {"error":"no available lane"}'})
            is True
        )

    def test_usage_limit_still_beats_admission_wording(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        assert (
            _is_transient_raw_error(
                {"data": "Monthly usage limit has been reached; too many requests"}
            )
            is False
        )


class TestImagePromptCapability:
    """The direct prompt path gates on the capability the harness advertised.

    Ungated it sent an image block to a harness that advertised none, and the
    harness rejected the WHOLE prompt (-32602) instead of the documented
    fallback: leave the path in the text so a tool-capable agent can open it.
    """

    def _client(self, tmp_path, advertised):
        client = acp_client.AcpClient(work_dir=tmp_path)
        client._prompt_capabilities = advertised
        return client

    def test_the_property_fails_closed_before_the_handshake(self, tmp_path):
        client = acp_client.AcpClient(work_dir=tmp_path)
        assert client.supports_image_prompt is False

    def test_initialize_session_captures_the_advertised_capabilities(self):
        import inspect

        source = inspect.getsource(acp_client.AcpClient._initialize_session)
        assert "promptCapabilities" in source
        assert "self._prompt_capabilities" in source

    @pytest.mark.asyncio
    async def test_send_prompt_withholds_images_when_not_advertised(self, tmp_path, monkeypatch):
        seen = {}

        def _fake_build(message, *, allow_image=True, **kwargs):
            seen["allow_image"] = allow_image
            return [{"type": "text", "text": message}]

        monkeypatch.setattr(acp_client, "build_prompt_blocks", _fake_build)
        client = self._client(tmp_path, {"image": False})

        async def _fake_send(method, params, **kwargs):
            seen["params"] = params
            return 1

        monkeypatch.setattr(client, "_send_request", _fake_send)
        await client._send_prompt("look at /tmp/shot.png")
        assert seen["allow_image"] is False
        assert all(block["type"] != "image" for block in seen["params"]["prompt"])

    @pytest.mark.asyncio
    async def test_send_prompt_inlines_images_when_advertised(self, tmp_path, monkeypatch):
        seen = {}

        def _fake_build(message, *, allow_image=True, **kwargs):
            seen["allow_image"] = allow_image
            return [{"type": "text", "text": message}]

        monkeypatch.setattr(acp_client, "build_prompt_blocks", _fake_build)
        client = self._client(tmp_path, {"image": True})

        async def _fake_send(method, params, **kwargs):
            return 1

        monkeypatch.setattr(client, "_send_request", _fake_send)
        await client._send_prompt("look at /tmp/shot.png")
        assert seen["allow_image"] is True

    def test_a_model_switch_makes_the_capability_unknown(self, tmp_path):
        """The answer belongs to the model the session handshook on.

        After a live switch the harness has advertised for a model the session
        no longer runs, and it re-negotiates nothing mid-connection, so the old
        answer is evidence about neither. Unknown is not a refusal.
        """
        client = self._client(tmp_path, {"image": False})
        assert client.supports_image_prompt is False
        client.note_model_changed()
        assert client.supports_image_prompt is None

    @pytest.mark.asyncio
    async def test_an_unknown_capability_tries_the_image(self, tmp_path, monkeypatch):
        seen = {}

        def _fake_build(message, *, allow_image=True, **kwargs):
            seen["allow_image"] = allow_image
            return [{"type": "image"}, {"type": "text", "text": message}]

        monkeypatch.setattr(acp_client, "build_prompt_blocks", _fake_build)
        client = self._client(tmp_path, {"image": False})
        client.note_model_changed()

        async def _fake_send(method, params, **kwargs):
            return 1

        monkeypatch.setattr(client, "_send_request", _fake_send)
        await client._send_prompt("look at /tmp/shot.png")
        assert seen["allow_image"] is True

    @pytest.mark.asyncio
    async def test_an_image_refusal_is_retried_as_text(self, tmp_path, monkeypatch):
        """Recalled, not surfaced: the turn survives and the agent gets the path."""
        calls = []

        def _fake_build(message, *, allow_image=True, **kwargs):
            if allow_image:
                return [{"type": "image"}, {"type": "text", "text": message}]
            return [{"type": "text", "text": message}]

        monkeypatch.setattr(acp_client, "build_prompt_blocks", _fake_build)
        client = self._client(tmp_path, {"image": False})
        client.note_model_changed()

        async def _fake_send(method, params, **kwargs):
            calls.append(params["prompt"])
            if len(calls) == 1:
                raise acp_client.AcpError(
                    "Invalid params: inline image prompts were not advertised" " by this connection"
                )
            return 2

        monkeypatch.setattr(client, "_send_request", _fake_send)
        assert await client._send_prompt("look at /tmp/shot.png") == 2
        assert [block["type"] for block in calls[1]] == ["text"]
        assert client._prompt_capabilities == {"image": False}
        assert client.supports_image_prompt is False

    @pytest.mark.asyncio
    async def test_a_failure_that_is_not_about_the_image_is_not_retried(
        self, tmp_path, monkeypatch
    ):
        calls = []

        def _fake_build(message, *, allow_image=True, **kwargs):
            return [{"type": "text", "text": message}]

        monkeypatch.setattr(acp_client, "build_prompt_blocks", _fake_build)
        client = self._client(tmp_path, {"image": False})
        client.note_model_changed()

        async def _fake_send(method, params, **kwargs):
            calls.append(params)
            raise acp_client.AcpError("transport closed")

        monkeypatch.setattr(client, "_send_request", _fake_send)
        with pytest.raises(acp_client.AcpError):
            await client._send_prompt("hello")
        assert len(calls) == 1
