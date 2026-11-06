"""Per-crew runtime policy and coalesced model discovery for the agents facade."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    import time

    from kiro_crew.dashboard.handlers.agents import (
        _MODEL_DISCOVERY_CACHE_TTL_SECS,
        DashboardState,
        KiroCrewConfig,
        _model_discovery_cache,
        _model_discovery_tasks,
        acp_model_config_options,
        logger,
        model_registry,
    )


def _agent_runtime_policy(
    request: web.Request, name: str, engine_identity: str | None = None
) -> dict[str, Any] | None:
    """Return companion-owned UI policy without making it an enforcement boundary."""
    platform_context = request.app.get("platform_context")
    getter = (
        getattr(platform_context.providers, "agent_runtime_policy", None)
        if platform_context
        else None
    )
    if not callable(getter):
        return None
    try:
        policy = getter(engine_identity or name)
    except Exception:
        logger.warning("Failed to read runtime policy for crew %s", name, exc_info=True)
        return None
    return policy if isinstance(policy, dict) else None


async def _discover_agent_models(
    state: DashboardState, name: str, backend: str | None = None
) -> dict[str, Any]:
    """Discover models once and always release the ephemeral provider ourselves."""
    key = f"dashboard:model-discovery:{name}" + (f":{backend}" if backend is not None else "")
    try:
        pending = (
            state.sessions.get_or_create(key, agent=name, backend_override=backend)
            if backend is not None
            else state.sessions.get_or_create(key, agent=name)
        )
        provider, _is_new, _resumed = await asyncio.wait_for(pending, timeout=30)
        available = await provider.discover_models()
        efforts: list[str] = getattr(provider, "get_valid_effort_levels", lambda: [])()
        configured_models = acp_model_config_options(getattr(provider, "acp_config_options", []))
        if configured_models:
            available = configured_models
        models = [
            {
                "modelId": str(model.get("modelId") or ""),
                "name": str(model.get("name") or model.get("modelId") or ""),
                "description": str(model.get("description") or ""),
            }
            for model in available
            if isinstance(model, dict) and model.get("modelId")
        ]
        result: dict[str, Any] = {
            "models": models,
            "effort_levels": [level for level in efforts if isinstance(level, str)],
        }
        if hasattr(provider, "supports_image_prompt"):
            images = provider.supports_image_prompt
            result["supports_images"] = images if isinstance(images, bool) else None
        return result
    finally:
        if state.sessions.has_session(key):
            state.sessions.release(key)
            await state.sessions.destroy(key)


async def _shared_agent_model_discovery(
    state: DashboardState, name: str, backend: str | None = None
) -> dict[str, Any]:
    """Coalesce overlapping discovery requests and briefly reuse successes."""
    cache_key = (id(state.sessions), name, backend)
    cached = _model_discovery_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < _MODEL_DISCOVERY_CACHE_TTL_SECS:
        return cached[1]

    task: asyncio.Task[dict[str, Any]] | None = _model_discovery_tasks.get(cache_key)
    if task is None:

        async def run() -> dict[str, Any]:
            result = await _discover_agent_models(state, name, backend)
            _model_discovery_cache[cache_key] = (time.monotonic(), result)
            return result

        task = asyncio.create_task(run(), name=f"model-discovery:{name}")
        _model_discovery_tasks[cache_key] = task

        def clear_finished_task(completed: asyncio.Task[dict[str, Any]]) -> None:
            if _model_discovery_tasks.get(cache_key) is completed:
                _model_discovery_tasks.pop(cache_key, None)

        task.add_done_callback(clear_finished_task)
    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and _model_discovery_tasks.get(cache_key) is task:
            _model_discovery_tasks.pop(cache_key, None)


async def api_kirocrew_agent_models(request: web.Request) -> web.Response:
    """GET /api/agents/{name}/models — discover a selectable crew's live options."""
    name = request.match_info["name"]
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if name not in cfg.agents:
        return web.json_response(
            {"error": "agent not found", "code": "agent_not_found"}, status=404
        )
    policy = _agent_runtime_policy(request, name, cfg.agents[name].kiro_agent or name)
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_pin_for

    pin = crewmate_pin_for(cfg, name)
    try:
        backend = pin.checked().backend if pin is not None else None
    except CrewmateHarnessUnavailable as exc:
        return web.json_response({"error": str(exc), "code": exc.code}, status=409)
    if pin is None and (policy is None or policy.get("model") != "selectable"):
        return web.json_response({"models": [], "effort_levels": []})
    state: DashboardState | None = request.app.get("state")
    if state is None:
        return web.json_response(
            {"error": "session manager unavailable", "code": "session_manager_unavailable"},
            status=503,
        )
    try:
        result = await _shared_agent_model_discovery(state, name, backend)
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": "model discovery timed out", "code": "models_timeout"}, status=503
        )
    except Exception:
        logger.warning("Model discovery failed for crew %s", name, exc_info=True)
        return web.json_response(
            {"error": "model discovery unavailable", "code": "models_unavailable"}, status=503
        )
    if not result["models"]:
        return web.json_response(
            {"error": "subscription returned no models", "code": "models_unavailable"}, status=503
        )
    return web.json_response(result)


def _normalize_model_key(name: str) -> str:
    """Canonical key for de-duping CC model ids across spelling variants.

    Mirrors ``normalizeModelKey`` in ``website/src/lib/model.ts``: both route a
    model id through the shared canonical registry (``model_registry.json``) so
    "same model?" has ONE definition across the dashboard (dropdown dedup, slot
    display, and the subagent downgrade flag).

    Resolution order:
    1. ``auto``/``default``/unset -> the ``auto`` sentinel (both mean "let the
       backend pick"); an empty id stays ``""`` (no pin, distinct from Auto).
    2. Registry canonical key: a canonical key, a registry alias, or a
       claude_code provider id -- with or without a region/vendor routing prefix
       (``us.anthropic.…``, ``global.anthropic.…``) -- folds to its canonical
       key. This makes an alias and its provider-prefixed canonical id equal
       (``us.anthropic.claude-opus-4-8[1m]`` == ``claude-opus-4.8`` ->
       ``opus-4.8-1m``) while keeping DISTINCT registry entries distinct -- the
       advertised dashed ``claude-opus-4-8`` (200K, ``opus-4.8``) does NOT fold
       onto dotted ``claude-opus-4.8`` (1M, ``opus-4.8-1m``); a bare
       ``.``->``-`` fold conflates those two different-window models.
    3. Fallback for an id the registry does not list (GPT/DeepSeek/Qwen, future
       models, operator-typed ids): a lossless fold -- lowercase,
       ``.``->``-`` -- so behavior is identity-preserving off the registered set,
       matching ``from_provider_id``'s pass-through contract.
    """
    string_fold = (name or "").strip().lower().replace(".", "-")
    if not string_fold:
        return ""
    if string_fold in ("default", "auto"):
        return "auto"
    # Registry lookups are exact and its keys/aliases/provider-ids are all
    # lowercase, so resolve on the lowercased id. canonical_key resolves
    # acp-first then claude_code AND peels a known routing prefix, so it covers
    # both spelling halves above; a miss returns None.
    resolved = model_registry.canonical_key((name or "").strip().lower())
    if resolved is not None:
        return resolved
    return string_fold
