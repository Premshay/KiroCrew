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
from urllib.parse import urlparse

from aiohttp import web

from kiro_crew.apps.manager import app_data_dir, is_app_enabled
from kiro_crew.atomic_write import atomic_write
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger("kirocrew.app.design-critique")

APP_NAME = "design-critique"
_RUNS_FILENAME = "review-runs.json"
_RUNS_MAX = 100
_RUNS_LOCK = threading.Lock()
_CONTEXTS_FILENAME = "project-contexts.json"
_CONTEXTS_MAX = 50
_CONTEXTS_LOCK = threading.Lock()
_DESIGN_ROUNDS_FILENAME = "design-rounds.json"
_DESIGN_ROUNDS_MAX = 100
_DESIGN_ROUNDS_LOCK = threading.Lock()
_VALID_STATUSES = frozenset({"running", "completed", "failed", "interrupted"})
_VALID_DESIGN_ROUND_STATUSES = frozenset(
    {
        "prepared",
        "owner_send_confirmed",
        "building",
        "ready_to_harvest",
        "harvested",
        "interrupted",
        "failed",
    }
)
_MAX_TEXT = 16_000
_MAX_REPORT_BYTES = 1 << 20


def _runs_path() -> Path:
    return app_data_dir(APP_NAME) / _RUNS_FILENAME


def _contexts_path() -> Path:
    return app_data_dir(APP_NAME) / _CONTEXTS_FILENAME


def _design_rounds_path() -> Path:
    return app_data_dir(APP_NAME) / _DESIGN_ROUNDS_FILENAME


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


