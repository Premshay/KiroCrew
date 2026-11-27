"""A newer live Antigravity quota probe overrides a stale quota-out mark (2026-11-21).

A refused turn on 11-20 marked every Gemini model out for 138 h; the live probe the
next day showed ~92% left and a run on that vendor finished, yet spawn.route kept
dropping every Antigravity candidate. A probe newer than the mark that shows room on
a model now clears the mark for that model.
"""

import json
import time

import pytest

from kiro_crew import vendor_limits as vl


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "_data_home", lambda: tmp_path)
    vl._reset_for_tests()
    return tmp_path


def _probe(home, as_of, remaining):
    (home / vl.ANTIGRAVITY_QUOTA_NAME).write_text(
        json.dumps(
            {
                "as_of": as_of,
                "models": {
                    m: {"group": "Gemini", "remaining_fraction": r, "window": "weekly"}
                    for m, r in remaining.items()
                },
            }
        )
    )


def _mark(now):
    vl.note_quota_out(
        "antigravity",
        until=now + 5 * 86400,
        model="gemini-4-hydrogen-high",
        detail="quota",
        now=now,
    )


def test_newer_probe_with_room_clears_the_mark(home):
    now = time.time()
    _probe(home, now - 10, {"gemini-4-hydrogen-high": 0.0, "gemini-4-neon-high": 0.0})
    _mark(now - 5)
    assert "antigravity" in vl.quota_outs(now)
    _probe(home, now, {"gemini-4-hydrogen-high": 0.9, "gemini-4-neon-high": 0.9})
    assert "antigravity" not in vl.quota_outs(now)
    assert vl.quota_out("antigravity", "gemini-4-hydrogen-high", now) is None


def test_older_probe_does_not_clear_the_mark(home):
    now = time.time()
    _probe(home, now - 100, {"gemini-4-hydrogen-high": 0.9})
    _mark(now - 5)
    assert vl.quota_outs(now)["antigravity"]["models"]["gemini-4-hydrogen-high"] > now


def test_newer_probe_still_empty_keeps_the_mark(home):
    now = time.time()
    _probe(home, now - 100, {"gemini-4-hydrogen-high": 0.0})
    _mark(now - 5)
    _probe(home, now, {"gemini-4-hydrogen-high": 0.0})
    assert vl.quota_out("antigravity", "gemini-4-hydrogen-high", now) is not None


def test_no_probe_keeps_the_mark(home):
    now = time.time()
    _mark(now - 5)
    assert "antigravity" in vl.quota_outs(now)


def test_other_vendors_are_untouched(home):
    now = time.time()
    _probe(home, now, {"gemini-4-hydrogen-high": 0.9})
    vl.note_quota_out("claude", until=now + 3600, detail="limit", now=now - 5)
    assert vl.quota_outs(now)["claude"]["until"] > now


def test_newer_claude_reading_not_limited_clears_claude_mark(home):
    # Two Claude accounts: one hit its limit, the other keeps serving turns.
    now = time.time()
    vl.note_quota_out("claude", until=now + 5 * 3600, detail="rate_limit", now=now - 60)
    assert vl.quota_outs(now)["claude"]["until"] > now
    vl.note_claude_rate_limit({"status": "allowed", "rateLimitType": "seven_day"}, now=now)
    assert "claude" not in vl.quota_outs(now)
    assert vl.quota_out("claude", now=now) is None


def test_newer_claude_reading_rejected_keeps_claude_mark(home):
    now = time.time()
    vl.note_quota_out("claude", until=now + 5 * 3600, detail="rate_limit", now=now - 60)
    vl.note_claude_rate_limit(
        {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": now + 3600}, now=now
    )
    assert vl.quota_outs(now)["claude"]["until"] > now


def test_codex_reading_newer_and_not_out_clears_codex_mark(home, monkeypatch):
    now = time.time()
    vl.note_quota_out("codex", until=now + 86400, detail="usage limit", now=now - 60)
    monkeypatch.setattr(
        vl, "codex_health", lambda reading=None: {"out": False, "as_of": now, "used_pct": 40.0}
    )
    assert "codex" not in vl.quota_outs(now)


def test_a_group_mark_covers_a_model_added_to_the_group_later(home):
    """2026-11-27: a Gemini group mark listed its members before Carbon existed, so it
    dropped Hydrogen and Neon and left Carbon as the only Antigravity offer."""
    now = time.time()
    _probe(home, now - 10, {"gemini-4-hydrogen-high": 0.0, "gemini-4-neon-high": 0.0})
    _mark(now - 5)
    _probe(
        home,
        now - 1,
        {"gemini-4-hydrogen-high": 0.0, "gemini-4-neon-high": 0.0, "gemini-4-carbon-high": 0.0},
    )
    models = vl.quota_outs(now)["antigravity"]["models"]
    assert "gemini-4-carbon-high" in models
    assert vl.quota_out("antigravity", "gemini-4-carbon-high", now) is not None


def test_a_new_group_member_with_room_is_not_covered(home):
    now = time.time()
    _probe(home, now - 10, {"gemini-4-hydrogen-high": 0.0})
    _mark(now - 5)
    _probe(home, now, {"gemini-4-hydrogen-high": 0.9, "gemini-4-carbon-high": 0.9})
    assert vl.quota_out("antigravity", "gemini-4-carbon-high", now) is None
