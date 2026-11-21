"""Quota marks from dashboard chat failures and serving-seat provenance."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import vendor_limits as vl
from kiro_crew.dashboard import chat_quota_out as qom
from kiro_crew.decisions.points import spawn_route as sr
from kiro_crew.subagent_manager import quota_switch as qs

DIED = 1795170785.0154736
AGY_TAIL = (
    "Antigravity provider fault: status ERROR: Individual quota reached. Please upgrade "
    "your subscription to increase your limits. Resets in 1h30m18s.."
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "_data_home", lambda: tmp_path)
    vl._reset_for_tests()
    monkeypatch.setattr(sr, "_limits_cache", None)
    yield
    vl._reset_for_tests()


class TestTombstoneVendor:
    def test_tombstone_writer_records_the_serving_seat(self, tmp_path, monkeypatch):
        from kiro_crew import subagent_persistence as sp

        monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
        (tmp_path / "abc123").mkdir()
        (tmp_path / "abc123" / "state.json").write_text(
            json.dumps(
                {
                    "id": "abc123",
                    "task": "t",
                    "agent": "crew-claude",
                    "runtime_agent": "crew-antigravity",
                    "resolved_model": "gemini-4-hydrogen-medium",
                    "started": DIED - 5,
                }
            )
        )
        sp.write_tombstone("abc123", cause="error", recovery_action="partial_result")
        tomb = json.loads((tmp_path / "abc123" / "tombstone.json").read_text())
        assert tomb["runtime_agent"] == "crew-antigravity"
        assert tomb["resolved_model"] == "gemini-4-hydrogen-medium"


# ---------------------------------------------------------------------------
# Chat turns
# ---------------------------------------------------------------------------


def _turn(**kw):
    base = dict(
        kiro_agent="crew-antigravity",
        owner_agent="crew-antigravity",
        provider_name="acp",
        _turn_model="gemini-4-hydrogen-high",
        client=None,
        assistant_text="",
        _turn_refusal=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestChatOutMark:
    @pytest.mark.asyncio
    async def test_quota_error_marks_the_model_group_out(self):
        fut = qom.mark_quota_out(_turn(), RuntimeError(AGY_TAIL))
        assert fut is not None
        hit = await fut
        assert hit is not None and hit.vendor == "antigravity"
        mark = vl.quota_out("antigravity", "gemini-4-hydrogen-high")
        assert mark is not None
        assert "Individual quota reached" in mark["detail"]

    @pytest.mark.asyncio
    async def test_streamed_refusal_text_is_read(self):
        turn = _turn(assistant_text="some answer\n" + AGY_TAIL)
        fut = qom.mark_quota_out(turn, refusal=True)
        assert fut is not None and (await fut) is not None
        assert vl.quota_out("antigravity", "gemini-4-hydrogen-high") is not None

    @pytest.mark.asyncio
    async def test_non_quota_error_schedules_nothing(self):
        assert qom.mark_quota_out(_turn(), RuntimeError("connection reset")) is None
        assert vl.quota_outs() == {}

    @pytest.mark.asyncio
    async def test_claude_chat_with_rejected_health_is_not_marked_without_text(self, monkeypatch):
        monkeypatch.setattr(vl, "claude_health", lambda now=None: {"out": True, "resets_at": 9e12})
        hit = qs.note_chat_quota("boom", runtime_agent="crew-claude", model="sonnet")
        assert hit is None
        assert vl.quota_outs() == {}

    @pytest.mark.asyncio
    async def test_never_raises(self, monkeypatch):
        class Bad:
            def __str__(self):
                raise ValueError("no")

        assert qom.mark_quota_out(_turn(), Bad()) is None

        def explode(*a, **k):
            raise OSError("disk")

        monkeypatch.setattr(vl, "note_quota_out", explode)
        fut = qom.mark_quota_out(_turn(), AGY_TAIL)
        assert fut is not None and (await fut) is None

    @pytest.mark.asyncio
    async def test_does_not_wait_for_the_mark(self, monkeypatch):
        gate = threading.Event()

        def slow(text, **kw):
            gate.wait(5)
            return None

        monkeypatch.setattr(qs, "note_chat_quota", slow)
        fut = qom.mark_quota_out(_turn(), AGY_TAIL)
        assert fut is not None and not fut.done()
        gate.set()
        await fut


class TestChatSeams:
    """The real ``_run_chat`` path: both refusal branches and the generic error arm."""

    @staticmethod
    def _state(tmp_path, monkeypatch):
        from chat_test_helpers import _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        return state

    @staticmethod
    def _client(stream):
        from dashboard_chat_helpers import _provider_mock

        client = _provider_mock()
        client.context_usage_pct = MagicMock(return_value=10.0)
        client.served_model = "gemini-4-hydrogen-high"
        client.stream = stream
        client.stream_command = stream
        return client

    @staticmethod
    def _record(monkeypatch):
        calls: list[dict] = []

        def rec(text, **kw):
            calls.append({"text": text, **kw})
            return None

        monkeypatch.setattr(qs, "note_chat_quota", rec)
        return calls

    async def _drain(self):
        for _ in range(50):
            if not qom._PENDING:
                return
            await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_streamed_quota_refusal(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_REFUSAL
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        async def stream(msg):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=AGY_TAIL)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL)

        calls = self._record(monkeypatch)
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        client = self._client(stream)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        await _run_chat(state, slot, "hello")
        await self._drain()
        assert len(calls) == 1
        assert "Individual quota reached" in calls[0]["text"]
        assert calls[0]["model"] == "gemini-4-hydrogen-high"
        # The chat's model was not switched and the error card still shows.
        client.set_model.assert_not_called()
        assert any(m.get("role") == "error" for m in slot.messages)

    @pytest.mark.asyncio
    async def test_bare_refusal_with_quota_explanation(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_REFUSAL, RefusalInfo
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def stream(msg):
            yield LLMEvent(
                kind=EVENT_COMPLETE,
                stop_reason=STOP_REASON_REFUSAL,
                refusal=RefusalInfo(explanation="status RESOURCE_EXHAUSTED"),
            )

        calls = self._record(monkeypatch)
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_or_create = AsyncMock(return_value=(self._client(stream), True, False))
        await _run_chat(state, slot, "hello")
        await self._drain()
        assert len(calls) == 1 and "RESOURCE_EXHAUSTED" in calls[0]["text"]

    @pytest.mark.asyncio
    async def test_raised_quota_error(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _run_chat

        async def stream(msg):
            raise RuntimeError(AGY_TAIL)
            yield  # pragma: no cover

        calls = self._record(monkeypatch)
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_or_create = AsyncMock(return_value=(self._client(stream), True, False))
        state.sessions.record_failure = AsyncMock()
        await _run_chat(state, slot, "hello")
        await self._drain()
        assert len(calls) == 1 and "Individual quota reached" in calls[0]["text"]
        assert any(m.get("role") == "error" for m in slot.messages)

    @pytest.mark.asyncio
    async def test_plain_refusal_marks_nothing(self, tmp_path, monkeypatch):
        from kiro_crew.acp.types import STOP_REASON_REFUSAL
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

        async def stream(msg):
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL)

        calls = self._record(monkeypatch)
        state = self._state(tmp_path, monkeypatch)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_or_create = AsyncMock(return_value=(self._client(stream), True, False))
        await _run_chat(state, slot, "hello")
        await self._drain()
        assert calls == []
