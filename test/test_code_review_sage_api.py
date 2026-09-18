"""The ``code_review_sage_api`` agent bridge.

An agent session cannot mint the owner token the Sage routes expect: host
provenance is proved by comparing namespaces with the gateway, and a seat's own
sandbox makes that comparison undeterminable, so the mint fails closed. The app
is therefore reachable from a session only through this tool, which calls it with
the gateway's own credential. These tests pin the surface it admits -- the review
lifecycle and nothing else.
"""

from __future__ import annotations

import pytest

from kiro_crew.mcp_tools import apps
from kiro_crew.validation import (
    CODE_REVIEW_SAGE_API_SCHEMA,
    MCP_CORE_SCHEMAS,
    code_review_sage_call_allowed,
)


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/review"),
        ("GET", "/runs"),
        ("GET", "/runs/0af7e5c68b85"),
        ("GET", "/runs/0af7e5c68b85/report"),
    ],
)
def test_admits_the_review_lifecycle(method: str, path: str) -> None:
    assert code_review_sage_call_allowed(method, path) is True


@pytest.mark.parametrize(
    "method, path",
    [
        # Settings, repository configuration and the human-decision routes are
        # off-surface by design.
        ("GET", "/settings"),
        ("PUT", "/settings"),
        ("GET", "/repos"),
        ("POST", "/repos"),
        ("DELETE", "/runs/0af7e5c68b85"),
        ("POST", "/runs/0af7e5c68b85/post"),
        ("POST", "/runs/0af7e5c68b85/cancel"),
        ("POST", "/runs/0af7e5c68b85/archive"),
        # A different verb on an allowed path is a different call.
        ("GET", "/review"),
        ("DELETE", "/runs"),
        # A dots-only segment must never match: the HTTP client would resolve it
        # to a route this allowlist did not admit.
        ("GET", "/runs/.."),
        ("GET", "/runs/."),
        ("GET", "/runs/../report"),
        # No nested or trailing segments.
        ("GET", "/runs/a/b/c"),
        ("GET", "/runs/0af7e5c68b85/report/extra"),
        # Non-string input cannot be admitted.
        ("GET", None),
        (None, "/runs"),
    ],
)
def test_refuses_every_off_surface_call(method: object, path: object) -> None:
    assert code_review_sage_call_allowed(method, path) is False


def test_handler_refuses_before_it_reaches_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verdict comes from the allowlist, not from the app's answer."""

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a refused call must not reach the app")

    monkeypatch.setattr(apps.mcp_core, "_get", _explode)
    monkeypatch.setattr(apps.mcp_core, "_post", _explode)
    out = apps.code_review_sage_api(
        "code_review_sage_api", {"method": "GET", "path": "/settings"}
    )
    assert out.startswith("Error:")
    assert "/settings" in out


def test_the_tool_is_advertised_and_schema_validated() -> None:
    assert "code_review_sage_api" in [t["name"] for t in apps.schemas()]
    assert MCP_CORE_SCHEMAS["code_review_sage_api"] is CODE_REVIEW_SAGE_API_SCHEMA
