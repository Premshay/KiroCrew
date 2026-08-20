"""Durable Design Critique review-run routes."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from kiro_crew.apps.manager import app_data_dir, is_app_enabled
from kiro_crew.atomic_write import atomic_write
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kirocrew.app.design-critique")

APP_NAME = "design-critique"
_RUNS_FILENAME = "review-runs.json"
_RUNS_MAX = 100
_RUNS_LOCK = threading.Lock()
_VALID_STATUSES = frozenset({"running", "completed", "failed", "interrupted"})
_MAX_TEXT = 16_000
_MAX_REPORT_BYTES = 1 << 20


def _runs_path() -> Path:
    return app_data_dir(APP_NAME) / _RUNS_FILENAME


def _now() -> int:
    return int(time.time() * 1000)


def _redact_text(value: object) -> str:
    text = _bounded_text(value)
    try:
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
    except Exception:
        logger.warning("design critique review-run redaction unavailable", exc_info=True)
        return "[unavailable: redaction is not available]"
    return text


def _bounded_text(value: object) -> str:
    return str(value or "")[:_MAX_TEXT]


def _safe_value(value: object, *, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            _redact_text(key)[:128]: _safe_value(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    return _redact_text(value)


def _read_runs_locked() -> list[dict[str, Any]]:
    try:
        data = json.loads(_runs_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("design critique review-run ledger is unreadable", exc_info=True)
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write_runs_locked(runs: list[dict[str, Any]]) -> None:
    atomic_write(_runs_path(), json.dumps(runs, indent=2), mode=0o600)


def _recover_runs() -> None:
    with _RUNS_LOCK:
        runs = _read_runs_locked()
        changed = False
        for run in runs:
            if run.get("status") == "running":
                run.update(
                    status="interrupted",
                    error={"code": "gateway_restarted"},
                    updated_at=_now(),
                )
                changed = True
        if changed:
            _write_runs_locked(runs)


def _create_run(payload: dict[str, Any]) -> dict[str, Any]:
    slot_key = _bounded_text(payload.get("slot_key"))[:256]
    agent = _bounded_text(payload.get("agent"))[:128]
    if not slot_key or not agent:
        raise ValueError("invalid_run")
    now = _now()
    run = {
        "id": uuid.uuid4().hex,
        "status": "running",
        "slot_key": slot_key,
        "agent": agent,
        "model": _bounded_text(payload.get("model"))[:256],
        "stage": _bounded_text(payload.get("stage"))[:128],
        "source": _safe_value(
            payload.get("source") if isinstance(payload.get("source"), dict) else {}
        ),
        "screens": _safe_value(
            payload.get("screens") if isinstance(payload.get("screens"), list) else []
        ),
        "created_at": now,
        "updated_at": now,
        "report": None,
        "error": None,
    }
    with _RUNS_LOCK:
        runs = _read_runs_locked()
        runs.insert(0, run)
        _write_runs_locked(runs[:_RUNS_MAX])
    return run


def _update_run(run_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    with _RUNS_LOCK:
        runs = _read_runs_locked()
        for run in runs:
            if run.get("id") != run_id:
                continue
            status = payload.get("status")
            if status is not None:
                if status not in _VALID_STATUSES:
                    raise ValueError("invalid_status")
                run["status"] = status
            for field in ("stage", "source", "screens", "error"):
                if field in payload:
                    run[field] = _safe_value(payload[field])
            if "report" in payload:
                report = _safe_value(payload["report"])
                if len(json.dumps(report)) > _MAX_REPORT_BYTES:
                    raise ValueError("report_too_large")
                run["report"] = report
            run["updated_at"] = _now()
            _write_runs_locked(runs[:_RUNS_MAX])
            return dict(run)
    return None


def _list_runs() -> list[dict[str, Any]]:
    with _RUNS_LOCK:
        return [dict(run) for run in _read_runs_locked()[:_RUNS_MAX]]


def _require_enabled(handler: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(handler)
    async def wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response({"code": "app_disabled"}, status=403)
        return await handler(request)

    return wrapped


async def _json_body(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


@_require_enabled
async def _handle_runs(request: web.Request) -> web.Response:
    return web.json_response({"runs": await asyncio.to_thread(_list_runs)})


@_require_enabled
async def _handle_create_run(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return web.json_response({"code": "invalid_json"}, status=400)
    try:
        run = await asyncio.to_thread(_create_run, body)
    except ValueError as exc:
        return web.json_response({"code": str(exc)}, status=400)
    except OSError:
        logger.warning("design critique review-run create failed", exc_info=True)
        return web.json_response({"code": "review_run_persist_failed"}, status=500)
    return web.json_response({"run": run}, status=201)


@_require_enabled
async def _handle_update_run(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return web.json_response({"code": "invalid_json"}, status=400)
    try:
        run = await asyncio.to_thread(_update_run, request.match_info["run_id"], body)
    except ValueError as exc:
        return web.json_response({"code": str(exc)}, status=400)
    except OSError:
        logger.warning("design critique review-run update failed", exc_info=True)
        return web.json_response({"code": "review_run_persist_failed"}, status=500)
    if run is None:
        return web.json_response({"code": "review_run_not_found"}, status=404)
    return web.json_response({"run": run})


def register_routes(app: web.Application) -> None:
    """Register app-owned persistence endpoints and close stale live records."""
    try:
        _recover_runs()
    except OSError:
        logger.warning("design critique review-run recovery failed", exc_info=True)
    app.router.add_get("/api/apps/design-critique/runs", _handle_runs)
    app.router.add_post("/api/apps/design-critique/runs", _handle_create_run)
    app.router.add_patch("/api/apps/design-critique/runs/{run_id}", _handle_update_run)
