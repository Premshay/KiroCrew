from __future__ import annotations

import asyncio
import builtins
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import kiro_crew.dashboard.handlers.usage as usage
import kiro_crew.llm_helpers_parts.turn_stream as collector
from kiro_crew.agent_sdk.drivers.acp_turn_vocab import TurnUsage
from kiro_crew.llm_helpers import TURN_FALLBACK_ATTR, TurnNotCompleted
from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent
from kiro_crew.workflows.agent_exec import build_agent_fn
from kiro_crew.workflows.agent_pool import build_pooled_agent_fn


class Provider:
    def __init__(self):
        self.last_prompt_stats = SimpleNamespace(credits=7)
        self._kc_turn_billed = (self.last_prompt_stats, TurnUsage(credits=7))
        setattr(self, TURN_FALLBACK_ATTR, ("primary", "fallback"))

    async def stream(self, message):
        self.last_prompt_stats = SimpleNamespace(credits=3)
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="cancelled")

    def set_delete_delay(self, seconds):
        pass

    def is_process_alive(self):
        return True


class Sessions:
    def __init__(self):
        self.provider = Provider()
        self.released = []
        self.destroyed = []

    async def get_or_create(self, key, **kwargs):
        return self.provider, False, False

    def release(self, key, **kwargs):
        self.released.append(key)

    async def destroy(self, key):
        self.destroyed.append(key)


@pytest.fixture
def writer(monkeypatch):
    writer = AsyncMock()
    monkeypatch.setattr(usage, "persist_token_record_async", writer)
    return writer


@pytest.fixture
def executors(monkeypatch):
    async def vet(**kwargs):
        return kwargs.get("cwd")

    monkeypatch.setattr("kiro_crew.workflows.agent_exec.vet_step_spawn", vet)
    monkeypatch.setattr("kiro_crew.workflows.agent_pool.vet_step_spawn", vet)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cold", "pooled", "unpooled"])
@pytest.mark.parametrize("accumulated", [False, True])
async def test_cancel_before_collector_attempt_does_not_bill_prior_turn(
    monkeypatch, writer, executors, path, accumulated
):
    sessions = Sessions()
    if not accumulated:
        del sessions.provider._kc_turn_billed

    async def cancelled_probe(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(collector, "probe_fallback_restore", cancelled_probe)
    if path == "cold":
        fn, pool, opts = build_agent_fn(sessions, run_id="fresh"), None, {}
        prefix = "wf:"
    else:
        fn, pool = build_pooled_agent_fn(sessions, run_id="fresh")
        opts = {"max_tool_calls": 1} if path == "unpooled" else {}
        prefix = "wf-unpooled:" if path == "unpooled" else "wf-pool:"
    try:
        with pytest.raises(asyncio.CancelledError):
            await fn("test", opts)
    finally:
        if pool:
            await pool.shutdown()
    writer.assert_awaited_once()
    assert writer.await_args.args[0].startswith(prefix)
    assert writer.await_args.args[2].credits == 0


@pytest.mark.asyncio
async def test_usage_import_failure_does_not_mask_incomplete(monkeypatch, executors, caplog):
    sessions = Sessions()
    setattr(sessions.provider, TURN_FALLBACK_ATTR, None)
    original = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "kiro_crew.dashboard.handlers.usage":
            raise RuntimeError("broken usage import")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    fn = build_agent_fn(sessions, run_id="fresh")
    with pytest.raises(TurnNotCompleted):
        await fn("test", {})
    assert sessions.released and sessions.destroyed
    assert "workflow usage row skipped" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["cold", "pooled", "unpooled"])
@pytest.mark.parametrize("attempts", [1, 2])
async def test_fresh_incomplete_usage_includes_all_attempts(
    monkeypatch, writer, executors, path, attempts
):
    sessions = Sessions()
    setattr(sessions.provider, TURN_FALLBACK_ATTR, None)
    count = 0

    async def stream(message):
        nonlocal count
        count += 1
        sessions.provider.last_prompt_stats = SimpleNamespace(credits=count + 1)
        reason = "retry" if count < attempts else "cancelled"
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=reason)

    sessions.provider.stream = stream
    monkeypatch.setattr(
        collector,
        "timed_out_continuation",
        lambda event: "continue" if event.stop_reason == "retry" else "",
    )
    if path == "cold":
        fn, pool, opts = build_agent_fn(sessions, run_id="fresh"), None, {}
        prefix = "wf:"
    else:
        fn, pool = build_pooled_agent_fn(sessions, run_id="fresh")
        opts = {"max_tool_calls": 1} if path == "unpooled" else {}
        prefix = "wf-unpooled:" if path == "unpooled" else "wf-pool:"
    try:
        with pytest.raises(TurnNotCompleted):
            await fn("test", opts)
    finally:
        if pool:
            await pool.shutdown()
    writer.assert_awaited_once()
    assert writer.await_args.args[0].startswith(prefix)
    assert writer.await_args.args[2].credits == (2 if attempts == 1 else 5)
    assert sessions.destroyed
