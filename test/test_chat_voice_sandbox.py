"""Voice endpoint sandbox refusal and user-visible recovery messages."""

from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from chat_voice_test_helpers import _make_voice_app

from kiro_crew.slack.handler import _VoiceConfig


class TestSandboxRefusalReachesTheUser:
    """A fail-closed sandbox must reach the CHAT SURFACE, not only the log.

    The remedy prose lives in ``SandboxUnavailableError`` and nowhere else. A
    provider that collapses it into a generic "no audio" leaves the one fact that
    would fix the host — that the sandbox refused, and what to do about it —
    discoverable only by reading the gateway log.
    """

    _PROSE = "SANDBOX-REMEDY-SENTINEL: set agent.sandbox_allow_unsandboxed_exec=true"

    @pytest.mark.asyncio
    async def test_system_refusal_is_relayed_with_the_sandbox_prose(self, tmp_path, monkeypatch):
        """The non-streaming branch reports the refusal, not a config guess.

        A config-shaped message for a sandbox refusal sends the operator to check
        settings that are already correct.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = _VoiceConfig(
            provider="system",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        async def refuse(*a, **kw):
            raise SandboxUnavailableError(self._PROSE, "no_backend", "not Linux")

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.synthesize_speech", refuse)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.notify = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post(
                "/api/voice/synthesize", json={"text": "hi", "slot": "s1", "request_id": "rq1"}
            )
            assert resp.status == 502
            body = await resp.json()
            # Auto-speak sends one request per sentence, and a caller picks its own
            # slot. A DIFFERENT slot must still not raise a second notification:
            # the refusal is a host-level property, so the throttle keys on the
            # sandbox kind alone and request data never enters a long-lived key.
            await client.post("/api/voice/synthesize", json={"text": "again", "slot": "s2"})
        assert self._PROSE in body["error"], "the sandbox's own remedy must be relayed"
        assert "espeak-ng" not in body["error"], "must not blame the engine install"
        # A localized UI cannot translate the relayed prose, so the refusal also
        # carries a machine-readable id -- and it names the KIND, since that is
        # what decides which remedy the prose describes.
        assert body["code"] == "sandbox_no_backend"
        # The refusal reaches a surface the user actually sees. Neither the 502 nor
        # the voice_error broadcast does: both auto-speak call sites discard the
        # rejected request, and no dashboard code consumes "voice_error".
        assert state.notify.call_count == 1, "one notification per sandbox kind, not per slot"
        assert self._PROSE in state.notify.call_args[0][2], "the remedy must survive"
        # The dashboard learns about it too, not just the HTTP caller.
        errors = [c for c in state.broadcast_ws.call_args_list if c[0][0] == "voice_error"]
        assert errors, "a voice_error must be broadcast"
        payload = errors[0][0][1]
        assert self._PROSE in payload["error"]
        # The dashboard's voice_error handler drops any event without a
        # request_id and keys its generic failure off `code`, so a broadcast
        # missing either is discarded before it reaches that surface at all.
        assert payload["request_id"] == "rq1"
        assert payload["code"] == "sandbox_no_backend"

    @pytest.mark.asyncio
    async def test_polly_refusal_is_relayed_with_the_sandbox_prose(self, tmp_path, monkeypatch):
        """The Polly (streaming) branch relays it too.

        Covered explicitly because the two providers take DIFFERENT endpoint
        branches -- the system engine is one local WAV, Polly is sentence-chunked -- so fixing
        only the branch named in the report would have left the other silent.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = _VoiceConfig(
            provider="polly",
            default_voice="Joanna",
            default_engine="neural",
            default_rate="100%",
            default_pitch="0%",
            aws_profile="",
            region="us-east-1",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        async def refuse(*a, **kw):
            raise SandboxUnavailableError(self._PROSE, "no_backend", "not Linux")
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.streaming_voice_reply", refuse)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.notify = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "hi", "slot": "s1"})
            # Identical to the system branch, not merely "an error": Polly reaches
            # the sandbox through the streaming path, and falling to the generic
            # handler there would report 500 with no notification -- the remedy
            # invisible on a Polly host, which is the defect being fixed.
            assert resp.status == 502
            body = await resp.json()
        assert self._PROSE in body["error"], "the sandbox's own remedy must be relayed"
        assert body["code"] == "sandbox_no_backend"
        assert state.notify.call_count == 1, "the user-visible surface must fire here too"
        assert self._PROSE in state.notify.call_args[0][2]

    @pytest.mark.asyncio
    async def test_a_transient_refusal_never_gains_the_opt_in_advice(self, tmp_path, monkeypatch):
        """The endpoint must add no remedy of its own.

        For ``transient`` the sandbox says retry and never to disable isolation, so the
        ``allow_unsandboxed_exec`` key must be absent unless the sandbox supplied it.
        """
        from kiro_crew.sandbox import SandboxUnavailableError

        transient = "This looks TRANSIENT (momentary resource pressure). Do NOT disable; retry."
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = _VoiceConfig(
            provider="system",
        )
        monkeypatch.setattr("kiro_crew.dashboard.chat_voice._vc", mock_vc)

        async def refuse(*a, **kw):
            raise SandboxUnavailableError(transient, "transient", "fork: EAGAIN")

        monkeypatch.setattr("kiro_crew.dashboard.chat_voice.synthesize_speech", refuse)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        # Mocked like the sibling refusal tests: the real notification bus hands the
        # write to an executor, and a backlogged worker can land it after pytest has
        # removed `tmp_path` -- recreating the directory as residue.
        state.notify = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "hi", "slot": "s1"})
            body = await resp.json()
        assert transient in body["error"]
        assert "sandbox_allow_unsandboxed_exec" not in body["error"]
        # The code tracks the kind, so a client can tell "retry shortly" apart
        # from "this host will never synthesize" without parsing the prose.
        assert body["code"] == "sandbox_transient"
