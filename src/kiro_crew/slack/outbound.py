"""The lifecycle of a posted Slack OPTIONS control.

Rendering text for Slack is NOT this module's job. ``slack.format`` owns that —
``render_for_slack`` for bodies and ``build_options_blocks`` for the control,
which redacts every choice through ``redact_for_display`` so a key split by ANSI,
emphasis, backticks or link markup is caught in the form Slack actually shows.
This module deliberately holds no second copy of that pipeline: an earlier
version did, and the two drifted apart until the same credential-exposure bug
had to be fixed twice, three review rounds apart.

What is left here is the part ``slack.format`` has no opinion about: a posted
control stays answerable until something spends it. ``PostedOptions`` carries
enough to find the control again, and ``expire_options`` renders it spent once
the conversation has moved past the question it asked.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kiro_crew.slack.format import build_options_selected_blocks, replace_options_blocks

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

#: Notification/fallback text for the message carrying an OPTIONS control.
OPTIONS_FALLBACK_TEXT = "Options"


@dataclass(frozen=True)
class PostedOptions:
    """A posted OPTIONS control, addressed well enough to expire it later.

    ``blocks`` is the block list exactly as posted. Keeping it means expiry can
    run the same block surgery the Send button uses, editing only the OPTIONS
    block and leaving any surrounding blocks (a timing footer, a
    Link-to-Dashboard button) intact — without re-fetching the message.
    """

    channel: str
    ts: str
    choices: tuple[str, ...]
    blocks: tuple[dict, ...]
    text: str = OPTIONS_FALLBACK_TEXT


async def expire_options(slack: SlackClientOps, posted: PostedOptions) -> None:
    """Render a previously-posted OPTIONS control as spent. Best-effort.

    Strikes every choice through, so a control the conversation has moved past
    reads as unanswerable rather than inviting a click that would answer a
    superseded question. Only the OPTIONS block is replaced; surrounding blocks
    survive.

    Every failure is swallowed: a thread that keeps a stale control is the
    status quo, not a reason to disrupt the turn that triggered the cleanup.
    """
    try:
        spent = build_options_selected_blocks(list(posted.choices), [])
        blocks = replace_options_blocks(list(posted.blocks), spent)
        await slack.update_message(posted.channel, posted.ts, text=posted.text, blocks=blocks)
    except Exception:
        logger.debug("Failed to expire Slack OPTIONS control", exc_info=True)
