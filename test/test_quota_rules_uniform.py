"""One quota model for every vendor (operator ruling of 2026-11-21).

Every reader fills one normalized shape (vendor -> accounts -> windows or models ->
``{used_pct, resets_at, as_of, status}``), one rule set (``decisions.quota_limits``:
``low_pct`` 50, ``out_pct`` 95, ``rejected`` is out) judges it, a vendor is out only
when every account is, and quota-out marks are keyed by vendor + account + model group.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import quota_rules as qr
from kiro_crew import vendor_limits as vl
from kiro_crew.config.sections_decisions import DecisionsConfig, coerce_quota_limits
from kiro_crew.decisions.points import spawn_route as sr

NOW = 1_795_170_000.0
RULES = qr.QuotaRules()
#: The real reader and rules lookup, kept before the fixture swaps them out.
REAL_CODEX_QUOTA = vl.codex_quota
REAL_RULES = qr.rules
FAR = 9e12


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "_data_home", lambda: tmp_path)
    # No host Codex rollout or live config: both would leak into the readings under test.
    monkeypatch.setattr(vl, "codex_health", lambda reading=None: {})
    monkeypatch.setattr(vl, "codex_quota", lambda reading=None: {})
    monkeypatch.setattr(qr, "rules", lambda config=None: qr.DEFAULT_RULES)
    vl._reset_for_tests()
    yield
    vl._reset_for_tests()


def _agy(tmp_path, rows, as_of=NOW):
    (tmp_path / vl.ANTIGRAVITY_QUOTA_NAME).write_text(
        json.dumps({"as_of": as_of, "models": rows}), encoding="utf-8"
    )


def _claude(status, pct=None, window="five_hour", resets=NOW + 3600, now=NOW, account="default"):
    info = {"status": status, "rateLimitType": window, "resetsAt": resets}
    if pct is not None:
        info["utilization"] = pct / 100.0
    vl.note_claude_rate_limit(info, now=now, account=account)


# ---------------------------------------------------------------------------
# The normalized shape
# ---------------------------------------------------------------------------


class TestShape:
    def test_claude_fills_accounts_windows(self):
        _claude("allowed", 42)
        shape = vl.claude_quota()
        assert shape["source"] == "claude_rate_limit_event"
        reading = shape["accounts"]["default"]["windows"]["five_hour"]
        assert reading == {
            "status": "allowed",
            "used_pct": 42.0,
            "resets_at": NOW + 3600,
            "as_of": NOW,
        }

    def test_codex_fills_accounts_windows(self):
        # The real reader, fed a reading (never the host's rollout).
        shape = REAL_CODEX_QUOTA(
            {
                "five_hour": {"used_percent": 12.34, "resets_at": NOW + 60, "stale": False},
                "weekly": {"used_percent": 99.0, "resets_at": NOW - 1, "stale": True},
                "captured_at": NOW,
            }
        )
        assert shape == {
            "source": "codex_rollout",
            "accounts": {
                "default": {
                    "windows": {
                        "five_hour": {"used_pct": 12.3, "resets_at": NOW + 60, "as_of": NOW}
                    }
                }
            },
        }

    def test_antigravity_fills_accounts_models(self, tmp_path):
        _agy(tmp_path, {"m1": {"remaining_fraction": 0.25, "group": "g"}})
        assert vl.antigravity_quota() == {
            "source": "agy_quota",
            "accounts": {
                "default": {"models": {"m1": {"used_pct": 75.0, "as_of": NOW, "group": "g"}}}
            },
        }

    def test_snapshot_lists_only_vendors_with_a_reading(self, tmp_path):
        _claude("allowed", 10)
        assert set(vl.quota_snapshot()) == {"claude"}


# ---------------------------------------------------------------------------
# One rule set, every vendor
# ---------------------------------------------------------------------------


class TestUniformThresholds:
    def test_defaults_live_in_one_config_place(self):
        assert DecisionsConfig().quota_limits == {"low_pct": 50.0, "out_pct": 95.0}
        assert qr.QuotaRules().as_state() == {"low_pct": 50.0, "out_pct": 95.0}

    @pytest.mark.parametrize(
        "raw,want",
        [
            (None, {"low_pct": 50.0, "out_pct": 95.0}),
            ({"low_pct": 60}, {"low_pct": 60.0, "out_pct": 95.0}),
            ({"low_pct": "x", "out_pct": 200}, {"low_pct": 50.0, "out_pct": 100.0}),
            ({"low_pct": 90, "out_pct": 80}, {"low_pct": 50.0, "out_pct": 95.0}),
        ],
    )
    def test_config_is_normalized(self, raw, want):
        assert coerce_quota_limits(raw) == want

    def test_rules_read_the_config(self):
        cfg = SimpleNamespace(
            decisions=SimpleNamespace(quota_limits={"low_pct": 30, "out_pct": 80})
        )
        assert REAL_RULES(cfg) == qr.QuotaRules(30.0, 80.0)
        assert REAL_RULES(SimpleNamespace(decisions=None)) == qr.DEFAULT_RULES

    def test_out_pct_is_out_for_claude_codex_and_antigravity(self, tmp_path):
        _claude("allowed", 96)
        assert vl.claude_health(NOW)["out"] is True
        codex = qr.vendor_view(
            {"accounts": {"default": {"windows": {"weekly": {"used_pct": 96.0}}}}}, RULES
        )
        assert codex["out"] is True
        _agy(tmp_path, {"m1": {"remaining_fraction": 0.04}})
        assert vl.antigravity_health(now=NOW)["out"] is True

    def test_below_out_pct_is_not_out_anywhere(self, tmp_path):
        _claude("allowed_warning", 94)
        assert vl.claude_health(NOW)["out"] is False
        _agy(tmp_path, {"m1": {"remaining_fraction": 0.06}})
        assert vl.antigravity_health(now=NOW)["out"] is False

    def test_rejected_is_out_whatever_the_percentage(self):
        _claude("rejected", 10)
        h = vl.claude_health(NOW)
        assert (h["out"], h["used_pct"], h["status"]) == (True, 100.0, "rejected")

    def test_a_refused_window_binds_over_a_percentage_out_one(self):
        _claude("allowed", 97, window="seven_day", resets=NOW + 86400)
        _claude("rejected", None, window="five_hour", resets=NOW + 600)
        assert vl.claude_health(NOW)["status"] == "rejected"

    def test_low_is_one_rule(self):
        assert qr.is_low({"used_pct": 50.0}, RULES) and not qr.is_low({"used_pct": 49.9}, RULES)
        assert not qr.is_low({}, RULES)
        assert qr.is_low({"used_pct": 30}, qr.QuotaRules(low_pct=30))
        assert sr._is_low({"used_pct": 50.0})

    def test_custom_thresholds_apply_to_every_reader(self, tmp_path):
        strict = qr.QuotaRules(low_pct=20, out_pct=80)
        _claude("allowed", 85)
        assert vl.claude_health(NOW, rules=strict)["out"] is True
        assert vl.claude_health(NOW, rules=RULES)["out"] is False
        _agy(tmp_path, {"m1": {"remaining_fraction": 0.15}})
        assert vl.antigravity_health(now=NOW, rules=strict)["out"] is True

    def test_a_96_pct_claude_reading_is_not_a_quota_hit(self):
        from kiro_crew.subagent_manager import quota_switch as qs

        _claude("allowed", 96)
        assert qs.detect("failed (refusal)", vendor="claude", now=NOW) is None


# ---------------------------------------------------------------------------
# Accounts aggregation
# ---------------------------------------------------------------------------


class TestAccounts:
    def test_out_only_when_every_account_is(self):
        views = {"a": {"out": True, "used_pct": 100.0}, "b": {"out": False, "used_pct": 70.0}}
        got = qr.aggregate_accounts(views)
        assert (got["out"], got["used_pct"], got["account"]) == (False, 70.0, "b")

    def test_used_pct_is_the_best_account(self):
        views = {"a": {"out": False, "used_pct": 80.0}, "b": {"out": False, "used_pct": 20.0}}
        assert qr.aggregate_accounts(views)["used_pct"] == 20.0

    def test_all_out_binds_on_the_first_reset(self):
        views = {
            "a": {"out": True, "used_pct": 100.0, "resets_at": NOW + 500},
            "b": {"out": True, "used_pct": 100.0, "resets_at": NOW + 100},
        }
        got = qr.aggregate_accounts(views)
        assert (got["out"], got["resets_at"]) == (True, NOW + 100)

    def test_unknown_accounts_are_skipped(self):
        assert qr.aggregate_accounts({"a": {}, "b": {}}) == {}
        assert qr.aggregate_accounts({"a": {"out": True}, "b": {}})["out"] is True

    def test_per_model_takes_each_models_best_account(self):
        views = {
            "a": {"out": False, "used_pct": 90.0, "per_model": {"m1": 90.0, "m2": 10.0}},
            "b": {"out": False, "used_pct": 40.0, "per_model": {"m1": 40.0, "m2": 60.0}},
        }
        assert qr.aggregate_accounts(views)["per_model"] == {"m1": 40.0, "m2": 10.0}

    def test_claude_second_account_keeps_the_vendor_usable(self):
        _claude("rejected", None, account="default")
        _claude("allowed", 30, account="work")
        h = vl.claude_health(NOW)
        assert (h["out"], h["used_pct"], h["account"]) == (False, 30.0, "work")
        assert set(h["accounts"]) == {"default", "work"}

    def test_single_account_health_is_unchanged(self):
        _claude("allowed", 42)
        assert set(vl.claude_health(NOW)) == {
            "out",
            "used_pct",
            "window",
            "resets_at",
            "status",
            "source",
            "as_of",
        }

    def test_default_account_store_layout_is_kept(self, tmp_path):
        _claude("allowed", 42)
        _claude("allowed", 10, account="work")
        raw = json.loads((tmp_path / vl.STORE_NAME).read_text())
        assert raw["claude"]["five_hour"]["used_pct"] == 42.0
        assert raw["_accounts"]["claude"]["work"]["five_hour"]["used_pct"] == 10.0


# ---------------------------------------------------------------------------
# Quota-out marks: vendor + account + model group
# ---------------------------------------------------------------------------


class TestMarks:
    def test_mark_key_is_vendor_account_group(self, tmp_path):
        _agy(tmp_path, {"h1": {"remaining_fraction": 0.5, "group": "hyd"}, "h2": {"group": "hyd"}})
        mark = vl.note_quota_out("antigravity", until=NOW + 100, model="h1", now=NOW)
        assert (mark["account"], mark["group"]) == ("default", "hyd")
        raw = json.loads((tmp_path / vl.STORE_NAME).read_text())
        assert raw["_marks"]["antigravity"]["default"]["hyd"]["until"] == NOW + 100
        vl.note_quota_out("codex", until=NOW + 100, model="gpt", now=NOW, account="work")
        raw = json.loads((tmp_path / vl.STORE_NAME).read_text())
        assert raw["_marks"]["codex"]["work"][""]["until"] == NOW + 100

    def test_vendor_out_only_when_every_account_is_marked(self):
        vl.note_quota_out("codex", until=NOW + 100, now=NOW, account="a")
        vl.note_quota_out("codex", until=NOW + 50, now=NOW, account="b")
        assert vl.quota_outs(now=NOW)["codex"] == {"until": NOW + 50}
        assert vl.quota_out("codex", now=NOW)["account"] == "b"
        assert vl.quota_out("codex", now=NOW + 60) is None  # b is back
        assert vl.quota_out("codex", now=NOW + 60, account="a")["until"] == NOW + 100

    def test_an_account_with_a_live_reading_and_no_mark_keeps_the_vendor(self):
        vl.note_quota_out("claude", until=NOW + 100, now=NOW, account="a")
        # Reconciliation reads at the wall clock, so the reading must not have reset.
        _claude("allowed", 20, now=NOW - 10, account="b", resets=FAR)
        assert "claude" not in vl.quota_outs(now=NOW)
        assert vl.quota_out("claude", now=NOW) is None
        assert vl.quota_out("claude", now=NOW, account="a") is not None

    def test_reconciliation_is_per_account(self):
        vl.note_quota_out("claude", until=NOW + 100, now=NOW - 60, account="a")
        vl.note_quota_out("claude", until=NOW + 100, now=NOW - 60, account="b")
        assert vl.quota_outs(now=NOW)["claude"]["until"] == NOW + 100
        # A newer reading on account b that is not out clears b's mark, not a's.
        _claude("allowed", 10, now=NOW, account="b", resets=FAR)
        assert vl.quota_out("claude", now=NOW, account="b") is None
        assert vl.quota_out("claude", now=NOW, account="a") is not None
        assert "claude" not in vl.quota_outs(now=NOW)

    def test_a_newer_reading_at_out_pct_keeps_the_mark(self):
        vl.note_quota_out("claude", until=NOW + 100, now=NOW - 60)
        _claude("allowed", 96, now=NOW, resets=FAR)
        assert vl.quota_outs(now=NOW)["claude"]["until"] == NOW + 100

    def test_antigravity_room_uses_out_pct(self, tmp_path):
        _agy(tmp_path, {"h1": {"remaining_fraction": 0.0, "group": "hyd"}}, as_of=NOW - 100)
        vl.note_quota_out("antigravity", until=NOW + 100, model="h1", now=NOW - 50)
        _agy(tmp_path, {"h1": {"remaining_fraction": 0.03, "group": "hyd"}}, as_of=NOW)
        assert vl.quota_outs(now=NOW)["antigravity"]["models"] == {"h1": NOW + 100}
        _agy(tmp_path, {"h1": {"remaining_fraction": 0.5, "group": "hyd"}}, as_of=NOW)
        assert "antigravity" not in vl.quota_outs(now=NOW)

    def test_model_marks_need_every_account(self):
        # No probe: a probe's own (default) account would be a third, unmarked account.
        vl.note_quota_out("antigravity", until=NOW + 100, model="h1", now=NOW, account="a")
        vl.note_quota_out("antigravity", until=NOW + 80, model="h1", now=NOW, account="b")
        assert vl.quota_outs(now=NOW + 1)["antigravity"]["models"] == {"h1": NOW + 80}

    def test_a_pre_account_store_is_migrated(self, tmp_path):
        (tmp_path / vl.STORE_NAME).write_text(
            json.dumps(
                {
                    "claude": {"five_hour": {"status": "allowed", "used_pct": 5.0, "as_of": 1}},
                    "_outs": {
                        "codex": {"vendor": {"until": NOW + 100, "as_of": NOW, "model": ""}},
                        "antigravity": {
                            "models": {"h1": {"until": NOW + 70, "as_of": NOW, "model": "h1"}}
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        vl._reset_for_tests()
        outs = vl.quota_outs(now=NOW)
        assert outs["codex"] == {"until": NOW + 100}
        assert outs["antigravity"] == {"models": {"h1": NOW + 70}}
        vl.note_quota_out("claude", until=NOW + 10, now=NOW)  # any write persists the new layout
        raw = json.loads((tmp_path / vl.STORE_NAME).read_text())
        assert "_outs" not in raw and raw["_marks"]["codex"]["default"][""]["until"] == NOW + 100


# ---------------------------------------------------------------------------
# The thresholds ride to the decision server
# ---------------------------------------------------------------------------


class TestStateLimits:
    def test_build_state_carries_the_limits(self):
        state = sr.build_state("b", vendor="claude", parent_model="", cands=[], health={})
        assert state["limits"] == {"low_pct": 50.0, "out_pct": 95.0}

    def test_build_state_takes_explicit_limits(self):
        state = sr.build_state(
            "b",
            vendor="claude",
            parent_model="",
            cands=[],
            health={},
            limits={"low_pct": 40.0, "out_pct": 90.0},
        )
        assert state["limits"] == {"low_pct": 40.0, "out_pct": 90.0}
