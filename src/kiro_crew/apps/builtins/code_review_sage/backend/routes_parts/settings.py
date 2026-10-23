"""The review settings: model, effort, active namespaces and concurrency.

The selectable models, the model validator, the ``review`` section read and
atomic write, and the settings endpoint with its audit. New settings go here.

Composed onto :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`;
see this package's ``__init__``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_review_sage.backend.routes import (
        _review_settings_lock,
        atomic_write,
        discovery,
        learning,
        logger,
        model_registry,
        require_owner_dashboard_request,
        review_pool,
        store,
        web,
    )


# --- Settings (model / effort / active namespaces) ---------------------------
# These let the dashboard read + write the review knobs that live in config.json
# under the "review" section. The generic GET /api/apps/{name}/config already
# exposes the full config (read), so this route is the WRITE path plus a focused
# settings view that also enumerates available models, efforts, and namespaces.


def _load_known_models() -> list[str]:
    """Selectable models for the review-settings dropdown — the registry's
    CANONICAL keys (e.g. ``opus-4.8-1m``), which are the wire/persisted format
    the review worker consumes, NOT the provider ids from ``available_models``.

    Provider ids carry a ``[1m]`` capability suffix (e.g.
    ``global.anthropic.claude-opus-4-8[1m]``); the brackets fail ``_valid_model``'s
    safe-token check, so sourcing the dropdown from provider ids made every 1M
    variant unselectable (the PUT 400'd and the dropdown snapped back — only the
    bracket-free plain ids survived). Canonical keys are bracket-free tokens that
    both pass validation AND match what ``review_pool`` writes into the worker's
    cli.json overlay. Empty on failure; the UI still offers 'Default (agent config)'."""
    try:
        return [row["model_name"] for row in model_registry.display_list("claude_code")]
    except Exception:  # pragma: no cover - defensive
        return []


def _known_models() -> list[str]:
    """Safe override keys for the reviewer's latest runtime model snapshot."""
    try:
        reviewer = review_pool.reviewer_info()
        engine = reviewer.get("engine")
        models: list[str] = []
        for raw in reviewer.get("models") or []:
            if not isinstance(raw, str):
                continue
            model = (
                model_registry.from_provider_id(raw, "claude_code") if engine == "claude" else raw
            )
            if not model or len(model) > 64 or not all(c.isalnum() or c in "._-" for c in model):
                continue
            if model not in models:
                models.append(model)
        return models
    except Exception:
        logger.warning("Could not enumerate review models", exc_info=True)
        return []


def _valid_model(m: str) -> bool:
    """A model id is acceptable if it is a safe token (it becomes a cli.json
    overlay key for the worker subprocess) and belongs to Sage's latest runtime
    capability snapshot."""
    if not m or len(m) > 64 or not all(c.isalnum() or c in "._-" for c in m):
        return False
    known = _known_models()
    return m in known


def _load_review_section() -> dict:
    """Read persisted agent, model, effort, and namespace-resolution settings."""
    try:
        cfg = store.load_config()
    except Exception:
        cfg = {}
    review = cfg.get("review") if isinstance(cfg, dict) else None
    if not isinstance(review, dict):
        review = {}
    return {
        "agent": review.get("agent") or None,
        "model": review.get("model") or None,
        "effort": review.get("effort", ""),
        "active_namespaces": review.get("active_namespaces") or ["default"],
        "namespace_bindings": review.get("namespace_bindings") or {},
        "max_concurrent": review_pool.effective_max_concurrent(),
    }


