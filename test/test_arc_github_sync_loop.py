"""The gateway's periodic arc PR sync: it keeps running after a failing pass."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from kiro_crew import arc_github_sync


@pytest.mark.asyncio
async def test_a_failing_pass_does_not_stop_the_loop(monkeypatch):
    calls = 0

    async def flaky_pass():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("gh is down")
        if calls >= 3:
            raise asyncio.CancelledError
        return {"arc_x": 1}

    monkeypatch.setattr(arc_github_sync, "_pass", flaky_pass)
    monkeypatch.setattr(arc_github_sync, "_FIRST_PASS_DELAY_S", 0)
    with pytest.raises(asyncio.CancelledError):
        await arc_github_sync._loop(interval=0)
    assert calls == 3


def test_the_gateway_starts_the_sync():
    from kiro_crew.dashboard import server

    assert "kick_arc_github_sync(state)" in inspect.getsource(server)
