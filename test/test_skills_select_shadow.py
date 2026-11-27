"""skills.select in shadow mode for crewmates and named seats (operator, 2026-11-27).

A crewmate's skills come from its own spec, so the picker only LOGS there: both
arms on one row marked ``shadow``, nothing injected, the turn never waits, and one
round at a time. Live rows carry the agent and harness too.
"""

import threading
import time
from test import test_decisions_points as _points
from unittest.mock import AsyncMock

import pytest

from kiro_crew import decisions as core
from kiro_crew.decisions.points import skills_select as sel

_answers, _loader = _points._answers, _points._loader
bg_loop, enabled = _points.bg_loop, _points.enabled


@pytest.fixture
def rows(monkeypatch):
    written = []
    monkeypatch.setattr(sel._log, "append", lambda row, **kw: written.append(row) or True)
    published = []
    monkeypatch.setattr(sel, "publish_outcome", lambda key, row: published.append(row) or True)
    return written, published


def _wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _extra(row):
    return row.get("extra") or row


def test_a_shadow_pick_logs_both_arms_and_injects_nothing(bg_loop, enabled, rows, monkeypatch):
    written, published = rows
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers(sel.NONE_OPTION)))
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.shadow_select(
        loader, "please code review this", None, session_key="s", loop=bg_loop,
        agent="plumb", harness="deepseek",
    ) is True
    assert _wait_for(lambda: written)
    row = _extra(written[0])
    assert row["shadow"] is True
    assert (row["agent"], row["harness"]) == ("plumb", "deepseek")
    assert row["baseline"] == ["review"] and row["jev"] == []
    assert published == [], "a shadow row has no strip to publish"


def test_the_turn_never_waits_on_a_shadow_pick(bg_loop, enabled, rows, monkeypatch):
    gate = threading.Event()

    async def slow(*a, **k):
        import asyncio

        await asyncio.get_running_loop().run_in_executor(None, gate.wait, 5)
        return _answers("review")

    monkeypatch.setattr(core, "decide", slow)
    loader = _loader(("review", {"triggers": "code review"}))
    started = time.monotonic()
    assert sel.shadow_select(loader, "code review", None, session_key="s", loop=bg_loop)
    assert time.monotonic() - started < 0.5
    # One round at a time: a second turn while the first waits is skipped.
    assert sel.shadow_select(loader, "code review", None, session_key="s", loop=bg_loop) is False
    gate.set()
    assert _wait_for(lambda: rows[0])
    assert _wait_for(lambda: sel._shadow_in_flight == 0)


def test_live_rows_carry_agent_and_harness_and_are_not_shadow(bg_loop, enabled, rows, monkeypatch):
    written, published = rows
    monkeypatch.setattr(core, "decide", AsyncMock(return_value=_answers("review")))
    loader = _loader(("review", {"triggers": "code review"}))
    picked = sel.selected_skills(
        loader, "code review", None, session_key="s", loop=bg_loop,
        agent="kirocrew", harness="claude",
    )
    assert picked == ["review"]
    row = _extra(written[0])
    assert (row["agent"], row["harness"], row["shadow"]) == ("kirocrew", "claude", False)
    assert len(published) == 1


def test_a_disabled_point_schedules_nothing(bg_loop, rows, monkeypatch):
    monkeypatch.setattr(core, "is_enabled", lambda *a, **k: False)
    spy = AsyncMock(return_value=_answers("review"))
    monkeypatch.setattr(core, "decide", spy)
    loader = _loader(("review", {"triggers": "code review"}))
    assert sel.shadow_select(loader, "code review", None, session_key="s", loop=bg_loop) is False
    spy.assert_not_awaited()
