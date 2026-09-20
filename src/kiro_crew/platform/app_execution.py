"""Request-authenticated app provenance for in-process background execution.

Only gateway route adapters bind this carrier. It is not serialized, accepted
from workload/config data, or a replacement for governance admission.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any


@dataclass(frozen=True)
class AppExecutionContext:
    app: str
    user: str
    session_key: str = ""


_CURRENT: ContextVar[AppExecutionContext | None] = ContextVar("app_execution", default=None)


def current_app_execution() -> AppExecutionContext | None:
    return _CURRENT.get()


def _from_request(request: Any, app: str) -> AppExecutionContext | None:
    # These mapping fields belong to token_auth middleware, never headers/body.
    user = request.get("user")
    caller_app = request.get("app")
    dashboard_user = request.get("is_dashboard_user")
    if not isinstance(user, str) or not user.strip():
        return None
    if not isinstance(caller_app, str) or not (
        (caller_app == app and dashboard_user is False)
        or (caller_app == "" and dashboard_user is True)
    ):
        return None
    session_key = ""
    token = request.get("auth_token")
    if isinstance(token, str) and token:
        from kiro_crew.dashboard.token_auth import extract_claims_from_token

        session_key = extract_claims_from_token(token, ("session_key",)).get("session_key", "")
    return AppExecutionContext(app=app, user=user, session_key=session_key)


def authenticated_app_execution(app: str):
    """Bind a platform-owned route identity; unauthenticated doubles stay unprivileged."""
    if not isinstance(app, str) or not app.strip():
        raise ValueError("App route identity must be nonempty")

    def decorate(handler):
        @wraps(handler)
        async def wrapped(request):
            execution = await asyncio.to_thread(_from_request, request, app)
            token = _CURRENT.set(execution)
            try:
                return await handler(request)
            finally:
                _CURRENT.reset(token)

        return wrapped

    return decorate


def capture_app_execution(target):
    """Carry only app provenance into an owned worker, restoring it on every exit."""
    execution = current_app_execution()

    @wraps(target)
    def run(*args, **kwargs):
        token = _CURRENT.set(execution)
        try:
            return target(*args, **kwargs)
        finally:
            _CURRENT.reset(token)

    return run
