"""Sage's bridge and the manifest-owned app tools at the MCP entry point."""

from __future__ import annotations

import json

import pytest

from kiro_crew import mcp_core
from kiro_crew.apps import app_agent_tools
from kiro_crew.apps import manager as apps_manager
from kiro_crew.mcp_tools import app_tools, apps, build_tool_names
from kiro_crew.validation import ValidationError

SAGE = "code_review_sage_api"
OWNERS = {
    "issue_radar_record_investigation": "issue-radar",
    "issue_radar_crew_read": "issue-radar",
    "issue_radar_crew_record": "issue-radar",
    "ops_mission_control_api": "ops-mission-control",
    "pod_up": "dev-fleet",
    "pod_restart": "dev-fleet",
    "pod_down": "dev-fleet",
    "pod_status": "dev-fleet",
    "pod_ls": "dev-fleet",
}


def _installed(enabled: bool) -> None:
    path = apps_manager.app_dir("code-review-sage") / apps_manager.INSTALLED_META_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": "code-review-sage", "enabled": enabled}), encoding="utf-8")


def _listed() -> set[str]:
    return {tool["name"] for tool in mcp_core._list_tools()}


def test_shipped_nine_use_the_manifest_domain() -> None:
    shipped = {tool.name: tool.app for tool in app_agent_tools.shipped_tools()}
    assert {name: shipped[name] for name in OWNERS} == OWNERS
    assert set(OWNERS) <= {tool["name"] for tool in app_tools.schemas()}
    assert set(OWNERS).isdisjoint(apps.HANDLERS)
    assert set(apps.HANDLERS) == {SAGE}
    assert SAGE in build_tool_names()
    assert SAGE not in {tool["name"] for tool in app_tools.schemas()}


def test_sage_listing_tracks_enablement() -> None:
    assert SAGE not in _listed()
    _installed(True)
    assert SAGE in _listed()
    _installed(False)
    assert SAGE not in _listed()
    assert SAGE in build_tool_names()


@pytest.mark.parametrize("state", [None, "error"])
def test_sage_listing_keeps_unreadable_enablement(monkeypatch, state) -> None:
    def read(_name):
        if state == "error":
            raise OSError("unreadable")
        return state

    monkeypatch.setattr(apps, "app_enabled_state", read)
    assert SAGE in _listed()


def test_names_only_does_not_read_sage_enablement(monkeypatch) -> None:
    def read(_name):
        raise AssertionError("unexpected enablement read")

    monkeypatch.setattr(apps, "app_enabled_state", read)
    assert SAGE in build_tool_names()


def test_sage_dispatch_and_refusal(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        mcp_core, "_get", lambda path: calls.append(path) or {"error": "app disabled"}
    )
    assert "app disabled" in mcp_core._call_tool(SAGE, {"method": "GET", "path": "/runs"})
    assert calls == ["/api/apps/code-review-sage/runs"]
    assert SAGE not in _listed()
    result = mcp_core._call_tool(SAGE, {"method": "GET", "path": "/settings"})
    assert result.startswith("Error")
    assert calls == ["/api/apps/code-review-sage/runs"]


@pytest.mark.parametrize(
    "args",
    [
        {"method": "POST", "path": "/runs"},
        {"method": "GET", "path": "/runs/../settings"},
        {"method": "POST", "path": "/review", "query": "x=1"},
        {"method": "GET", "path": "/runs", "body_json": "{}"},
    ],
)
def test_sage_validation_refuses_outside_contract(args) -> None:
    with pytest.raises(ValidationError):
        mcp_core._validate_args(SAGE, args)


def test_sage_post_scrubs_decoded_body_and_response(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        mcp_core, "_post", lambda path, body: calls.append((path, body)) or {"ok": True}
    )
    monkeypatch.setattr(apps, "redact", lambda value: value.replace("secret", "redacted"))
    result = mcp_core._call_tool(
        SAGE, {"method": "POST", "path": "/review", "body_json": '{"nested":"secret"}'}
    )
    assert calls == [("/api/apps/code-review-sage/review", {"nested": "redacted"})]
    assert '"ok": true' in result


@pytest.mark.parametrize("body", ["{", "[]", "null"])
def test_sage_refuses_invalid_body(monkeypatch, body) -> None:
    calls = []
    monkeypatch.setattr(mcp_core, "_post", lambda *args: calls.append(args))
    assert mcp_core._call_tool(
        SAGE, {"method": "POST", "path": "/review", "body_json": body}
    ).startswith("Error")
    assert calls == []
