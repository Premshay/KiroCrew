"""Owner-only, local task-start advice. Applying a model stays on the picker route."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.handlers.decisions import _deny_non_owner
from kiro_crew.loop_lock import LoopBoundLock

logger = logging.getLogger(__name__)
_lock = LoopBoundLock()
_KEY = web.AppKey("preference_advice", dict)
_MAX_TASK = 4000
_COMPUTE_SECONDS = 3.0
_WORK = web.AppKey("preference_work", asyncio.Task)
# Reviewed examples change only when the preference file does, so their vectors
# are kept per embedding model; a request then embeds just its own task.
_example_vectors: dict[tuple[str, str], list[float]] = {}
_example_vectors_lock = threading.Lock()


def _eligible(slot) -> bool:
    return bool(
        slot is not None
        and slot.memory_mode == "persistent"
        and getattr(slot, "_pending_memory_mode", None) in (None, "persistent")
        and not slot.is_remote
        and not slot.total_messages
        and not (slot._task and not slot._task.done())
    )


def _scope(slot) -> tuple:
    return (slot.model, slot.agent, slot.project, slot.workspace, slot._model_pick_gen)


def _configuration() -> dict:
    from kiro_crew.config.paths import config_dir

    path = config_dir() / "routing-preferences.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(1_000_001)
    except FileNotFoundError:
        return {}
    if len(raw) > 1_000_000:
        raise ValueError("preference file too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("invalid preference configuration")
    return data


def preference_advisor_enabled() -> bool:
    try:
        return _configuration().get("enabled") is True
    except (OSError, ValueError):
        logger.warning("Preference adviser configuration is unavailable", exc_info=True)
        return False


def _compute(
    task: str,
    advertised: list[str],
    session_key: str | None = None,
    routing_armed: bool = False,
    deadline: float | None = None,
    role: str = "parent",
    require_mapping: bool = True,
) -> dict:
    from kiro_crew.preference_routing import PreferenceExample, advise, resolve_budget_model

    data = _configuration()
    if data.get("enabled") is not True:
        return {"reason": "not_configured"}
    from kiro_crew.decisions import is_enabled

    if routing_armed and is_enabled("model.route", session_key=session_key):
        return {"reason": "automatic_routing_active"}
    rows = data.get("examples", [])
    mapping = data.get("models", {})
    if not isinstance(rows, list) or len(rows) > 500 or not isinstance(mapping, dict):
        raise ValueError("invalid preference configuration")
    if not all(isinstance(value, str) and len(value) <= 200 for value in mapping.values()):
        raise ValueError("invalid model mapping")
    if require_mapping and not any(
        model in advertised and model not in ("auto", "auto:jev") for model in mapping.values()
    ):
        return {"reason": "no_advertised_mapping"}
    examples = []
    for row in rows:
        if not isinstance(row, dict) or row.get("reviewed") is not True:
            continue
        if not all(
            isinstance(row.get(k), str) and row[k]
            for k in ("id", "group", "task", "role", "budget")
        ):
            raise ValueError("invalid reviewed example")
        if len(row["task"]) > _MAX_TASK:
            raise ValueError("example exceeds task budget")
        examples.append(
            PreferenceExample(
                row["id"][:128],
                row["group"],
                row["task"],
                row["role"],
                row["budget"],
                float(row.get("weight", 1.0)),
            )
        )
    if not any(e.role == role for e in examples):
        return {"reason": "no_examples"}
    from kiro_crew.embeddings import LlamaCppEmbedder, get_shared_embedder

    # An extension can install a network encoder; this preview promises local processing.
    backend = get_shared_embedder()
    if type(backend) is not LlamaCppEmbedder:
        return {"reason": "local_embedding_required"}

    example_texts = {e.task for e in examples}
    model_id = backend.model_id

    def encode(text: str):
        key = (model_id, text)
        if text in example_texts:
            with _example_vectors_lock:
                cached = _example_vectors.get(key)
            if cached is not None:
                return cached
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("preference scoring budget exhausted")
        vector = backend.embed(text)
        if vector and text in example_texts:
            with _example_vectors_lock:
                # Bounded by the configuration's 500-example cap per model.
                _example_vectors[key] = list(vector)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("preference scoring budget exhausted")
        return vector

    try:
        advice = advise(task, role, examples, encode)
    except TimeoutError:
        # The request already answered advice_timeout; raising here would surface
        # from the shielded task as an unhandled asyncio error.
        return {"reason": "advice_timeout"}
    model = resolve_budget_model(advice, mapping, advertised)
    evidence = [e.task[:240] for e in examples if e.id in advice.examples][:2]
    return {**asdict(advice), "model": model, "evidence": evidence}


async def _score(
    request: web.Request,
    task: str,
    advertised: list[str],
    session_key: str | None = None,
    routing_armed: bool = False,
    role: str = "parent",
    require_mapping: bool = True,
) -> dict:
    work = request.app.get(_WORK)
    if work is not None and not work.done():
        return {"reason": "advice_busy"}
    # Native inference can outlive cancellation; retain its task until it really finishes.
    work = asyncio.create_task(
        asyncio.to_thread(
            _compute,
            task,
            advertised,
            session_key,
            routing_armed,
            time.monotonic() + _COMPUTE_SECONDS,
            role,
            require_mapping,
        )
    )
    request.app[_WORK] = work
    work.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    try:
        return await asyncio.wait_for(asyncio.shield(work), _COMPUTE_SECONDS)
    except TimeoutError:
        logger.info("Preference advice exceeded its scoring budget")
        return {"reason": "advice_timeout"}
    except (OSError, ValueError, TypeError):
        logger.warning("Could not read routing preferences", exc_info=True)
        return {"reason": "preference_unavailable"}


async def api_preference_consult(request: web.Request) -> web.Response:
    """Read-only task-boundary advice for a verified persistent session."""
    from kiro_crew.dashboard.handlers._shared import _is_restricted_session
    from kiro_crew.dashboard.handlers.cron import _recognize_session
    from kiro_crew.history import is_incognito_transcript

    if request.get("app"):
        return web.json_response(
            {"error": "owner session required", "code": "dashboard_owner_required"}, status=403
        )
    state = request.app["state"]
    session_key = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, session_key, "preference_advice", blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return refusal
    if _is_restricted_session(state, request):
        return web.json_response({"reason": "restricted_session"}, status=403)
    body, error = await read_bounded_json(request)
    if error is not None:
        return error
    assert body is not None
    task, models, role = body.get("task"), body.get("models"), body.get("role")
    if (
        not isinstance(task, str)
        or not task.strip()
        or len(task) > _MAX_TASK
        or role not in ("parent", "worker")
        or not isinstance(models, list)
        or len(models) > 200
        or not all(isinstance(model, str) and len(model) <= 200 for model in models)
    ):
        return web.json_response(
            {"error": "invalid advice request", "code": "preference_invalid"}, status=400
        )
    result = await _score(
        request, task, models, session_key=session_key, role=role, require_mapping=False
    )
    refusal = await _recognize_session(
        state, session_key, "preference_advice", blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return refusal
    if _is_restricted_session(state, request):
        return web.json_response({"reason": "restricted_session"}, status=403)
    result.pop("evidence", None)
    return web.json_response({**result, "advisory": True, "role": role})


async def api_preference_advice(request: web.Request) -> web.Response:
    denied = await _deny_non_owner(request, "preference_advice")
    if denied is not None:
        return denied
    body, error = await read_bounded_json(request)
    if error is not None:
        return error
    assert body is not None
    name, task, advertised = body.get("slot"), body.get("task"), body.get("models")
    if (
        not isinstance(name, str)
        or not isinstance(task, str)
        or not task.strip()
        or len(task) > _MAX_TASK
        or not isinstance(advertised, list)
        or len(advertised) > 200
        or not all(isinstance(m, str) and len(m) <= 200 for m in advertised)
    ):
        return web.json_response(
            {"error": "invalid advice request", "code": "preference_invalid"}, status=400
        )
    state = request.app["state"]
    slot = state._slots.get(name)
    if name == "":
        result = await _score(request, task, advertised, require_mapping=False)
        if result.get("reason") == "preference_unavailable":
            return web.json_response(
                {"error": "routing preferences unavailable", "code": "preference_unavailable"},
                status=503,
            )
        return web.json_response({**result, "preview": True})
    if not _eligible(slot) or slot._model_pick_gen:
        return web.json_response({"reason": "keep_current"})
    scope = _scope(slot)
    task_hash = hashlib.sha256(task.encode()).hexdigest()
    if state._slots.get(name) is not slot or not _eligible(slot) or _scope(slot) != scope:
        return web.json_response({"reason": "keep_current"})
    if any(
        r["slot"] is slot and r["task_hash"] == task_hash and r["response"] is not None
        for r in request.app.get(_KEY, {}).values()
    ):
        return web.json_response({"reason": "already_answered"})
    from kiro_crew.dashboard.chat_runner import _jev_route_armed
    from kiro_crew.dashboard.chat_utils import effective_session_key

    result = await _score(
        request,
        task,
        advertised,
        effective_session_key(slot),
        _jev_route_armed(slot),
        require_mapping=False,
    )
    if result.get("reason") == "preference_unavailable":
        return web.json_response(
            {"error": "routing preferences unavailable", "code": "preference_unavailable"},
            status=503,
        )
    if state._slots.get(name) is not slot or not _eligible(slot) or _scope(slot) != scope:
        return web.json_response({"reason": "keep_current"})
    if not (result.get("model") or result.get("budget")) or (
        result.get("model") and result["model"] == slot.model
    ):
        return web.json_response({**result, "model": None})
    if _lock.locked():
        return web.json_response({"reason": "advice_busy"})
    cache = request.app.setdefault(_KEY, {})
    if any(
        r["slot"] is slot and r["task_hash"] == task_hash and r["response"] is not None
        for r in cache.values()
    ):
        return web.json_response({"reason": "already_answered"})
    now = time.monotonic()
    for key in list(cache):
        if now - cache[key]["created"] > 900 or cache[key]["slot"] is slot:
            del cache[key]
    if len(cache) >= 128:
        del cache[next(iter(cache))]
    token = uuid.uuid4().hex
    cache[token] = {
        "slot": slot,
        "created": now,
        "current": slot.model,
        "result": result,
        "task_hash": task_hash,
        "scope": scope,
        "response": None,
    }
    return web.json_response({**result, "id": token, "current": slot.model})


async def api_preference_feedback(request: web.Request) -> web.Response:
    denied = await _deny_non_owner(request, "preference_feedback")
    if denied is not None:
        return denied
    body, error = await read_bounded_json(request)
    if error is not None:
        return error
    assert body is not None
    token, choice, model = body.get("id"), body.get("choice"), body.get("model")
    if (
        not isinstance(token, str)
        or choice not in ("use", "keep", "choose")
        or not isinstance(model, str)
    ):
        return web.json_response(
            {"error": "invalid feedback", "code": "preference_invalid"}, status=400
        )
    async with _lock:
        record = request.app.get(_KEY, {}).get(token)
        if not record or time.monotonic() - record["created"] > 900:
            return web.json_response(
                {"error": "recommendation expired", "code": "preference_stale"}, status=409
            )
        slot = record["slot"]
        if not _eligible(slot) or request.app["state"]._slots.get(slot.key) is not slot:
            return web.json_response(
                {"error": "task already changed", "code": "preference_stale"}, status=409
            )
        if _scope(slot)[1:4] != record["scope"][1:4]:
            return web.json_response(
                {"error": "task context changed", "code": "preference_stale"}, status=409
            )
        response = {"choice": choice, "model": model}
        if record["response"] is not None:
            if record["response"] == response:
                return web.json_response({"ok": True})
            return web.json_response(
                {"error": "already answered", "code": "preference_stale"}, status=409
            )
        expected = (
            record["result"]["model"]
            if choice == "use"
            else record["current"] if choice == "keep" else model
        )
        if model != expected or slot.model != model:
            return web.json_response(
                {"error": "model selection changed", "code": "preference_stale"}, status=409
            )
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.decisions import log
        from kiro_crew.history import TranscriptWithheld

        session_key = effective_session_key(slot)
        row = log.build_row(
            point="model.preference",
            session_key=session_key,
            latency_ms=0,
            scrubbed=False,
            error="",
            answers={},
            extra={
                "kind": "preference_feedback",
                "advice_id": token,
                "task_hash": record["task_hash"],
                "budget": record["result"]["budget"],
                "suggested": record["result"]["model"],
                **response,
            },
        )
        history = request.app["state"].conversation_log

        def commit() -> bool:
            if history is None:
                return False
            with history.publication_hold(session_key):
                if not _eligible(slot):
                    raise TranscriptWithheld("session no longer permits feedback")
                return log.append(row)

        try:
            recorded = await asyncio.to_thread(commit)
        except TranscriptWithheld:
            return web.json_response(
                {"error": "session no longer permits feedback", "code": "preference_stale"},
                status=409,
            )
        if not recorded:
            return web.json_response(
                {"error": "feedback was not saved", "code": "preference_write_failed"}, status=503
            )
        record["response"] = response
        return web.json_response({"ok": True})
