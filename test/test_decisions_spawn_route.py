"""``spawn.route``: vendor health from tombstones, the offered candidates, and the
refusal-shaped answer a model-less spawn gets.

What is held here:

* A limit hit takes the vendor out until the reset the error names (a clock, a
  date) or the 5-hour window when it names none -- and a later DELIVERY clears it
  whatever the named reset said, because the tombstones show deliveries resuming
  well before a week-out date.
* Only the child's vendor's configured tiers are offered; no tier is hardcoded.
* Every refusal -- no consent, no candidates, an out-of-domain answer, a provider
  failure -- is ``None``, which the run path reads as "keep the seat's model".
* The provider's ``note`` survives as the child's card line.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from kiro_crew.config.sections import (
    DECISION_PROVIDER_ENDPOINT_DEFAULT,
    DecisionsConfig,
    coerce_spawn_route,
)
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.points import spawn_route as sr
from kiro_crew.decisions.types import Answer

#: Captured before any fixture stubs it, so the advertised-tier tests run the real one.
REAL_ADVERTISED_TIERS = sr.advertised_tiers

TIERS = {
    "codex": {"large": ["gpt-5.6-sol"], "small": ["gpt-5.6-terra"]},
    "claude": {"large": [], "small": ["claude-sonnet-5"]},
}


def _config(*, bucket: int = 100, spawn_route: dict | None = None):
    return SimpleNamespace(
        decisions=DecisionsConfig(
            bucket=bucket, spawn_route=TIERS if spawn_route is None else spawn_route
        )
    )


@pytest.fixture(autouse=True)
def consent(tmp_path, monkeypatch):
    path = tmp_path / "decisions_consent.json"
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: path)

    def _set(value, endpoint=DECISION_PROVIDER_ENDPOINT_DEFAULT):
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps({"enabled": value, "endpoint": endpoint}), encoding="utf-8")

    _set(True)
    return _set


@pytest.fixture(autouse=True)
def quiet_log(tmp_path, monkeypatch):
    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")


@pytest.fixture(autouse=True)
def no_advertised(monkeypatch):
    """No adapter list by default, so the config pins alone are the candidates."""
    monkeypatch.setattr(sr, "advertised_tiers", lambda vendor, advertised=None: {})


@pytest.fixture(autouse=True)
def no_tombstones(monkeypatch):
    """No registry read by default; a test that wants health installs its own rows."""
    monkeypatch.setattr(sr, "_health_cache", None)
    monkeypatch.setattr(sr, "load_tombstones", lambda root=None: [])


@pytest.fixture
def snapshot(monkeypatch):
    import kiro_crew.decisions.gate as gate_mod

    def _set(cfg):
        monkeypatch.setattr(gate_mod, "_snapshot", lambda: cfg)
        return cfg

    _set(_config())
    return _set


class _Oracle:
    def __init__(self, value: str, p: float = 0.9, note: str = "survey/simple small") -> None:
        self.value, self.p, self.note = value, p, note
        self.states: list = []
        self.questions: list = []

    async def ask(self, state, questions):
        self.states.append(state)
        self.questions.append(questions)
        return {
            q.id: Answer(id=q.id, value=self.value, p=self.p, note=self.note) for q in questions
        }


@pytest.fixture
def install_oracle(monkeypatch):
    import kiro_crew.decisions.impl_jev as impl_mod

    def _install(oracle):
        monkeypatch.setattr(impl_mod, "JevOracle", lambda provider: oracle)
        return oracle

    return _install


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Vendor health
# ---------------------------------------------------------------------------


def _local(y, mo, d, h, mi) -> float:
    return dt.datetime(y, mo, d, h, mi).astimezone().timestamp()


def _hit(vendor_agent: str, died: float, detail: str, **extra) -> dict:
    return {
        "id": "x",
        "agent": vendor_agent,
        "died": died,
        "started": died - 10,
        "cause": "error",
        "recovery_action": "pending",
        "detail": detail,
        **extra,
    }


def _delivered(vendor_agent: str, died: float) -> dict:
    return {
        "id": "d",
        "agent": vendor_agent,
        "died": died,
        "started": died - 60,
        "cause": "delivered",
        "recovery_action": "delivered",
    }


class TestVendorHealth:
    def test_a_named_clock_is_the_reset_on_the_same_day(self):
        died = _local(2026, 9, 10, 11, 35)
        rows = [
            _hit(
                "crew-codex",
                died,
                "AcpError: {'message': \"You've hit your usage limit for GPT-5.3-Codex-Spark. "
                "Switch to another model now, or try again at 4:20 PM.\", 'codexErrorInfo': 'usageLimitExceeded'}",
            )
        ]
        h = sr.vendor_health(rows, now=died + 60)
        assert h["codex"]["out"] is True
        assert h["codex"]["until"] == _local(2026, 9, 10, 16, 20)
        assert h["codex"]["models"] == ["GPT-5.3-Codex-Spark"]
        assert sr.vendor_health(rows, now=_local(2026, 9, 10, 16, 21))["codex"]["out"] is False

    def test_a_named_date_is_the_reset(self):
        died = _local(2026, 9, 26, 0, 12)
        rows = [
            _hit(
                "crew-codex",
                died,
                "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
                "to purchase more credits or try again at Oct 1st, 2026 9:19 AM.",
            )
        ]
        h = sr.vendor_health(rows, now=died + 3600)
        assert h["codex"]["out"] is True
        assert h["codex"]["until"] == _local(2026, 10, 1, 9, 19)
        # a week-out reset is an upper bound: one probe may go after the 5-h window
        assert h["codex"]["probe_after"] == died + sr.PROBE_AFTER_S

    def test_no_named_time_is_the_five_hour_window(self):
        died = _local(2026, 9, 9, 22, 41)
        rows = [
            _hit(
                "crew-claude-atlas",
                died,
                "AcpError: {'errorKind': 'rate_limit'}. Retrying will not help until the limit resets.",
            )
        ]
        h = sr.vendor_health(rows, now=died + 60)
        assert h["claude"]["out"] is True and h["claude"]["until"] == died + sr.DEFAULT_WINDOW_S
        assert sr.vendor_health(rows, now=died + sr.DEFAULT_WINDOW_S + 1)["claude"]["out"] is False

    def test_a_later_delivery_clears_the_outage_before_the_named_reset(self):
        died = _local(2026, 9, 18, 12, 50)
        rows = [
            _hit(
                "crew-codex",
                died,
                "You've hit your usage limit. Visit ... or try again at Sep 24th, 2026 12:03 AM.",
            ),
            _delivered("crew-codex", died + 33 * 3600),
        ]
        assert sr.vendor_health(rows, now=died + 3600)["codex"]["out"] is True
        assert sr.vendor_health(rows, now=died + 34 * 3600)["codex"]["out"] is False

    def test_a_future_tombstone_is_not_evidence_when_replaying(self):
        died = _local(2026, 9, 18, 12, 50)
        rows = [
            _hit("crew-codex", died, "try again at Sep 24th, 2026 12:03 AM. usageLimitExceeded"),
            _delivered("crew-codex", died + 3600),
        ]
        assert sr.vendor_health(rows, now=died + 60)["codex"]["out"] is True

    def test_throttling_is_a_blip(self):
        died = 1_700_000_000.0
        rows = [
            _hit(
                "crew-claude",
                died,
                "Bedrock is throttling requests. Try: (1) wait a few seconds and retry",
            )
        ]
        assert sr.vendor_health(rows, now=died + 60)["claude"]["out"] is True
        assert sr.vendor_health(rows, now=died + sr.THROTTLE_WINDOW_S + 1)["claude"]["out"] is False

    def test_other_vendors_are_untouched(self):
        died = 1_700_000_000.0
        rows = [_hit("crew-codex", died, "usageLimitExceeded try again at 4:46 PM.")]
        h = sr.vendor_health(rows, now=died + 60)
        assert h["claude"]["out"] is False and h["deepseek"]["out"] is False

    def test_a_non_limit_death_is_not_a_hit(self):
        rows = [
            _hit(
                "crew-codex",
                1_700_000_000.0,
                "AcpProcessDied: Runtime process died during prompt (rc=-15)",
            )
        ]
        assert sr.vendor_health(rows, now=1_700_000_060.0)["codex"]["out"] is False


class TestVendorOfSeat:
    @pytest.mark.parametrize(
        "agent,model,expected",
        [
            ("crew-codex-atlas", "", "codex"),
            ("crew-claude", "", "claude"),
            ("crew-deepseek-pro", "", "deepseek"),
            ("crew-local-atlas", "", "local"),
            ("", "claude-fable-5-1[1m]", "claude"),
            ("", "gpt-5.6-sol", "codex"),
            ("", "deepseek-3.2", "deepseek"),
            ("", "", ""),
        ],
    )
    def test_seat_then_model(self, agent, model, expected):
        assert sr.vendor_of_seat(agent, model) == expected


# ---------------------------------------------------------------------------
# Config and candidates
# ---------------------------------------------------------------------------


class TestConfig:
    def test_shipped_state_is_empty(self):
        assert DecisionsConfig().spawn_route == {}

    def test_coercion_keeps_known_vendors_and_lists(self):
        got = coerce_spawn_route(
            {
                "codex": {"large": ["gpt-5.6-sol", "auto", ""], "small": "gpt-5.6-terra"},
                "bogus": {"large": ["x"]},
            }
        )
        assert got == {
            "codex": {"top": [], "large": ["gpt-5.6-sol"], "small": ["gpt-5.6-terra"], "mini": []}
        }

    def test_candidates_are_the_child_vendors_pins_only(self):
        cands = sr.candidates_for("codex", TIERS)
        assert [c["model"] for c in cands] == ["gpt-5.6-sol", "gpt-5.6-terra"]
        assert [c["key"] for c in cands] == ["codex/large/0", "codex/small/0"]
        assert sr.candidates_for("deepseek", TIERS) == []


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


class TestRoutedSpawn:
    def test_a_configured_vendor_gets_the_answered_tier_and_the_note(
        self, install_oracle, snapshot
    ):
        oracle = install_oracle(_Oracle("codex/small/0", p=0.7))
        got = _run(
            sr.routed_spawn(
                "READ-ONLY: find every caller.",
                agent="crew-codex",
                parent_model="gpt-5.6-sol",
                session_key="s1",
            )
        )
        assert got is not None
        assert (got["model"], got["tier"], got["vendor"], got["p"]) == (
            "gpt-5.6-terra",
            "small",
            "codex",
            0.7,
        )
        assert got["reason"] == "survey/simple small"
        assert sr.reason_line(got).startswith(
            "spawn.route: survey/simple small -> codex small (gpt-5.6-terra) p=0.70"
        )
        state = oracle.states[0]
        assert state["parent_model"] == "gpt-5.6-sol" and state["parent_vendor"] == "codex"
        assert state["agent"] == "crew-codex"
        assert [c["key"] for c in state["candidates"]] == ["codex/large/0", "codex/small/0"]
        assert oracle.questions[0][0].options == ["codex/large/0", "codex/small/0"]

    def test_health_rides_along_and_is_said_on_the_line(
        self, install_oracle, snapshot, monkeypatch
    ):
        died = _local(2026, 9, 26, 0, 12)
        monkeypatch.setattr(
            sr,
            "load_tombstones",
            lambda root=None: [
                _hit(
                    "crew-codex",
                    died,
                    "usage limit. try again at Oct 1st, 2026 9:19 AM. usageLimitExceeded",
                )
            ],
        )
        monkeypatch.setattr(sr.time, "time", lambda: died + 600)
        oracle = install_oracle(_Oracle("codex/large/0"))
        got = _run(
            sr.routed_spawn("Implement the schema migration.", agent="crew-codex", session_key="s1")
        )
        assert got["vendor_out"] is True
        assert oracle.states[0]["health"]["codex"]["out"] is True
        assert "vendor rate-limited until" in sr.reason_line(got)

    def test_no_pins_for_the_vendor_asks_nothing(self, install_oracle, snapshot):
        oracle = install_oracle(_Oracle("x"))
        assert _run(sr.routed_spawn("anything", agent="crew-deepseek", session_key="s1")) is None
        assert oracle.states == []

    def test_unknown_vendor_asks_nothing(self, install_oracle, snapshot):
        oracle = install_oracle(_Oracle("x"))
        assert (
            _run(sr.routed_spawn("anything", agent="", parent_model="", session_key="s1")) is None
        )
        assert oracle.states == []

    def test_no_consent_routes_nothing(self, install_oracle, snapshot, consent):
        consent(None)
        oracle = install_oracle(_Oracle("codex/small/0"))
        assert _run(sr.routed_spawn("anything", agent="crew-codex", session_key="s1")) is None
        assert oracle.states == []

    def test_an_out_of_domain_answer_routes_nothing(self, install_oracle, snapshot):
        install_oracle(_Oracle("claude/small/0"))
        assert _run(sr.routed_spawn("anything", agent="crew-codex", session_key="s1")) is None

    def test_a_provider_failure_routes_nothing(self, install_oracle, snapshot):
        class _Broken:
            async def ask(self, state, questions):
                raise RuntimeError("down")

        install_oracle(_Broken())
        assert _run(sr.routed_spawn("anything", agent="crew-codex", session_key="s1")) is None

    def test_the_brief_is_bounded(self, install_oracle, snapshot):
        oracle = install_oracle(_Oracle("codex/small/0"))
        _run(sr.routed_spawn("x" * 10_000, agent="crew-codex", session_key="s1"))
        assert len(oracle.states[0]["brief"]) == sr.MAX_BRIEF_CHARS


class TestNoteOnTheWire:
    def test_note_is_bounded_and_optional(self):
        from kiro_crew.decisions.impl_jev import MAX_NOTE_CHARS, _answer_from_wire
        from kiro_crew.decisions.types import Choice

        q = Choice("spawn", "which", options=["a", "b"])
        a = _answer_from_wire(
            q,
            {
                "type": "choice",
                "choice": "a",
                "probabilities": {"a": 0.9, "b": 0.1},
                "note": "n" * 500,
            },
        )
        assert a.note == "n" * MAX_NOTE_CHARS
        b = _answer_from_wire(
            q,
            {
                "type": "choice",
                "choice": "a",
                "probabilities": {"a": 0.9, "b": 0.1},
                "note": {"role": "x"},
            },
        )
        assert b.note == ""


ADVERTISED = {
    "codex": [
        "gpt-6-astra[medium]",
        "gpt-6-astra[high]",
        "gpt-5.6-sol[medium]",
        "gpt-5.6-sol[high]",
        "gpt-5.6-terra[medium]",
        "gpt-5.6-terra[high]",
        "gpt-5.6-luna[medium]",
        "gpt-5.5[high]",
    ],
    "claude": [
        "default",
        "opus[1m]",
        "claude-fable-5-1[1m]",
        "sonnet",
        "haiku",
        "agent",
        "subagent",
    ],
    "deepseek": [
        '["deepseek-official","deepseek-flash"]',
        '["deepseek-official","deepseek-v4-pro"]',
        '["deepseek-official","deepseek-v4-flash-vision-exp"]',
    ],
}


class TestAdvertisedTiers:
    """The real function, over the lists this machine's adapters advertised on 2026-09-29."""

    def _tiers(self, vendor):
        return REAL_ADVERTISED_TIERS(vendor, ADVERTISED.get(vendor, []))

    def test_claude_rungs(self):
        assert self._tiers("claude") == {
            "top": ["claude-fable-5-1[1m]"],
            "large": ["opus[1m]"],
            "small": ["sonnet"],
            "mini": ["haiku"],
        }

    def test_codex_rungs_take_the_rung_effort_and_skip_unlisted_families(self):
        assert self._tiers("codex") == {
            "top": ["gpt-6-astra[high]"],
            "large": ["gpt-5.6-sol[high]"],
            "small": ["gpt-5.6-terra[medium]"],
            "mini": ["gpt-5.6-luna[medium]"],
        }

    def test_deepseek_pro_and_flash_without_experimental_variants(self):
        pro, flash = ['["deepseek-official","deepseek-v4-pro"]'], [
            '["deepseek-official","deepseek-flash"]'
        ]
        assert self._tiers("deepseek") == {"top": pro, "large": pro, "small": flash, "mini": flash}

    def test_a_vendor_without_families_has_none(self):
        assert self._tiers("local") == {}


