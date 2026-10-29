"""Read-only repository recommendations for Setup."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from aiohttp import web

from . import clone_setup, commit, store
from .repository_survey import resolve_survey_ref, survey_repository


async def handle_repository_survey(_request: web.Request) -> web.Response:
    from .routes import _refuse_while_running, _run_is_active

    if busy := await _refuse_while_running("repository discovery needs a stable checkout."):
        return busy

    def read():
        with commit.clone_lock():
            if _run_is_active():
                return None
            config = store.read_json(store.config_path(), {}) or {}
            if not config.get("clone"):
                raise ValueError("Connect a repository before discovering workloads.")
            root = Path(config["clone"])
            if not clone_setup._repository_is_isolated(root):
                raise ValueError("Repository isolation failed; reconnect the repository.")
            branch = str(config.get("branch") or "HEAD")
            ref = resolve_survey_ref(root, branch)
            return survey_repository(root, ref)

    try:
        result = await asyncio.to_thread(read)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        from kiro_crew.platform.context import redact_via_context

        return web.json_response(
            {"code": "repository_survey_failed", "error": redact_via_context(str(exc))[:4000]},
            status=400,
        )
    if result is None:
        return web.json_response(
            {
                "code": "run_in_progress",
                "error": "A run started; retry discovery after it finishes.",
            },
            status=409,
        )
    return web.json_response(result)


async def handle_repository_assessment(request: web.Request) -> web.Response:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    from . import notify, repository_assessment
    from .routes import _audit_owner_route_allowed, _refuse_while_running

    operation = "auto_improvement.repository_assessment"
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    await _audit_owner_route_allowed(request, operation)
    busy = await _refuse_while_running("repository assessment needs a stable checkout.")
    if busy is not None:
        return busy
    # Only a bounded repository area selector; never a model prompt or command.
    try:
        raw = await request.content.readexactly(4097)
    except asyncio.IncompleteReadError as exc:
        raw = exc.partial
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        return web.json_response({"error": "Assessment requires a JSON object"}, status=400)
    if len(raw) > 4096 or not isinstance(body, dict):
        return web.json_response({"error": "Assessment requires a bounded JSON object"}, status=400)
    area_path = body.get("areaPath", "")
    if set(body) - {"areaPath"} or not isinstance(area_path, str) or len(area_path) > 300:
        return web.json_response({"error": "Invalid assessment area"}, status=400)
    notify.bind(request)
    try:
        result = await asyncio.to_thread(repository_assessment.start_assessment, area_path)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        from kiro_crew.platform.context import redact_via_context

        return web.json_response({"error": redact_via_context(str(exc))[:2000]}, status=400)
    if result is None:
        return web.json_response({"error": "An assessment is already running"}, status=409)
    return web.json_response(result, status=202)


async def handle_repository_assessment_status(_request: web.Request) -> web.Response:
    from .repository_assessment import read_assessment

    area_path = _request.query.get("areaPath", "")
    if len(area_path) > 300:
        return web.json_response({"error": "Invalid assessment area"}, status=400)
    try:
        return web.json_response(await asyncio.to_thread(read_assessment, area_path))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        from kiro_crew.platform.context import redact_via_context

        return web.json_response({"error": redact_via_context(str(exc))[:2000]}, status=400)


async def handle_repository_assessment_stop(request: web.Request) -> web.Response:
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    from .repository_assessment import stop_assessment
    from .routes import _audit_owner_route_allowed

    operation = "auto_improvement.repository_assessment_stop"
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    await _audit_owner_route_allowed(request, operation)
    stop_assessment()
    return web.json_response({"ok": True})
