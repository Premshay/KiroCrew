"""A Claude model window out stops that model only (2026-11-26).

Claude holds some weekly limits per model (``seven_day_opus``, ``seven_day_sonnet``,
a separate Fable limit). A refused model window used to read as the whole account
out, so one model's limit blocked every Claude model.
"""

import time

import pytest

from kiro_crew import vendor_limits as vl
from kiro_crew.decisions.points.spawn_route import drop_quota_outs
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


def _cands():
    return [
        {"key": "claude/fable", "vendor": "claude", "model": "fable"},
        {"key": "claude/opus", "vendor": "claude", "model": "opus"},
        {"key": "claude/pinned", "vendor": "claude", "model": "claude-fable-5-2"},
    ]


def test_a_refused_fable_window_keeps_the_account_usable(home):
    now = time.time()
    _read("seven_day", "allowed_warning", now, pct=0.4)
    _read("seven_day_fable", "rejected", now)
    assert vl.claude_health(now)["out"] is False
    assert quota_switch.detect("", vendor="claude", model="opus", now=now) is None


def test_a_refused_fable_window_drops_fable_only(home):
    now = time.time()
    _read("seven_day", "allowed", now, pct=0.4)
    _read("seven_day_fable", "rejected", now)
    outs = vl.quota_outs(now)
    assert "until" not in outs["claude"]
    kept = [c["key"] for c in drop_quota_outs(_cands(), outs)]
    assert kept == ["claude/opus"]


def test_quota_out_names_the_model_window(home):
    now = time.time()
    resets = now + 86400
    _read("seven_day_fable", "rejected", now, resets=resets)
    mark = vl.quota_out("claude", "claude-fable-5-2", now)
    assert mark["until"] == resets
    assert "seven_day_fable" in mark["detail"]
    assert vl.quota_out("claude", "opus", now) is None


def test_a_full_model_window_counts_as_out(home):
    now = time.time()
    _read("seven_day_opus", "allowed_warning", now, pct=0.99)
    assert set(vl.claude_model_outs(now)) == {"opus"}


def test_a_model_window_lifts_at_its_reset(home):
    now = time.time()
    _read("seven_day_fable", "rejected", now - 100, resets=now - 1)
    assert vl.claude_model_outs(now) == {}
    assert vl.quota_out("claude", "fable", now) is None


def test_a_later_allowed_model_reading_clears_it(home):
    now = time.time()
    _read("seven_day_fable", "rejected", now - 10)
    _read("seven_day_fable", "allowed", now, pct=0.2)
    assert vl.claude_model_outs(now) == {}


def test_a_refused_base_window_still_stops_every_model(home):
    now = time.time()
    _read("seven_day", "rejected", now)
    _read("seven_day_fable", "allowed", now)
    assert vl.claude_health(now)["out"] is True
