"""Descriptor and advertisement for the guarded Sage bridge."""

from __future__ import annotations

from typing import Any

from kiro_crew.apps.manager import app_enabled_state
from kiro_crew.mcp_tools.catalog import loaded_binding


def schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "code_review_sage_api",
            "description": (
                "Call the Code Review Sage app's HTTP API with the gateway's own "
                "credential. This is the ONLY way an agent session reaches that "
                "API: its routes are same-origin authed like /config, and a "
                "sandboxed seat cannot mint the owner token they expect — host "
                "provenance is proved by namespace identity, which a seat's own "
                "sandbox makes undeterminable, so the mint fails closed. Use it "
                "to fire a review and to read the runs it produced. Paths are "
                "rooted at the app base (pass '/review', not the full URL). "
                "Reachable: POST /review, GET /runs, GET /runs/<run_id>, "
                "GET /runs/<run_id>/report. Settings, repository configuration "
                "and the post/archive/cancel actions are deliberately not "
                "callable from here."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST"],
                        "description": "HTTP method",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "API path relative to /api/apps/code-review-sage. "
                            'POST: /review with body {"changes":["<PR url>"]}. '
                            "GET: /runs, /runs/<run_id>, /runs/<run_id>/report."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional query string without the leading '?', GET only "
                            "— e.g. 'limit=20' on /runs"
                        ),
                    },
                    "body_json": {
                        "type": "string",
                        "description": (
                            "JSON object for POST bodies, serialized as a string — "
                            'e.g. \'{"changes": ["https://github.com/o/r/pull/1"]}\' '
                            "for /review"
                        ),
                    },
                },
                "required": ["method", "path"],
            },
        }
    ]


def advertised() -> list[dict[str, Any]]:
    try:
        state = loaded_binding("kiro_crew.mcp_tools.apps", "app_enabled_state", app_enabled_state)(
            "code-review-sage"
        )
    except Exception:
        state = None
    return [] if state is False else schemas()