class TestEffectiveTiers:
    def test_a_pinned_tier_replaces_only_that_tier(self, monkeypatch):
        monkeypatch.setattr(
            sr,
            "advertised_tiers",
            lambda vendor, advertised=None: {
                "top": ["claude-fable-5-1[1m]"],
                "large": ["opus[1m]"],
                "small": ["sonnet"],
                "mini": ["haiku"],
            },
        )
        cfg = _config(spawn_route={"claude": {"large": ["claude-opus-4.8"]}})
        assert sr.effective_tiers("claude", cfg) == {
            "top": ["claude-fable-5-1[1m]"],
            "large": ["claude-opus-4.8"],
            "small": ["sonnet"],
            "mini": ["haiku"],
        }

    def test_no_pins_uses_the_advertised_tiers(self, install_oracle, snapshot, monkeypatch):
        snapshot(_config(spawn_route={}))
        monkeypatch.setattr(
            sr,
            "advertised_tiers",
            lambda vendor, advertised=None: {
                "large": ["gpt-5.6-sol[high]"],
                "small": ["gpt-5.6-terra[medium]"],
            },
        )
        oracle = install_oracle(_Oracle("codex/small/0"))
        got = _run(sr.routed_spawn("READ-ONLY: list files.", agent="crew-codex", session_key="s1"))
        assert got["model"] == "gpt-5.6-terra[medium]"
        assert [c["model"] for c in oracle.states[0]["candidates"]] == [
            "gpt-5.6-sol[high]",
            "gpt-5.6-terra[medium]",
        ]
