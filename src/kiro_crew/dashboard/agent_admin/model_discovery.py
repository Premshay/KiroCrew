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


async def _discover_agent_models(state: DashboardState, name: str) -> dict[str, Any]:
    """Discover models once and always release the ephemeral provider ourselves."""
    key = f"dashboard:model-discovery:{name}"
    try:
        provider, _is_new, _resumed = await asyncio.wait_for(
            state.sessions.get_or_create(key, agent=name), timeout=30
        )
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


async def _shared_agent_model_discovery(state: DashboardState, name: str) -> dict[str, Any]:
    """Coalesce overlapping discovery requests and briefly reuse successes."""
    cache_key = (id(state.sessions), name)
    cached = _model_discovery_cache.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < _MODEL_DISCOVERY_CACHE_TTL_SECS:
        return cached[1]

    task: asyncio.Task[dict[str, Any]] | None = _model_discovery_tasks.get(cache_key)
    if task is None:

        async def run() -> dict[str, Any]:
            result = await _discover_agent_models(state, name)
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
    if policy is None or policy.get("model") != "selectable":
        return web.json_response({"models": [], "effort_levels": []})
    state: DashboardState | None = request.app.get("state")
    if state is None:
        return web.json_response(
            {"error": "session manager unavailable", "code": "session_manager_unavailable"},
            status=503,
        )
    try:
        result = await _shared_agent_model_discovery(state, name)
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
