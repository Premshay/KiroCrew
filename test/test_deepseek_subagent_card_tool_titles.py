"""A Crew-spawned subagent on the DeepSeek Harness keeps each call's name on its card.

dsh names a call in its ``tool_call`` frame (``title: "bash"``) and then asks with a
``session/request_permission`` whose ``toolCall`` carries only ``toolCallId``. The
permission event's title is therefore ``unknown``, and the card row the
``tool_call`` frame drew used to be overwritten with ``unknown``. The card now keeps
the name the call's own ``tool_call`` frame stated. Display only: the approval
ladder still judges the permission event exactly as before.

Replays the live captures ``acp_frames/deepseek/permission-request-live.jsonl`` (one
``bash`` call) and ``side-read-only-live.jsonl`` (six calls) through a real
``AcpProvider`` / ``AcpClient`` into the real run loop.
"""

from __future__ import annotations

import pytest
from acp_frame_replay_harness import CORPUS
from test_claude_subagent_card_tool_titles import _card, _frames, _run

from kiro_crew.agent_sdk.backends import ACP_BACKEND_DEEPSEEK

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

PERMISSION_FIXTURE = CORPUS / "deepseek" / "permission-request-live.jsonl"
SIDE_FIXTURE = CORPUS / "deepseek" / "side-read-only-live.jsonl"


@pytest.mark.asyncio
async def test_the_permission_request_keeps_the_calls_name_on_the_card() -> None:
    frames = _frames(PERMISSION_FIXTURE, ACP_BACKEND_DEEPSEEK)
    # The premise: dsh's ask names only the call id.
    asks = [f for f in frames if f.get("method") == "session/request_permission"]
    assert asks and all(set(f["params"]["toolCall"]) == {"toolCallId"} for f in asks)

    info, fired = await _run(frames, ACP_BACKEND_DEEPSEEK)

    card = _card(fired)
    assert card, "the run fired no subagent_tool frame"
    assert all(tool == "bash" for tool, _call, _update in card), card
    assert "unknown" not in {tool for tool, _call, _update in card}
    assert info.last_tool == "bash"


@pytest.mark.asyncio
async def test_every_call_of_a_multi_call_turn_keeps_its_own_name() -> None:
    frames = _frames(SIDE_FIXTURE, ACP_BACKEND_DEEPSEEK)

    _info, fired = await _run(frames, ACP_BACKEND_DEEPSEEK)

    names = {call: tool for tool, call, _update in _card(fired)}
    assert names == {
        "toolu_side_00": "read",
        "toolu_side_01": "grep",
        "toolu_side_02": "glob",
        "toolu_side_03": "write",
        "toolu_side_04": "bash",
        "toolu_side_05": "mcp__probe__read",
    }
