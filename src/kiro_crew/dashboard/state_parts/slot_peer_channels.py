"""Durable peer-channel inbox admission, framing, and attention projection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.dashboard.state_parts.chat_slot import _ChatSlot

_PEER_CHANNEL_INBOX_MAX = 20


def init_peer_channel_fields(self: _ChatSlot) -> None:
    self._peer_channel_inbox = []
    self._channel_collaboration_signature = ""


def queue_peer_channel_message(self: _ChatSlot, message: object) -> str:
    """Persist one peer delivery or return its explicit delivery outcome."""
    if not isinstance(message, dict):
        return "invalid"

    from kiro_crew.validation import sanitize_string

    def clean(value: object, limit: int) -> str:
        return sanitize_string(value)[:limit] if isinstance(value, str) else ""

    channel_id = clean(message.get("channel_id"), 32)
    message_id = clean(message.get("message_id"), 32)
    from_role = clean(message.get("from_role"), 100)
    content = clean(message.get("content"), 4000)
    msg_type = clean(message.get("msg_type"), 32) or "progress"
    delivery = clean(message.get("delivery"), 32) or "next_turn"
    content, _ = redact_exfiltration_urls(content)
    content, _ = redact_credentials(content)
    if not channel_id or not message_id or not from_role or not content:
        return "invalid"
    if any(
        item.get("channel_id") == channel_id and item.get("message_id") == message_id
        for item in self._peer_channel_inbox
    ):
        return "duplicate"
    if len(self._peer_channel_inbox) >= _PEER_CHANNEL_INBOX_MAX:
        return "backpressure"
    self._peer_channel_inbox.append(
        {
            "channel_id": channel_id,
            "message_id": message_id,
            "from_role": from_role,
            "content": content,
            "msg_type": msg_type,
            "delivery": delivery,
        }
    )
    return "queued"


def restore_peer_channel_inbox(self: _ChatSlot, inbox: object) -> None:
    """Restore valid peer deliveries without changing their persisted order."""
    if not isinstance(inbox, list):
        return
    for message in inbox:
        self.queue_peer_channel_message(message)


def drain_peer_channel_inbox(
    self: _ChatSlot, *, keep: set[tuple[str, str]] | None = None
) -> list[dict[str, Any]]:
    """Drain unreserved peer deliveries after prompt framing.

    A named request carries its durable message identity in the structural
    queue entry. Leave those inbox rows in place until that entry starts,
    so an earlier peer turn cannot consume the body intended for a later
    card.
    """
    keep = keep or set()
    messages: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    for message in self._peer_channel_inbox:
        identity = (str(message.get("channel_id", "")), str(message.get("message_id", "")))
        if identity in keep:
            retained.append(message)
        else:
            messages.append(dict(message))
    self._peer_channel_inbox = retained
    return messages


def remove_peer_channel_message(self: _ChatSlot, channel_id: str, message_id: str) -> bool:
    """Remove one delivered peer message once a live steer accepts it."""
    for index, message in enumerate(self._peer_channel_inbox):
        if message.get("channel_id") == channel_id and message.get("message_id") == message_id:
            del self._peer_channel_inbox[index]
            return True
    return False


def peer_channel_inbox_payload(self: _ChatSlot) -> list[dict[str, Any]]:
    """Return a copy suitable for durable slot metadata."""
    return [dict(message) for message in self._peer_channel_inbox]


def peer_channel_attention_payload(self: _ChatSlot) -> list[dict[str, str]]:
    """Project peer requests separately from passive inbox receipts."""
    return [
        {
            "channel_id": str(message.get("channel_id", "")),
            "from_role": str(message.get("from_role", "")),
            "delivery": str(message.get("delivery", "next_turn")),
        }
        for message in self._peer_channel_inbox
        if message.get("msg_type") == "mention"
    ]
