"""Read-only repository recommendations for Setup."""

from __future__ import annotations

import asyncio
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
