"""Kiro Crew notifications when a long Auto-Improvement task ends.

Environment checks, calibrations and runs outlive the page that started them, so their
outcome is pushed to the local notification bus on the ``task-finished`` channel the
manifest declares. Pushes go in-process, through the same gates the HTTP producer
route applies: the app must be enabled, the channel declared, the payload valid, and
the gateway's per-app rate limiter must allow it.

Tasks finish on worker threads, while the bus belongs to the gateway's event loop. A
route handler therefore :func:`bind`\\ s the loop and gateway state before it starts a
task, and :func:`task_finished` hands the push to that loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from kiro_crew.notifications.bus import NotificationPayload, NotificationValidationError
from kiro_crew.platform.context import redact_via_context

from . import store

__all__ = ["CHANNEL", "bind", "task_finished"]

logger = logging.getLogger(__name__)

CHANNEL = "task-finished"
_APP_URL = "/auto-improvement"
_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 1000

_bound_lock = threading.Lock()
_bound: tuple[asyncio.AbstractEventLoop, Any] | None = None


def bind(request: Any) -> None:
    """Remember the running loop and gateway state; call from a route handler."""
    global _bound
    state = request.app.get("state") if hasattr(request, "app") else None
    with _bound_lock:
        _bound = (asyncio.get_running_loop(), state)


def _clip(text: str, limit: int) -> str:
    text = " ".join(redact_via_context(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def task_finished(title: str, body: str, *, group_key: str) -> None:
    """Push one note about a finished task. Thread-safe; never raises."""
    with _bound_lock:
        bound = _bound
    if bound is None:
        return
    loop, state = bound
    if state is None or getattr(state, "notification_bus", None) is None:
        return
    try:
        from kiro_crew.dashboard.handlers import notifications_push

        declared = notifications_push._resolve_app_channels(store.APP_NAME) or {}
    except Exception as exc:  # noqa: BLE001 — a notification never fails its task
        logger.warning("%s: could not read notification channels: %s", store.APP_NAME, exc)
        return
    if CHANNEL not in declared:
        return
    payload = NotificationPayload(
        source=f"app:{store.APP_NAME}",
        channel=f"{store.APP_NAME}.{CHANNEL}",
        title=_clip(title, _MAX_TITLE_CHARS),
        body=_clip(body, _MAX_BODY_CHARS),
        group_key=group_key,
        url=_APP_URL,
    )
    try:
        payload.validate()
    except NotificationValidationError as exc:
        logger.warning("%s: dropped an invalid notification: %s", store.APP_NAME, exc)
        return
    try:
        loop.call_soon_threadsafe(_push, state, payload, declared[CHANNEL])
    except RuntimeError:  # the loop closed with the gateway
        return


def _push(state: Any, payload: NotificationPayload, priority: str) -> None:
    try:
        bus = state.notification_bus
        if not bus.is_registered(payload.channel):
            bus.register_channel(payload.channel, priority)
        limiter = getattr(state, "notification_rate_limiter", None)
        if limiter is not None and not limiter.allow(store.APP_NAME):
            logger.info("%s: notification rate limit reached", store.APP_NAME)
            return
        bus.push(payload)
    except Exception as exc:  # noqa: BLE001 — a notification never fails its task
        logger.warning("%s: notification push failed: %s", store.APP_NAME, exc)
