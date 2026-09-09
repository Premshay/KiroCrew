"""The transcript budget one history consolidation prompt carries.

The unconsolidated tail has no natural ceiling: a session that goes a long time
between passes, or whose consolidation kept failing, accumulates every message
since the marker and renders all of them into a single prompt. Past some length
no provider accepts that prompt, so the span that most needs extracting becomes
the one that can never be extracted.

Bounding the prompt is only half of it. The durable ``last_consolidated`` marker
is what says a message has been through a memory pass, so it has to follow the
PROMPT rather than the snapshot — on the success path and on the abandon path
alike. These tests pin the split, the separator accounting, the oversized-message
case, and that no offset ever advances past what a model actually read.
"""

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew.history import (
    _CONSOLIDATION_MAX_ATTEMPTS,
    _CONSOLIDATION_PROMPT_BUDGET,
    ConversationLog,
    HistoryConsolidator,
    _consolidation_chunk,
    _fmt_message,
)

KEY = "dashboard:chat-bounds"


def _msg(content: str, role: str = "user") -> dict:
    return {"ts": "2026-09-09T12:00:00", "role": role, "content": content, "tools": []}


def _rendered_size(messages: list[dict]) -> int:
    """Exactly what the prompt builder produces for *messages*."""
    return len("\n".join(_fmt_message(m) for m in messages))


