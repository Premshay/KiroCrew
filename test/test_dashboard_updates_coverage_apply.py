"""The dashboard update apply, ``POST /api/update``, and overlay controls.

The update endpoint refuses during shutdown. The simulator and cancel endpoint
control the update overlay. Boot-time update coverage lives in the update tests.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from dashboard_updates_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_module_state,
    _request,
)

from kiro_crew.dashboard.handlers import updates


class TestApplyRefusals:
    """``POST /api/update`` refuses during shutdown."""

    @pytest.mark.asyncio
    async def test_refuses_once_the_gateway_is_shutting_down(self, monkeypatch):
        """An apply started now would be cut off mid-write by the shutdown."""
        from types import SimpleNamespace

        from kiro_crew.platform import update_provider

        monkeypatch.setattr(updates, "shutdown_event", SimpleNamespace(is_set=lambda: True))
        applied = AsyncMock()
        monkeypatch.setattr(update_provider, "apply_policy_update", applied)

        resp = await updates.api_update_apply(_request({}))

        assert resp.status == 503
        assert json.loads(resp.body.decode())["code"] == "shutting_down"
        applied.assert_not_awaited()


class TestSimulateAndCancel:
    """The two overlay-driving endpoints."""

    @pytest.mark.asyncio
    async def test_a_missing_body_is_treated_as_empty_not_an_error(self, monkeypatch):
        """The simulator is a local test aid; an absent body must not 400."""
        req = _request(ValueError("no body"))
        state = req.app["state"]
        resp = await updates.api_update_simulate(req)
        assert resp.status == 200
        assert json.loads(resp.body.decode())["status"] == "simulating"
        for task in list(state._background_tasks):
            task.cancel()

    @pytest.mark.asyncio
    async def test_a_simulated_rejection_uses_the_caller_s_message(self):
        resp = await updates.api_update_simulate(
            _request({"reject": True, "reject_message": "nope"})
        )
        assert resp.status == 409
        assert json.loads(resp.body.decode())["error"] == "nope"

    @pytest.mark.asyncio
    async def test_cancel_clears_the_overlay_on_both_sides_of_the_failed_event(self):
        req = _request({})
        state = req.app["state"]
        resp = await updates.api_update_cancel(req)
        assert resp.status == 200
        # Cleared, then a "failed" event so clients drop the overlay, then cleared
        # again -- a single clear would race clients that had not yet polled.
        assert state.clear_update_progress.call_count == 2
        state.push_update_progress.assert_called_once_with("failed", "Update cancelled by user")
