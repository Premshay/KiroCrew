"""Bounded provider notices and their per-session delivery queue."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict, deque

from kiro_crew.acp._dispatch_parts.surfaced_text import redact_text
from kiro_crew.acp.types import EVENT_NOTICE, METHOD_SESSION_UPDATE, AcpEvent, JsonRpcMessage

logger = logging.getLogger("kiro_crew.acp._dispatch")

NOTICE_STATE_MAX = 128
NOTICE_TITLE_MAX = 256
NOTICE_DESCRIPTION_MAX = 4096
NOTICE_SEVERITY_MAX = 64


def parse_notice(update: object) -> AcpEvent | None:
    if not isinstance(update, dict) or update.get("sessionUpdate") != "notice":
        return None
    title = update.get("title")
    severity = update.get("severity")
    if not isinstance(title, str) or not title.strip() or not isinstance(severity, str):
        return None
    description = update.get("description")
    description = description if isinstance(description, str) else ""

    def clean(value: str, limit: int) -> str:
        value = redact_text(value)
        return "".join(c for c in value if c in "\n\t" or c.isprintable())[:limit]

    title = clean(title.strip(), NOTICE_TITLE_MAX)
    if not title:
        return None
    return AcpEvent(
        kind=EVENT_NOTICE,
        title=title,
        text=clean(description, NOTICE_DESCRIPTION_MAX),
        notice_severity=clean(severity, NOTICE_SEVERITY_MAX),
    )


class SessionNoticeState:
    """Per-session notice dedupe window and between-turn staging queue.

    Both are capped at ``NOTICE_STATE_MAX``. The dedupe window forgets its oldest
    key when full, which can only let a repeat through. The staging queue drops
    its oldest notice when full; each drop is counted, and ``drain`` reports the
    count once as a warning notice so a truncated backlog never reads as complete.
    """

    def __init__(self) -> None:
        self.session_id = ""
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.pending: deque[AcpEvent] = deque(maxlen=NOTICE_STATE_MAX)
        self.dropped = 0

    def accept(
        self, msg: JsonRpcMessage, session_id: str, *, stage: bool = False
    ) -> AcpEvent | None:
        params = msg.params if isinstance(msg.params, dict) else {}
        if not session_id or params.get("sessionId") != session_id:
            return None
        if not msg.is_method(METHOD_SESSION_UPDATE) or msg.fanout_no_owner:
            return None
        if self.session_id != session_id:
            self._reset(session_id)
        event = parse_notice(params.get("update"))
        if event is None:
            return None
        key = hashlib.sha256(
            json.dumps([event.notice_severity, event.title, event.text]).encode("utf-8")
        ).hexdigest()
        if key in self.seen:
            self.seen.move_to_end(key)
            return None
        self.seen[key] = None
        if len(self.seen) > NOTICE_STATE_MAX:
            self.seen.popitem(last=False)
        if stage:
            if len(self.pending) == self.pending.maxlen:
                self.dropped += 1
            self.pending.append(event)
        return event

    def drain(self, session_id: str) -> list[AcpEvent]:
        if self.session_id != session_id:
            self._reset(session_id)
        events = list(self.pending)
        self.pending.clear()
        if self.dropped:
            logger.warning(
                "Provider notice backlog over %d: %d earlier notice(s) dropped",
                NOTICE_STATE_MAX,
                self.dropped,
            )
            events.insert(
                0,
                AcpEvent(
                    kind=EVENT_NOTICE,
                    title=f"{self.dropped} earlier provider notice(s) were dropped",
                    text=f"Only the latest {NOTICE_STATE_MAX} queued notices are shown.",
                    notice_severity="warning",
                ),
            )
            self.dropped = 0
        return events

    def _reset(self, session_id: str) -> None:
        self.session_id = session_id
        self.seen.clear()
        self.pending.clear()
        self.dropped = 0
