"""A rejected Claude overage bucket is not out of quota (2026-11-26).

``seven_day_overage_included: rejected`` arrived at 06:56 while ``seven_day`` still
read ``allowed_warning``. Every reading counted, so Claude read out, a failed run
wrote an account-wide mark until the weekly reset, and the allowed weekly readings
after it could not clear the mark. Only base windows judge out.
"""

import time

import pytest

from kiro_crew import vendor_limits as vl
from kiro_crew.subagent_manager import quota_switch


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "_data_home", lambda: tmp_path)
    vl._reset_for_tests()
    yield tmp_path
    vl._reset_for_tests()


def _read(window, status, now, pct=None, resets=None):
    info = {"status": status, "rateLimitType": window, "resetsAt": resets or now + 3 * 86400}
    if pct is not None:
        info["utilization"] = pct
    vl.note_claude_rate_limit(info, now=now)


def test_overage_rejected_beside_an_allowed_week_is_not_out(home):
    now = time.time()
    _read("seven_day", "allowed_warning", now - 10, pct=0.89)
    _read("seven_day_overage_included", "rejected", now)
    health = vl.claude_health(now)
    assert health["out"] is False
    assert health["window"] == "seven_day"
    assert quota_switch.detect("", vendor="claude", model="fable", now=now) is None


def test_a_rejected_base_window_is_still_out(home):
    now = time.time()
    _read("seven_day", "rejected", now)
    assert vl.claude_health(now)["out"] is True
    assert quota_switch.detect("", vendor="claude", model="fable", now=now) is not None


def test_only_overage_readings_are_unknown_not_out(home):
    now = time.time()
    _read("seven_day_overage_included", "rejected", now)
    assert vl.claude_health(now) == {}


def test_a_later_allowed_reading_clears_the_mark(home):
    now = time.time()
    _read("seven_day_overage_included", "rejected", now - 20)
    vl.note_quota_out("claude", until=now + 86400, model="fable", detail="x", now=now - 10)
    assert vl.quota_out("claude", "fable", now) is not None
    _read("seven_day", "allowed_warning", now, pct=0.89)
    assert vl.quota_out("claude", "fable", now) is None
    assert "claude" not in vl.quota_outs(now)


def test_an_older_allowed_reading_keeps_the_mark(home):
    now = time.time()
    _read("seven_day", "allowed_warning", now - 20, pct=0.89)
    vl.note_quota_out("claude", until=now + 86400, model="fable", detail="x", now=now - 10)
    _read("seven_day_overage_included", "rejected", now)
    assert vl.quota_out("claude", "fable", now) is not None