def _make_consolidator(log: ConversationLog, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


def _log_with(tmp_path, contents: list[str]) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        for body in contents:
            log.append(KEY, "user", body)
    return log


class TestChunkFitsTheBudget:
    def test_a_tail_within_the_budget_is_returned_whole(self) -> None:
        messages = [_msg(f"m{i}") for i in range(20)]
        assert _consolidation_chunk(messages) == messages

    def test_an_oversized_tail_is_split_at_a_message_boundary(self) -> None:
        # Quarter-budget bodies: four fit, the fifth cannot.
        messages = [_msg("x" * (_CONSOLIDATION_PROMPT_BUDGET // 4)) for _ in range(8)]
        chunk = _consolidation_chunk(messages)

        assert 0 < len(chunk) < len(messages)
        assert chunk == messages[: len(chunk)], "the chunk must be a prefix, in order"
        assert _rendered_size(chunk) <= _CONSOLIDATION_PROMPT_BUDGET

    def test_adding_the_next_message_would_exceed_the_budget(self) -> None:
        """The split is at the LAST message that fits, not an early bail-out."""
        messages = [_msg("x" * (_CONSOLIDATION_PROMPT_BUDGET // 4)) for _ in range(8)]
        chunk = _consolidation_chunk(messages)

        assert _rendered_size(messages[: len(chunk) + 1]) > _CONSOLIDATION_PROMPT_BUDGET

    def test_the_joining_newlines_are_charged(self) -> None:
        """A budget blind to the separators overshoots by one byte per message.

        Sized so the bodies alone land exactly on the budget: the only thing that
        can push the rendered block over is the ``"\\n"`` between them, so a chunk
        that still contains every message proves the separator went uncounted.
        """
        singles = [_msg(f"m{i}") for i in range(64)]
        used = sum(len(_fmt_message(m)) for m in singles)
        envelope = len(_fmt_message(_msg("")))
        slack = _CONSOLIDATION_PROMPT_BUDGET - used - envelope
        assert slack > 0, "fixture must leave room for a filler message"
        messages = singles + [_msg("x" * slack)]
        # Rendered messages alone are exactly at the ceiling; the 64 separators
        # between them are not, so only a separator-blind budget keeps them all.
        assert sum(len(_fmt_message(m)) for m in messages) == _CONSOLIDATION_PROMPT_BUDGET

        chunk = _consolidation_chunk(messages)

        assert len(chunk) < len(messages)
        assert _rendered_size(chunk) <= _CONSOLIDATION_PROMPT_BUDGET


class TestAnOversizedMessageStillMakesProgress:
    def test_a_first_message_over_the_budget_is_prompted_alone(self) -> None:
        """Refusing it would stall the session — and everything behind it — forever.

        Its size is a permanent property of the transcript, so no amount of
        waiting changes the verdict. Sending it is no worse than the unbounded
        prompt the budget replaces, and it terminates through the ordinary
        attempt cap.
        """
        huge = _msg("x" * (_CONSOLIDATION_PROMPT_BUDGET * 3))
        messages = [huge, _msg("small")]

        assert _consolidation_chunk(messages) == [huge]

    def test_the_chunk_is_never_empty(self) -> None:
        """An empty chunk would prompt nothing and mark nothing: a dead pass."""
        for width in (1, _CONSOLIDATION_PROMPT_BUDGET, _CONSOLIDATION_PROMPT_BUDGET * 10):
            assert _consolidation_chunk([_msg("x" * width)])


class TestTheMarkerFollowsThePrompt:
    @pytest.mark.asyncio
    async def test_only_the_prompted_prefix_is_marked_consolidated(self, tmp_path) -> None:
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET // 2)] * 6)
        c = _make_consolidator(log)
        before = log.unconsolidated_count(KEY)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            await c._consolidate(KEY, include_history=True)

        after = log.unconsolidated_count(KEY)
        assert 0 < after < before, "the unprompted tail must survive a bounded pass"

    @pytest.mark.asyncio
    async def test_the_prompt_carries_only_the_messages_that_are_marked(self, tmp_path) -> None:
        """The two must agree, or the marker retires a message no model read."""
        log = _log_with(tmp_path, [f"body-{i}-" + "x" * 40_000 for i in range(6)])
        c = _make_consolidator(log)
        before = log.unconsolidated_count(KEY)
        call = AsyncMock(return_value={"history_entry": "e"})

        with patch.object(c, "_call_llm", call):
            await c._consolidate(KEY, include_history=True)

        prompt = call.await_args.args[0]
        marked = before - log.unconsolidated_count(KEY)
        assert marked > 0
        for i in range(marked):
            assert f"body-{i}-" in prompt, "a marked message was never prompted"
        for i in range(marked, before):
            assert f"body-{i}-" not in prompt, "an unmarked message was prompted"

    @pytest.mark.asyncio
    async def test_successive_passes_drain_the_tail(self, tmp_path) -> None:
        """Bounded passes must reach the end, not stall partway."""
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET // 2)] * 8)
        c = _make_consolidator(log)

        passes = 0
        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            while log.unconsolidated_count(KEY) and passes < 20:
                before = log.unconsolidated_count(KEY)
                await c._consolidate(KEY, include_history=True)
                assert log.unconsolidated_count(KEY) < before, "a pass consolidated nothing"
                passes += 1

        assert log.unconsolidated_count(KEY) == 0
        assert passes > 1, "fixture no longer exercises the split"


class TestAbandonMarksOnlyWhatWasPrompted:
    @pytest.mark.asyncio
    async def test_the_unprompted_tail_survives_an_abandoned_span(self, tmp_path) -> None:
        """The cap retires the prefix that failed, not the tail behind it.

        Marking the whole snapshot would discard messages that were never in any
        prompt — the same silent loss the budget exists to prevent, arriving
        through the failure path instead of the success path.
        """
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET // 2)] * 6)
        c = _make_consolidator(log)
        before = log.unconsolidated_count(KEY)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS - 1,
                    "consolidation_retry_at": 0.0,
                },
            )

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        after = log.unconsolidated_count(KEY)
        assert 0 < after < before, "the abandon marker must cover the prompted prefix only"

    @pytest.mark.asyncio
    async def test_an_abandoned_prefix_lets_the_tail_consolidate(self, tmp_path) -> None:
        """Abandoning is progress, not a dead end: the next pass starts after it."""
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET // 2)] * 6)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY,
                {
                    "consolidation_attempts": _CONSOLIDATION_MAX_ATTEMPTS - 1,
                    "consolidation_retry_at": 0.0,
                },
            )
        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)
        remaining = log.unconsolidated_count(KEY)
        assert remaining

        # The abandon write clears the accounting, so the next span starts with
        # its own budget rather than inheriting the failed one's.
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) < remaining


class TestPrefsPassesKeepTheWholeTail:
    @pytest.mark.asyncio
    async def test_a_prefs_only_pass_prompts_every_unconsolidated_message(self, tmp_path) -> None:
        """Its window is a scheduling artifact, not a durable marker.

        ``maybe_consolidate``'s done-callback advances an in-memory offset to the
        count it scheduled against, with no channel back from the pass. Bounding
        this prompt without also making that offset follow the bound would drop
        the remainder from preference and project extraction outright, so the
        unbounded prompt is the lesser fault until the offset is durable.
        """
        log = _log_with(tmp_path, [f"body-{i}-" + "x" * 40_000 for i in range(6)])
        c = _make_consolidator(log)
        call = AsyncMock(return_value={})

        with patch.object(c, "_call_llm", call):
            await c._consolidate(KEY, include_history=False)

        prompt = call.await_args.args[0]
        for i in range(6):
            assert f"body-{i}-" in prompt
        assert log.unconsolidated_count(KEY) == 6, "a prefs pass must not move the marker"


class TestSpanIdentityIsUnchangedByTheBound:
    @pytest.mark.asyncio
    async def test_the_attempt_stamp_still_describes_the_whole_transcript(self, tmp_path) -> None:
        """The cap holds only while the stamped extent stays put.

        ``total`` is what the retry accounting compares the live transcript
        against; stamping the prompted prefix instead would read as growth on
        every later check and hand a failing span an unlimited supply of billed
        retries.
        """
        log = _log_with(tmp_path, ["x" * (_CONSOLIDATION_PROMPT_BUDGET // 2)] * 6)
        c = _make_consolidator(log)
        total = log.consolidation_counts(KEY)[0]

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        meta = log.get_metadata(KEY)
        assert int(meta["consolidation_attempts_count"]) == total
        assert (
            log.consolidation_retry_state(KEY, total)[0] == 1
        ), "the charge must still be attributed to this span"


class TestTheBudgetIsNotABehaviourChangeForOrdinarySessions:
    @pytest.mark.asyncio
    async def test_a_short_tail_consolidates_in_one_pass(self, tmp_path) -> None:
        log = _log_with(tmp_path, [f"m{i}" for i in range(12)])
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            await c._consolidate(KEY, include_history=True)

        assert log.unconsolidated_count(KEY) == 0
        assert log.consolidation_retry_state(KEY) == (0, 0.0)
        c._last_activity[KEY] = time.time() - 10
        c._tasks.clear()
        c.check_idle_sessions()
        assert not c._tasks
