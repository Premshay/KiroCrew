"""Mid-turn steering against claude-agent-acp's ``_session/steering``.

Every test drives the real ``AcpClient`` with a fake stdin at the process
boundary — the wire bytes it writes and the frames it is handed back are the
subject, because both defects this path can have are wire-level: a steer sent in
the wrong shape is dropped while the dashboard reports it delivered, and a steer
whose response is not recognised is requeued and delivered twice.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    EVENT_STEER_CONSUMED,
    METHOD_CLAUDE_STEER,
    METHOD_STEER,
    JsonRpcMessage,
)
from kiro_crew.dashboard.steer_settle import settle_consumed_steers


def _client(tmp_path: Path, *, backend: str = ACP_BACKEND_CLAUDE, steerable: bool = True):
    """A client with a live fake stdin, past the handshake."""
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._session_id = "sess-1"
    client._steering_advertised = steerable
    return client


def _written(client) -> list[dict]:
    """Every JSON-RPC frame the client wrote to stdin, in order."""
    return [
        json.loads(call.args[0].decode()) for call in client._process.stdin.write.call_args_list
    ]


class TestCapability:
    def test_handshake_advertisement_grants_the_capability(self, tmp_path):
        client = _client(tmp_path, steerable=False)
        assert client.supports_steer is False
        client._steering_advertised = True
        assert client.supports_steer is True

    def test_kiro_does_not_need_an_advertisement(self, tmp_path):
        # kiro-cli announces nothing at initialize, so gating it on the flag
        # would silently disable a capability it has always had.
        client = _client(tmp_path, backend="", steerable=False)
        assert client.supports_steer is True

    @pytest.mark.asyncio
    async def test_unadvertised_claude_refuses_to_steer(self, tmp_path):
        """The load-bearing case: an older claude-agent-acp on PATH.

        It answers to the same backend name and has no `_session/steering`
        handler. Writing the request anyway returns True to the dashboard, which
        renders the message as steered while the backend drops it.
        """
        client = _client(tmp_path, steerable=False)
        assert await client.steer("check the other branch too") is False
        assert _written(client) == []


class TestWireFormat:
    @pytest.mark.asyncio
    async def test_claude_gets_content_blocks_and_a_host_owned_idle(self, tmp_path):
        client = _client(tmp_path)
        assert await client.steer("  also check the migration  ") is True

        (frame,) = _written(client)
        assert frame["method"] == METHOD_CLAUDE_STEER
        assert frame["params"]["sessionId"] == "sess-1"
        assert frame["params"]["prompt"] == [{"type": "text", "text": "also check the migration"}]
        # promptRequired keeps the no-running-turn fallback with KiroCrew, whose
        # requeue produces a cancellable card. The backend default
        # (startedNewTurn) would run the text in a turn the slot knows nothing
        # about while the pending entry stayed registered — delivered twice.
        assert frame["params"]["_meta"]["steering"]["idleBehavior"] == "promptRequired"

    @pytest.mark.asyncio
    async def test_claude_text_carries_no_kiro_envelope(self, tmp_path):
        """`<user_message>` is kiro-cli's echo envelope, not content.

        claude-agent-acp delivers the blocks as a real user message, so the tags
        would reach the model as literal markup inside the user's own words.
        """
        client = _client(tmp_path)
        await client.steer("stop and summarise")
        (frame,) = _written(client)
        assert "<user_message>" not in json.dumps(frame["params"]["prompt"])

    @pytest.mark.asyncio
    async def test_kiro_wire_format_is_unchanged(self, tmp_path):
        client = _client(tmp_path, backend="", steerable=False)
        assert await client.steer("look at the logs") is True
        (frame,) = _written(client)
        assert frame["method"] == METHOD_STEER
        assert frame["params"] == {
            "sessionId": "sess-1",
            "message": "<user_message>\nlook at the logs\n</user_message>",
        }


class TestSettlement:
    @pytest.mark.asyncio
    async def test_response_is_classified_not_skipped(self, tmp_path):
        """The steer response is a FOREIGN-id response during someone's turn.

        Left unclaimed it falls to "skip" and is dropped, which is exactly the
        double-delivery bug: nothing settles the pending steer, so the turn
        teardown requeues text the backend already injected.
        """
        client = _client(tmp_path)
        await client.steer("narrow it to the v2 branch")
        (frame,) = _written(client)
        response = JsonRpcMessage(id=frame["id"], result={"outcome": "injected"})

        assert client._process_message(response, req_id=frame["id"] + 999) == "steer_result"

    @pytest.mark.asyncio
    async def test_injected_settles_the_pending_steer_end_to_end(self, tmp_path):
        """The synthesized echo must satisfy the parser kiro-cli's echo feeds.

        Settlement is shared code matching `<user_message>` blocks by equality;
        an echo the parser cannot match leaves the steer pending and requeues it.
        """
        client = _client(tmp_path)
        message = "narrow it to the v2 branch"
        await client.steer(message)
        (frame,) = _written(client)

        event = client._settle_steer_response(
            JsonRpcMessage(id=frame["id"], result={"outcome": "injected"})
        )
        assert event is not None
        assert event.kind == EVENT_STEER_CONSUMED
        assert settle_consumed_steers([message], event.text) == []

    @pytest.mark.asyncio
    async def test_prompt_required_leaves_the_steer_pending(self, tmp_path):
        """No running turn: the text did NOT run, so it must reach the requeue."""
        client = _client(tmp_path)
        await client.steer("never mind")
        (frame,) = _written(client)

        event = client._settle_steer_response(
            JsonRpcMessage(
                id=frame["id"],
                result={"outcome": "promptRequired", "reason": "noRunningTurn"},
            )
        )
        assert event is None

    @pytest.mark.asyncio
    async def test_error_response_leaves_the_steer_pending(self, tmp_path):
        client = _client(tmp_path)
        await client.steer("try the other file")
        (frame,) = _written(client)

        event = client._settle_steer_response(
            JsonRpcMessage(id=frame["id"], error={"code": -32601, "message": "Method not found"})
        )
        assert event is None

    @pytest.mark.asyncio
    async def test_unknown_outcome_leaves_the_steer_pending(self, tmp_path):
        """An outcome a later version adds must not be read as delivery.

        Failing toward the requeue costs a visible, cancellable duplicate card.
        Failing the other way marks the message consumed and asks it nowhere.
        """
        client = _client(tmp_path)
        await client.steer("hold on")
        (frame,) = _written(client)

        assert (
            client._settle_steer_response(
                JsonRpcMessage(id=frame["id"], result={"outcome": "deferredToNextTurn"})
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_started_new_turn_settles_because_the_text_ran(self, tmp_path):
        """A backend that ignored our idleBehavior still executed the message.

        Requeuing on the grounds that the outcome was not the one requested runs
        it a second time, which is the failure this whole path exists to avoid.
        """
        client = _client(tmp_path)
        await client.steer("switch to the other approach")
        (frame,) = _written(client)

        event = client._settle_steer_response(
            JsonRpcMessage(id=frame["id"], result={"outcome": "startedNewTurn"})
        )
        assert event is not None
        assert event.kind == EVENT_STEER_CONSUMED

    @pytest.mark.asyncio
    async def test_response_is_consumed_once(self, tmp_path):
        """A duplicate/replayed frame must not settle a later identical steer."""
        client = _client(tmp_path)
        await client.steer("same text")
        (frame,) = _written(client)
        payload = JsonRpcMessage(id=frame["id"], result={"outcome": "injected"})

        assert client._settle_steer_response(payload) is not None
        assert client._settle_steer_response(payload) is None
        assert client._pending_steer_requests == {}


class TestNoDoubleDelivery:
    """The whole point: an injected steer must not also be requeued.

    Settlement and requeue are the two halves of one decision — ``_run_chat``'s
    finally requeues whatever is still in ``slot._pending_steers``. So a Claude
    steer that the client injects but never settles is delivered TWICE: once
    into the running turn, once from the queue card the teardown creates.
    """

    @pytest.mark.asyncio
    async def test_the_settled_steer_reaches_the_runner_and_empties_the_ledger(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import (
            _requeue_unconsumed_steers,
            _settle_consumed_steers,
        )

        client = _client(tmp_path)
        message = "actually target the v2 base"
        await client.steer(message)
        (frame,) = _written(client)

        # The slot ledger the dashboard registered the steer in, before the RPC.
        slot = MagicMock()
        slot.key = "test"
        slot._pending_steers = [message]

        event = client._settle_steer_response(
            JsonRpcMessage(id=frame["id"], result={"outcome": "injected"})
        )
        _settle_consumed_steers(slot, event.text)

        assert slot._pending_steers == []
        # With the ledger empty the teardown is a no-op, so nothing is requeued
        # and the text is delivered exactly once.
        _requeue_unconsumed_steers(MagicMock(), slot)
        assert slot._pending_steers == []

    @pytest.mark.asyncio
    async def test_an_unsettled_steer_is_still_requeued(self, tmp_path):
        """The other direction: a steer the backend declined must NOT be lost."""
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        client = _client(tmp_path)
        message = "never mind, stop"
        await client.steer(message)
        (frame,) = _written(client)

        slot = MagicMock()
        slot.key = "test"
        slot._pending_steers = [message]

        event = client._settle_steer_response(
            JsonRpcMessage(
                id=frame["id"], result={"outcome": "promptRequired", "reason": "noRunningTurn"}
            )
        )
        assert event is None
        # No echo means nothing settles, so the entry survives for the requeue.
        assert slot._pending_steers == [message]
        _settle_consumed_steers(slot, "")
