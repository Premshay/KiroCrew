"""Quota out-marks beyond the spawned run's own ending (fork, operator ruling 2026-11-20).

1. ``spawn_route``'s tombstone scan attributes a run to the seat that SERVED it
   (``runtime_agent``, then provider, then model, then the template seat) through
   the same resolution ``quota_switch`` uses, and reads Antigravity's quota text
   from the run's result tail. Fixture shaped like the real tombstone of run
   521d4974a303c81e: the ``crew-claude`` template on the ``crew-antigravity``
   seat, ``detail`` the generic refusal text, the fault only in ``result.txt``.
2. A dashboard chat turn that failed on its quota writes the same out-mark
   (chat-2069, 11:19 IST, ``Individual quota reached`` on gemini-4-hydrogen-high)
   without switching the chat's model, raising or delaying the error card.
"""

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
    monkeypatch.setattr(sr, "_health_cache", None)
    yield
    vl._reset_for_tests()


def _write_521d4974(root):
    """The run's folder as it stood on 2026-11-20 (task text shortened)."""
    d = root / "521d4974a303c81e"
    d.mkdir(parents=True)
    tomb = {
        "id": "521d4974a303c81e",
        "task": "Read-only inventory. ...",
        "agent": "crew-claude",
        "parent_session": "dashboard:chat-2066-1795095153",
        "started": 1795170779.6387768,
        "died": DIED,
        "cause": "error",
        "recovery_action": "partial_result",
        "result_available": True,
        "result_path": str(d / "result.txt"),
        "provider": "acp",
        "cwd": "/home/prems/dev/repos/atlomy/Atlomy-Atlas",
        "pid": None,
        "turns": 0,
        "last_tool": "",
        "outcome": "failed",
        "elapsed": 5.376590013504028,
        "credits": 0.0,
        "detail": "failed (refusal) — partial result preserved",
        "taskq_attempt": 1,
    }
    state = {
        "id": "521d4974a303c81e",
        "agent": "crew-claude",
        "runtime_agent": "crew-antigravity",
        "requested_model": "gemini-4-hydrogen-medium",
        "resolved_model": "gemini-4-hydrogen-medium",
        "provider": "acp",
    }
    (d / "tombstone.json").write_text(json.dumps(tomb), encoding="utf-8")
    (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
    (d / "result.txt").write_text("x" * 5000 + "\n" + AGY_TAIL, encoding="utf-8")
    return d


class TestTombstoneVendor:
    def test_521d4974_reads_as_antigravity_out_not_claude(self, tmp_path):
        _write_521d4974(tmp_path / "subagents")
        rows = sr.load_tombstones(tmp_path / "subagents")
        assert len(rows) == 1
        row = rows[0]
        assert row["runtime_agent"] == "crew-antigravity"
        assert row["resolved_model"] == "gemini-4-hydrogen-medium"
        assert sr._vendor_of_tombstone(row) == "antigravity"
        hit = sr.limit_hit(row)
        assert hit is not None
        assert hit["vendor"] == "antigravity"
        assert hit["model"] == "gemini-4-hydrogen-medium"
        assert hit["until"] == pytest.approx(DIED + 3600 + 30 * 60 + 18)
        health = sr.vendor_health(rows, now=DIED + 60)
        assert health["antigravity"]["out"] is True
        assert health["claude"]["out"] is False

    def test_runtime_agent_on_the_row_wins_over_the_template_seat(self):
        row = {"agent": "crew-claude", "runtime_agent": "crew-antigravity", "provider": "acp"}
        assert sr._vendor_of_tombstone(row) == "antigravity"

    def test_order_provider_then_model_then_agent(self):
        assert sr._vendor_of_tombstone({"agent": "crew-claude", "provider": "codex"}) == "codex"
        assert (
            sr._vendor_of_tombstone(
                {"agent": "crew-claude", "provider": "acp", "resolved_model": "gpt-5.5"}
            )
            == "codex"
        )
        # Legacy: a base seat on the shared adapter names no vendor and is Claude.
        assert sr._vendor_of_tombstone({"agent": "kirocrew", "provider": "acp"}) == "claude"
        assert sr._vendor_of_tombstone({"agent": "derrick", "provider": "acp"}) == "claude"

    def test_same_resolution_as_quota_switch(self):
        info = SimpleNamespace(
            agent="crew-claude", runtime_agent="crew-antigravity", backend=None
        )
        assert qs.run_vendor(info, "gemini-4-hydrogen-medium") == "antigravity"
        assert sr._vendor_of_tombstone(
            {"agent": "crew-claude", "runtime_agent": "crew-antigravity"}
        ) == qs.run_vendor(info)

    @pytest.mark.parametrize(
        "detail", ["status QUOTA_EXHAUSTED", "429 RESOURCE_EXHAUSTED", AGY_TAIL]
    )
    def test_antigravity_quota_text_in_detail(self, detail):
        row = {"agent": "crew-antigravity", "died": DIED, "detail": detail}
        hit = sr.limit_hit(row)
        assert hit is not None and hit["vendor"] == "antigravity"

    def test_another_vendors_quota_text_in_a_claude_tail_is_not_a_hit(self):
        row = {
            "agent": "crew-claude",
            "provider": "acp",
            "died": DIED,
            "detail": "failed (refusal)",
            "result_tail": "the report mentions Individual quota reached once",
        }
        assert sr.limit_hit(row) is None

    def test_delivered_rows_are_not_enriched(self, tmp_path):
        d = _write_521d4974(tmp_path / "subagents")
        tomb = json.loads((d / "tombstone.json").read_text())
        tomb.update(outcome=None, cause="delivered")
        (d / "tombstone.json").write_text(json.dumps(tomb))
        (row,) = sr.load_tombstones(tmp_path / "subagents")
        assert "result_tail" not in row and "runtime_agent" not in row

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
        assert sr._vendor_of_tombstone(tomb) == "antigravity"


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
        monkeypatch.setattr(
            vl, "claude_health", lambda now=None: {"out": True, "resets_at": 9e12}
        )
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