def _read_contexts_locked() -> list[dict[str, Any]]:
    try:
        data = json.loads(_contexts_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("design critique project-context catalog is unreadable", exc_info=True)
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write_contexts_locked(contexts: list[dict[str, Any]]) -> None:
    atomic_write(_contexts_path(), json.dumps(contexts, indent=2), mode=0o600)


def _safe_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [path for item in value[:50] if (path := _redact_text(item).strip()[:512])]


def _create_context(payload: dict[str, Any]) -> dict[str, Any]:
    name = _redact_text(payload.get("name")).strip()[:160]
    if not name:
        raise ValueError("invalid_project_context")
    now = _now()
    context = {
        "id": uuid.uuid4().hex,
        "name": name,
        "repository": _redact_text(payload.get("repository"))[:1024],
        "context_paths": _safe_paths(payload.get("context_paths")),
        "notes": _redact_text(payload.get("notes")),
        "created_at": now,
        "updated_at": now,
    }
    with _CONTEXTS_LOCK:
        contexts = _read_contexts_locked()
        contexts.insert(0, context)
        _write_contexts_locked(contexts[:_CONTEXTS_MAX])
    return context


def _update_context(context_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"name", "repository", "context_paths", "notes"}
    if not set(payload).intersection(allowed):
        raise ValueError("empty_project_context_update")
    with _CONTEXTS_LOCK:
        contexts = _read_contexts_locked()
        for context in contexts:
            if context.get("id") != context_id:
                continue
            if "name" in payload:
                name = _redact_text(payload["name"]).strip()[:160]
                if not name:
                    raise ValueError("invalid_project_context")
                context["name"] = name
            if "repository" in payload:
                context["repository"] = _redact_text(payload["repository"])[:1024]
            if "context_paths" in payload:
                context["context_paths"] = _safe_paths(payload["context_paths"])
            if "notes" in payload:
                context["notes"] = _redact_text(payload["notes"])
            context["updated_at"] = _now()
            _write_contexts_locked(contexts[:_CONTEXTS_MAX])
            return dict(context)
    return None


def _delete_context(context_id: str) -> bool:
    with _CONTEXTS_LOCK:
        contexts = _read_contexts_locked()
        next_contexts = [context for context in contexts if context.get("id") != context_id]
        if len(next_contexts) == len(contexts):
            return False
        _write_contexts_locked(next_contexts[:_CONTEXTS_MAX])
    return True


def _list_contexts() -> list[dict[str, Any]]:
    with _CONTEXTS_LOCK:
        return [dict(context) for context in _read_contexts_locked()[:_CONTEXTS_MAX]]


def _read_design_rounds_locked() -> list[dict[str, Any]]:
    try:
        data = json.loads(_design_rounds_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        logger.warning("design critique design-round ledger is unreadable", exc_info=True)
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write_design_rounds_locked(rounds: list[dict[str, Any]]) -> None:
    atomic_write(_design_rounds_path(), json.dumps(rounds, indent=2), mode=0o600)


def _claude_design_url(value: object) -> str:
    url = _redact_text(value).strip()[:1024]
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "claude.ai" or not parsed.path.startswith("/design/"):
        raise ValueError("invalid_claude_design_url")
    return url


def _brief_lines(value: object, *, limit: int = 12) -> list[str]:
    if not isinstance(value, str):
        return []
    return [_redact_text(line).strip()[:512] for line in value.splitlines() if line.strip()][:limit]


def _design_round_prompt(payload: dict[str, Any], report: dict[str, Any]) -> str:
    intent = _bounded_text(payload.get("intent") or "ground")[:32]
    intent_line = {
        "ground": "discover / ground — match and extend the existing canonical context; do not invent a new visual language.",
        "reference": "reference — use the supplied system and references to build this surface.",
        "invent": "invent — explore a new direction, while respecting the stated constraints.",
    }.get(intent, "discover / ground — match the supplied canonical context.")
    mode = _bounded_text(payload.get("mode") or "generate-design")[:32]
    target = _redact_text(payload.get("target")).strip()[:2_000]
    project_name = _redact_text(payload.get("project_name")).strip()[:160]
    repository = _redact_text(payload.get("repository")).strip()[:1024]
    paths = _brief_lines(payload.get("context_paths"))
    notes = _redact_text(payload.get("notes")).strip()[:4_000]
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    finding_lines = []
    for finding in findings[:8]:
        if not isinstance(finding, dict):
            continue
        title = _redact_text(finding.get("title")).strip()[:240]
        fix = _redact_text(finding.get("fix")).strip()[:360]
        if title:
            finding_lines.append(f"- {title}" + (f": {fix}" if fix else ""))
    paths_block = "\n".join(f"- {path}" for path in paths) or "- No file paths supplied; ask before assuming implementation details."
    findings_block = "\n".join(finding_lines) or "- No critique findings were attached; use the stated target and constraints."
    return "\n".join(
        [
            "# Claude Design round",
            f"Mode: {mode}",
            f"Intent: {intent_line}",
            f"Project: {project_name or 'Unspecified project'}",
            f"Target: {target or 'Use the supplied critique and context to identify the focused surface.'}",
            "",
            "## Grounding",
            f"Repository: {repository or 'Not supplied'}",
            "Read/link these canonical inputs before designing:",
            paths_block,
            "",
            "## Constraints and decisions",
            notes or "- No additional constraints supplied.",
            "",
            "## Critique findings to address",
            findings_block,
            "",
            "## Acceptance",
            "- Produce the requested artifact; use generate-prototype only when the target needs interaction or state transitions.",
            "- Preserve the existing design system. Propose any new token or primitive only in design-system-delta.md; do not silently fork the system.",
            "- Before handoff, package the artifact plus design-system-delta.md and handoff-bundle-README.md.",
            "- The README must state the exact implementation instruction, referenced canonical inputs, and unresolved decisions.",
        ]
    )


def _create_design_round(payload: dict[str, Any]) -> dict[str, Any]:
    report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
    mode = _bounded_text(payload.get("mode") or "generate-design")[:32]
    if mode not in {"generate-design", "generate-prototype"}:
        raise ValueError("invalid_design_round_mode")
    intent = _bounded_text(payload.get("intent") or "ground")[:32]
    if intent not in {"ground", "reference", "invent"}:
        raise ValueError("invalid_design_round_intent")
    now = _now()
    prompt_payload = {
        "intent": intent,
        "mode": mode,
        "project_name": payload.get("project_name"),
        "repository": payload.get("repository"),
        "context_paths": payload.get("context_paths"),
        "notes": payload.get("notes"),
        "target": payload.get("target"),
    }
    round_record = {
        "id": uuid.uuid4().hex,
        "status": "prepared",
        "mode": mode,
        "intent": intent,
        "project_name": _redact_text(payload.get("project_name")).strip()[:160],
        "target": _redact_text(payload.get("target")).strip()[:2_000],
        "claude_design_url": _claude_design_url(payload.get("claude_design_url")),
        "handoff_path": _redact_text(payload.get("handoff_path")).strip()[:1024],
        "review_run_id": _bounded_text(payload.get("review_run_id"))[:128],
        "prompt": _design_round_prompt(prompt_payload, _safe_value(report)),
        "evidence": {"files": [], "note": ""},
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    with _DESIGN_ROUNDS_LOCK:
        rounds = _read_design_rounds_locked()
        rounds.insert(0, round_record)
        _write_design_rounds_locked(rounds[:_DESIGN_ROUNDS_MAX])
    return round_record


def _update_design_round(round_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"status", "claude_design_url", "handoff_path", "evidence", "error"}
    if not set(payload).intersection(allowed):
        raise ValueError("empty_design_round_update")
    with _DESIGN_ROUNDS_LOCK:
        rounds = _read_design_rounds_locked()
        for round_record in rounds:
            if round_record.get("id") != round_id:
                continue
            if "status" in payload:
                status = payload["status"]
                if status not in _VALID_DESIGN_ROUND_STATUSES:
                    raise ValueError("invalid_design_round_status")
                round_record["status"] = status
            if "claude_design_url" in payload:
                round_record["claude_design_url"] = _claude_design_url(payload["claude_design_url"])
            if "handoff_path" in payload:
                round_record["handoff_path"] = _redact_text(payload["handoff_path"]).strip()[:1024]
            if "evidence" in payload:
                evidence = payload["evidence"] if isinstance(payload["evidence"], dict) else {}
                round_record["evidence"] = {
                    "files": _safe_paths(evidence.get("files")),
                    "note": _redact_text(evidence.get("note")).strip()[:4_000],
                }
            if "error" in payload:
                round_record["error"] = _safe_value(payload["error"])
            round_record["updated_at"] = _now()
            _write_design_rounds_locked(rounds[:_DESIGN_ROUNDS_MAX])
            return dict(round_record)
    return None


def _list_design_rounds() -> list[dict[str, Any]]:
    with _DESIGN_ROUNDS_LOCK:
        return [dict(round_record) for round_record in _read_design_rounds_locked()[:_DESIGN_ROUNDS_MAX]]


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
async def _handle_contexts(request: web.Request) -> web.Response:
    return web.json_response({"contexts": await asyncio.to_thread(_list_contexts)})


@_require_enabled
async def _handle_create_context(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return web.json_response({"code": "invalid_json"}, status=400)
    try:
        context = await asyncio.to_thread(_create_context, body)
    except ValueError as exc:
        return web.json_response({"code": str(exc)}, status=400)
    except OSError:
        logger.warning("design critique project-context create failed", exc_info=True)
        return web.json_response({"code": "project_context_persist_failed"}, status=500)
    return web.json_response({"context": context}, status=201)


@_require_enabled
async def _handle_update_context(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return web.json_response({"code": "invalid_json"}, status=400)
    try:
        context = await asyncio.to_thread(_update_context, request.match_info["context_id"], body)
    except ValueError as exc:
        return web.json_response({"code": str(exc)}, status=400)
    except OSError:
        logger.warning("design critique project-context update failed", exc_info=True)
        return web.json_response({"code": "project_context_persist_failed"}, status=500)
    if context is None:
        return web.json_response({"code": "project_context_not_found"}, status=404)
    return web.json_response({"context": context})


@_require_enabled
async def _handle_delete_context(request: web.Request) -> web.Response:
    try:
        deleted = await asyncio.to_thread(_delete_context, request.match_info["context_id"])
    except OSError:
        logger.warning("design critique project-context delete failed", exc_info=True)
        return web.json_response({"code": "project_context_persist_failed"}, status=500)
    if not deleted:
        return web.json_response({"code": "project_context_not_found"}, status=404)
    return web.Response(status=204)


@_require_enabled
async def _handle_design_rounds(request: web.Request) -> web.Response:
    return web.json_response({"rounds": await asyncio.to_thread(_list_design_rounds)})


@_require_enabled
async def _handle_create_design_round(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return web.json_response({"code": "invalid_json"}, status=400)
    try:
        design_round = await asyncio.to_thread(_create_design_round, body)
    except ValueError as exc:
        return web.json_response({"code": str(exc)}, status=400)
    except OSError:
        logger.warning("design critique design-round create failed", exc_info=True)
        return web.json_response({"code": "design_round_persist_failed"}, status=500)
    return web.json_response({"round": design_round}, status=201)


@_require_enabled
async def _handle_update_design_round(request: web.Request) -> web.Response:
    body = await _json_body(request)
    if body is None:
        return web.json_response({"code": "invalid_json"}, status=400)
    try:
        design_round = await asyncio.to_thread(
            _update_design_round, request.match_info["round_id"], body
        )
    except ValueError as exc:
        return web.json_response({"code": str(exc)}, status=400)
    except OSError:
        logger.warning("design critique design-round update failed", exc_info=True)
        return web.json_response({"code": "design_round_persist_failed"}, status=500)
    if design_round is None:
        return web.json_response({"code": "design_round_not_found"}, status=404)
    return web.json_response({"round": design_round})


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
    app.router.add_get("/api/apps/design-critique/contexts", _handle_contexts)
    app.router.add_post("/api/apps/design-critique/contexts", _handle_create_context)
    app.router.add_patch("/api/apps/design-critique/contexts/{context_id}", _handle_update_context)
    app.router.add_delete("/api/apps/design-critique/contexts/{context_id}", _handle_delete_context)
    app.router.add_get("/api/apps/design-critique/design-rounds", _handle_design_rounds)
    app.router.add_post("/api/apps/design-critique/design-rounds", _handle_create_design_round)
    app.router.add_patch(
        "/api/apps/design-critique/design-rounds/{round_id}", _handle_update_design_round
    )