def _write_review_section(patch: dict) -> dict:
    """Merge a partial review-settings patch into config.json atomically. Only
    the agent/model/effort/active_namespaces/namespace_bindings keys are writable; everything
    else in the config is preserved. Returns the resulting review section."""
    with _review_settings_lock():
        cfg_path = store.data_dir() / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if not isinstance(cfg, dict):
                cfg = {}
        except (json.JSONDecodeError, OSError, FileNotFoundError):
            store.ensure_layout()
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        review = cfg.get("review")
        if not isinstance(review, dict):
            review = {}

        if "agent" in patch:
            a = patch["agent"]
            # Empty/None clears the override (back to the dedicated reviewer).
            # A non-empty value must name a known agent: it becomes the review
            # worker's spawn identity, so raw request input must not reach it.
            previous_agent = review.get("agent") or None
            if not a:
                review["agent"] = None
            elif review_pool.is_known_review_agent(str(a)):
                review["agent"] = str(a)
            else:
                raise ValueError(f"unknown agent {str(a)!r}")
            # A model override was chosen from the previous agent's runtime and
            # can be one the new agent rejects (a Codex model on a Claude seat
            # fails the review at session start), so switching agents resets it
            # to the new agent's default unless this patch sets a model too.
            if review["agent"] != previous_agent and "model" not in patch:
                review["model"] = None

        if "model" in patch:
            m = patch["model"]
            # Empty/None clears the override (inherits the system/agent default model).
            # A non-empty value is validated against Sage's runtime snapshot before
            # persisting, since it later becomes a cli.json overlay
            # key for the review worker subprocess — raw request input must not reach it.
            if not m or str(m).lower() == "auto":
                review["model"] = None
            elif _valid_model(str(m)):
                review["model"] = str(m)
            else:
                raise ValueError(f"unknown or invalid model {str(m)!r}")
        if "effort" in patch:
            eff = str(patch["effort"]).lower()
            # "" = inherit the model/provider default; otherwise a concrete level.
            review["effort"] = eff if (eff == "" or eff in review_pool.VALID_EFFORTS) else ""
        if "active_namespaces" in patch:
            ns = patch["active_namespaces"]
            if isinstance(ns, list) and ns:
                avail = set(learning.list_namespaces())
                cleaned = [str(n) for n in ns if str(n) in avail]
                review["active_namespaces"] = cleaned or ["default"]
        if "namespace_bindings" in patch:
            bindings = learning.validate_namespace_bindings(patch["namespace_bindings"])
            unknown = sorted(set(bindings) - set(learning.list_namespaces()))
            if unknown:
                raise ValueError(
                    "namespace bindings reference unknown namespaces: " + ", ".join(unknown)
                )
            pinned = discovery.read_repos()
            allowed_repositories = {
                ("github", "github.com", str(item["owner"]).lower(), str(item["repo"]).lower())
                for item in pinned
            }
            unavailable = sorted(
                f"{binding['repository']['owner']}/{binding['repository']['repository']}"
                for binding in bindings.values()
                if binding["scope"] == "repository"
                and (
                    binding["repository"]["provider"],
                    binding["repository"]["host"],
                    binding["repository"]["owner"],
                    binding["repository"]["repository"],
                )
                not in allowed_repositories
            )
            if unavailable:
                raise ValueError(
                    "repository bindings must reference pinned Sage repositories: "
                    + ", ".join(unavailable)
                )
            review["namespace_bindings"] = bindings
        if "max_concurrent" in patch:
            # How many reviews run at once on the shared runtime. Clamped to
            # [1, MAX_CONCURRENT_CEIL] so "review all" can fan out without letting a
            # user set an unbounded value that would saturate the host.
            try:
                mc = int(patch["max_concurrent"])
            except (TypeError, ValueError):
                raise ValueError("max_concurrent must be an integer")
            review["max_concurrent"] = max(1, min(mc, review_pool.MAX_CONCURRENT_CEIL))

        cfg["review"] = review
        # Same helper, same reasons as ``_write_runs`` -- including the retrying
        # replace this write also lacked.
        atomic_write(cfg_path, json.dumps(cfg, indent=2), restrict_to_owner=True)
        return review


async def _handle_settings(request: web.Request) -> web.Response:
    """GET  -> {settings, models, agents, efforts, namespaces}
    PUT  body {agent?, model?, effort?, active_namespaces?, namespace_bindings?} -> {ok, settings}.
    """
    if request.method == "GET":
        # All of this is synchronous file IO (config read + namespaces dir walk +
        # reviewer_info file read) — offload to a thread so it never blocks the
        # shared gateway event loop.
        def _build_settings_response() -> dict:
            try:
                namespaces = learning.list_namespaces()
            except Exception:
                namespaces = ["default"]
            reviewer = None
            if hasattr(review_pool, "reviewer_info"):
                try:
                    reviewer = review_pool.reviewer_info()
                except Exception:
                    reviewer = None
            try:
                agents = review_pool.known_review_agents()
            except Exception:
                agents = []
            return {
                "settings": _load_review_section(),
                "models": list(reviewer.get("models") or []) if reviewer else [],
                "agents": agents,
                "efforts": (
                    list(review_pool.VALID_EFFORTS)
                    if reviewer and reviewer.get("effort_override_supported")
                    else []
                ),
                "namespaces": namespaces,
                "pinned_repos": discovery.read_repos(),
                "reviewer": reviewer,
                "max_concurrent_max": review_pool.MAX_CONCURRENT_CEIL,
            }

        return web.json_response(await asyncio.to_thread(_build_settings_response))
    # PUT
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.settings_write")
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # SEL audit for this write path: it changes security-relevant subprocess
    # config — the model/effort flow into the review worker's cli.json overlay,
    # and the active namespaces select which learnings the worker loads. Both the
    # applied write (success) AND a rejected attempt (denied, e.g. an invalid/
    # injected model) are audited. Offloaded (ledger does sync file IO); failure
    # is swallowed so it can never break the response.
    async def _audit_settings(outcome: str) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel  # circular import: sel->config->apps cycle

            sel().log_api_access(
                caller="code-review-sage",
                operation="update_review_settings",
                outcome=outcome,
                resources="config.json#review(" + ",".join(sorted(body)) + ")",
            )

        try:
            await asyncio.to_thread(_emit)
        except Exception as exc:
            logger.warning("SEL audit failed for update_review_settings (%s): %s", outcome, exc)

    try:
        review = await asyncio.to_thread(_write_review_section, body)
        await _audit_settings("success")
        return web.json_response(
            {
                "ok": True,
                "settings": {
                    "agent": review.get("agent") or None,
                    "model": review.get("model") or None,
                    "effort": review.get("effort", ""),
                    "active_namespaces": review.get("active_namespaces") or ["default"],
                    "namespace_bindings": review.get("namespace_bindings") or {},
                    "max_concurrent": review.get("max_concurrent")
                    or review_pool.effective_max_concurrent(),
                },
            }
        )
    except ValueError as exc:
        # Bad client input (e.g. unknown model) — a 4xx, not a server fault.
        # Audit the rejected attempt (security-relevant: invalid model injection).
        await _audit_settings("denied")
        return web.json_response(
            {"code": "invalid_request", "ok": False, "error": str(exc)}, status=400
        )
    except Exception as exc:
        corr = uuid.uuid4().hex[:12]
        logger.warning("settings write failed [%s]: %s", corr, exc, exc_info=True)
        return web.json_response(
            {"code": "internal_error", "ok": False, "error": "internal error", "id": corr},
            status=500,
        )
