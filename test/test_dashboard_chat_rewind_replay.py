"""The first turn after an Edit + Send rewind carries the retained history.

A rewind discards the native conversation and rewrites the transcript to the
prefix before the edited message, then runs the edit on a fresh session. That
fresh session holds nothing, so the retained prefix must reach it as the
session replay; without it the agent answers the edit with no memory of the
conversation it was editing.

Each test drives the REAL runner with a mocked provider, a real
``ConversationLog`` seeded with the retained prefix, and the slot flagged the
way ``chat_rewind`` leaves it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader

SLOT_NAME = "rewind-replay"
RETAINED_USER = "Please remember the codeword is heliotrope."
RETAINED_REPLY = "Noted: the codeword is heliotrope."
EDITED = "What was the codeword?"


def _rewound_state(tmp_path, monkeypatch, *, with_builder: bool):
    builder = None
    if with_builder:
        builder = ContextBuilder(
            memory=MemoryStore(workspace=tmp_path / "workspace"),
            skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        )
    state = _make_state(tmp_path, context_builder=builder)
    if with_builder:
        state.context_builder.build_message = MagicMock(return_value=("task", None))
        state.context_builder.ensure_store = AsyncMock(return_value=object())
    else:
        state.context_builder = None
    prompts: list[str] = []
    provider = MagicMock()

    async def stream(prompt, *args, **kwargs):
        prompts.append(prompt)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="heliotrope")
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = stream
    # A fresh native session: the rewind discarded the old one.
    state.sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())

    slot = state.get_or_create_slot(SLOT_NAME)
    key = slot_history_key(slot)
    state.conversation_log.append(key, "user", RETAINED_USER)
    state.conversation_log.append(key, "assistant", RETAINED_REPLY)
    slot.append("user", RETAINED_USER)
    slot.append("assistant", RETAINED_REPLY)
    slot.append("user", EDITED)
    slot._rewind_context_once = True
    return state, slot, prompts


@pytest.mark.asyncio
async def test_rewound_turn_replays_the_retained_prefix(tmp_path, monkeypatch):
    state, slot, _prompts = _rewound_state(tmp_path, monkeypatch, with_builder=True)

    await asyncio.wait_for(chat_runner._run_chat(state, slot, EDITED), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)

    state.context_builder.build_message.assert_called()
    kwargs = state.context_builder.build_message.call_args.kwargs
    replay = kwargs["compressed_history"] or ""
    assert RETAINED_USER in replay
    assert RETAINED_REPLY in replay
    # The edited message is this turn's prompt, never part of its own history.
    assert EDITED not in replay
    # The persisted-history section stays off for the rewound turn; the replay
    # above is the one channel the retained prefix travels through.
    assert kwargs["include_session_history"] is False
    assert slot._rewind_context_once is False


@pytest.mark.asyncio
async def test_rewound_turn_without_a_builder_prepends_the_retained_prefix(tmp_path, monkeypatch):
    state, slot, prompts = _rewound_state(tmp_path, monkeypatch, with_builder=False)

    await asyncio.wait_for(chat_runner._run_chat(state, slot, EDITED), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)

    assert prompts, "the turn never reached the provider"
    assert RETAINED_USER in prompts[0]
    assert RETAINED_REPLY in prompts[0]
