"""spawn.route v2 (fork): cross-vendor candidates, vendor headroom, and the backend switch.

Contract: nexus ``docs/upstream/kirocrew-delegation-fit-issue.md`` and
``eval/decision-lane/POLICY.md`` (operator ruling of 2026-11-20).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from kiro_crew import vendor_limits as vl
from kiro_crew.config.sections import DECISION_PROVIDER_ENDPOINT_DEFAULT, DecisionsConfig
from kiro_crew.decisions import log as log_mod
from kiro_crew.decisions.points import spawn_route as sr
from kiro_crew.decisions.types import Answer

#: ``agy models`` on Nexus, 2026-11-20 (ids only).
AGY_IDS = [
    "gemini-4-neon-high",
    "gemini-4-neon-xhigh",
    "gemini-4-neon-medium",
    "gemini-4-neon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "gemini-4-argon-low",
    "gemini-4-hydrogen-high",
    "gemini-4-hydrogen-xhigh",
    "gemini-4-hydrogen-medium",
    "gemini-4-hydrogen-low",
    "gemini-pro-orchestrator-neon",
    "gemini-pro-orchestrator-g1-pro",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
    "claude-opus-5-5-high",
    "claude-sonnet-5-5-medium",
]

NOW = 1_795_170_000.0


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(vl, "_data_home", lambda: tmp_path)
    vl._reset_for_tests()
    monkeypatch.setattr(sr, "_limits_cache", None)
    monkeypatch.setattr(sr, "_health_cache", None)
    monkeypatch.setattr(sr, "_agy_models_cache", None)
    monkeypatch.setattr(sr, "load_tombstones", lambda root=None: [])
    consent = tmp_path / "decisions_consent.json"
    consent.write_text(
        json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}), encoding="utf-8"
    )
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: consent)
    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")
    yield
    vl._reset_for_tests()


# ---------------------------------------------------------------------------
# Claude: _claude/rateLimit on usage _meta
# ---------------------------------------------------------------------------


class TestClaudeRateLimit:
    def test_usage_meta_is_kept_and_survives_a_restart(self, tmp_path):
        vl.note_usage_meta(
            {
                "sessionUpdate": "usage_update",
                "used": 10,
                "size": 100,
                "_meta": {
                    "_claude/rateLimit": {
                        "status": "allowed_warning",
                        "utilization": 0.42,
                        "rateLimitType": "five_hour",
                        "resetsAt": NOW + 3600,
                    }
                },
            }
        )
        assert json.loads((tmp_path / vl.STORE_NAME).read_text())["claude"]["five_hour"][
            "used_pct"
        ] == 42.0
        vl._reset_for_tests()  # a restart: the store is read back from disk
        h = vl.claude_health(NOW)
        assert h["out"] is False and h["used_pct"] == 42.0 and h["window"] == "five_hour"
        assert h["resets_at"] == NOW + 3600 and h["source"] == "claude_rate_limit_event"

    def test_the_most_used_live_window_binds(self):
        vl.note_claude_rate_limit(
            {"status": "allowed", "utilization": 0.2, "rateLimitType": "five_hour",
             "resetsAt": NOW + 100}, now=NOW,
        )
        vl.note_claude_rate_limit(
            {"status": "allowed", "utilization": 0.7, "rateLimitType": "seven_day",
             "resetsAt": NOW + 86400}, now=NOW,
        )
        # an expired window no longer counts
        vl.note_claude_rate_limit(
            {"status": "allowed", "utilization": 0.99, "rateLimitType": "seven_day_opus",
             "resetsAt": NOW - 1}, now=NOW,
        )
        h = vl.claude_health(NOW)
        assert (h["window"], h["used_pct"]) == ("seven_day", 70.0)

    def test_rejected_is_out_until_its_reset(self):
        vl.note_claude_rate_limit(
            {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": NOW + 600}, now=NOW
        )
        assert vl.claude_health(NOW)["out"] is True
        assert vl.claude_health(NOW)["used_pct"] == 100.0
        assert vl.claude_health(NOW + 601) == {}

    def test_newest_reading_per_window_wins(self):
        vl.note_claude_rate_limit(
            {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": NOW + 600}, now=NOW
        )
        vl.note_claude_rate_limit(
            {"status": "allowed", "utilization": 0.1, "rateLimitType": "five_hour",
             "resetsAt": NOW + 600}, now=NOW + 5,
        )
        assert vl.claude_health(NOW + 10)["out"] is False

    @pytest.mark.parametrize(
        "meta", [None, {}, {"_claude/rateLimit": "x"}, {"_claude/rateLimit": {"status": "?"}}]
    )
    def test_anything_else_is_ignored(self, meta):
        vl.note_usage_meta({"sessionUpdate": "usage_update", "_meta": meta})
        assert vl.claude_health(NOW) == {}

    def test_unknown_is_empty_not_zero(self):
        assert vl.claude_health(NOW) == {}

    def test_the_acp_client_usage_path_records_it(self):
        from kiro_crew.acp import client_usage

        stats = SimpleNamespace(
            note_pct_reported=lambda: None, apply_cost_cumulative=lambda c: None
        )
        fake = SimpleNamespace(last_prompt_stats=stats, _is_claude=True)
        msg = SimpleNamespace(
            params={
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": 5,
                    "size": 10,
                    "_meta": {
                        "_claude/rateLimit": {
                            "status": "allowed",
                            "utilization": 0.5,
                            "rateLimitType": "five_hour",
                            "resetsAt": NOW + 60,
                        }
                    },
                }
            }
        )
        client_usage._track_usage_update(fake, msg)
        assert vl.claude_health(NOW)["used_pct"] == 50.0


# ---------------------------------------------------------------------------
# Codex, Antigravity, local
# ---------------------------------------------------------------------------


class TestOtherVendors:
    def test_codex_weekly_only_reading(self):
        h = vl.codex_health(
            {
                "five_hour": None,
                "weekly": {"used_percent": 87.0, "resets_at": NOW + 3600, "stale": False},
                "captured_at": "2026-11-20T06:15:00Z",
            }
        )
        assert (h["out"], h["used_pct"], h["window"], h["source"]) == (
            False, 87.0, "weekly", "codex_rollout"
        )
        assert h["resets_at"] == NOW + 3600 and h["as_of"] > 0

    def test_codex_full_window_is_out_and_stale_is_ignored(self):
        assert vl.codex_health(
            {"weekly": {"used_percent": 100.0, "resets_at": NOW + 1, "stale": False}}
        )["out"] is True
        assert vl.codex_health(
            {"weekly": {"used_percent": 100.0, "resets_at": NOW - 1, "stale": True}}
        ) == {}
        assert vl.codex_health({}) == {}

    def test_antigravity_quota_file(self, tmp_path):
        assert vl.antigravity_health() == {}  # absent file = unknown
        (tmp_path / vl.ANTIGRAVITY_QUOTA_NAME).write_text(
            json.dumps(
                {
                    "as_of": "2026-11-20T06:00:00Z",
                    "models": {
                        "gemini-4-hydrogen-medium": {
                            "remaining_fraction": 0.7,
                            "reset_time": "2099-01-01T00:00:00Z",
                        },
                        "gemini-4-neon-medium": {
                            "remaining_fraction": 0.0,
                            "reset_time": "2099-01-01T00:00:00Z",
                        },
                        # rolled over since the probe: no longer evidence
                        "gemini-4-neon-high": {
                            "remaining_fraction": 0.0,
                            "reset_time": "2000-01-01T00:00:00Z",
                        },
                    },
                }
            )
        )
        h = vl.antigravity_health(["gemini-4-hydrogen-medium"], now=NOW)
        assert h["per_model"] == {"gemini-4-hydrogen-medium": 30.0, "gemini-4-neon-medium": 100.0}
        assert (h["out"], h["used_pct"], h["window"], h["source"]) == (
            False, 30.0, "per_model", "agy_quota"
        )
        both = vl.antigravity_health(["gemini-4-hydrogen-medium", "gemini-4-neon-medium"], now=NOW)
        assert both["out"] is False and both["used_pct"] == 100.0
        assert vl.antigravity_health(["gemini-4-neon-medium"], now=NOW)["out"] is True

    def test_local_lanes_from_fleet_status(self):
        status = {
            "lanes": {"nexus-swap": {"available": True}, "noda-swap": {"available": False}},
            "roles": {"subagent": {"lane": "nexus-swap"}, "agent": {"lane": "noda-swap"}},
        }
        h = vl.local_health(status)
        assert h["lanes"] == {"subagent": True, "agent": False} and h["out"] is False
        assert h["source"] == "fleet_status"
        assert vl.local_health({"nothing": 1}) == {}


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


class TestAntigravityTiers:
    def test_flash_hydrogen_neon_and_never_argon(self):
        tiers = sr.advertised_tiers("antigravity", AGY_IDS)
        assert tiers["mini"] == ["gemini-3.8-flash-medium"]
        assert tiers["small"] == ["gemini-4-hydrogen-medium", "gemini-4-neon-medium"]
        assert tiers["large"] == ["gemini-4-hydrogen-high", "gemini-4-neon-high"]
        assert "top" not in tiers
        offered = [m for ids in tiers.values() for m in ids]
        assert not any("argon" in m or "claude" in m or "orchestrator" in m for m in offered)

    def test_an_argon_pin_is_never_offered(self, monkeypatch):
        monkeypatch.setattr(
            sr,
            "tier_map",
            lambda config=None: {
                "antigravity": {"small": ["gemini-4-argon-medium", "gemini-4-hydrogen-medium"]}
            },
        )
        monkeypatch.setattr(sr, "advertised_tiers", lambda vendor, advertised=None: {})
        assert sr.effective_tiers("antigravity")["small"] == ["gemini-4-hydrogen-medium"]

    def test_advertised_ids_come_from_agy_when_nothing_is_cached(self, monkeypatch):
        calls = []
        monkeypatch.setattr(sr, "_run_agy_models", lambda: calls.append(1) or list(AGY_IDS))
        assert sr.antigravity_advertised(now=NOW) == AGY_IDS
        assert sr.antigravity_advertised(now=NOW + 10) == AGY_IDS
        assert calls == [1]  # cached

    def test_the_quota_file_lists_ids_without_running_agy(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sr, "_run_agy_models", lambda: pytest.fail("agy run"))
        (tmp_path / vl.ANTIGRAVITY_QUOTA_NAME).write_text(
            json.dumps({"models": {"gemini-4-hydrogen-low": {"remaining_fraction": 1.0}}})
        )
        assert sr.antigravity_advertised(now=NOW) == ["gemini-4-hydrogen-low"]


def _fake_tiers(vendor, config=None):
    return {
        "claude": {"top": [], "large": ["claude-opus-5"], "small": ["claude-sonnet-5"], "mini": []},
        "codex": {"top": [], "large": ["gpt-6.2-sol[high]"], "small": [], "mini": []},
        "antigravity": {
            "top": [],
            "large": ["gemini-4-neon-high"],
            "small": ["gemini-4-hydrogen-medium", "gemini-4-neon-medium"],
            "mini": [],
        },
        "deepseek": {"top": [], "large": ["deepseek-v4-pro"], "small": [], "mini": []},
    }.get(vendor, {})


BACKENDS = {
    "antigravity": {"backend": "antigravity", "runtime_agent": "crew-antigravity", "native": ""},
    "codex": {"backend": "codex", "runtime_agent": "", "native": "codex"},
}


class TestCandidates:
    def test_every_runnable_vendor_with_its_backend(self, monkeypatch):
        monkeypatch.setattr(sr, "effective_tiers", _fake_tiers)
        cands = sr.all_candidates("claude", backends=BACKENDS)
        by_key = {c["key"]: c for c in cands}
        assert by_key["claude/small/0"]["backend"] == ""
        assert by_key["antigravity/small/0"] == {
            "key": "antigravity/small/0",
            "vendor": "antigravity",
            "tier": "small",
            "model": "gemini-4-hydrogen-medium",
            "backend": "antigravity",
        }
        assert by_key["codex/large/0"]["backend"] == "codex"
        # metered: never offered to a parent of another vendor
        assert not any(c["vendor"] == "deepseek" for c in cands)
        # a vendor with no backend on this host is not offered
        assert not any(c["vendor"] == "local" for c in cands)

    def test_local_roles_are_offered_when_a_seat_carries_them(self, monkeypatch):
        monkeypatch.setattr(sr, "effective_tiers", _fake_tiers)
        backends = {
            "local/subagent": {"backend": "local-background", "runtime_agent": "kirocrew-lite",
                               "native": ""},
            "local/agent": {"backend": "local-fleet", "runtime_agent": "crew-local",
                            "native": ""},
        }
        cands = [c for c in sr.all_candidates("claude", backends=backends) if c["vendor"] == "local"]
        assert cands == [
            {"key": "local/subagent/0", "vendor": "local", "tier": "subagent",
             "model": "subagent", "backend": "local-background"},
            {"key": "local/agent/0", "vendor": "local", "tier": "agent",
             "model": "agent", "backend": "local-fleet"},
        ]

    def test_a_local_parent_gets_its_roles_on_its_own_seat(self, monkeypatch):
        monkeypatch.setattr(sr, "effective_tiers", _fake_tiers)
        cands = sr.all_candidates("local", cross_vendor=False)
        assert [(c["key"], c["backend"]) for c in cands] == [
            ("local/subagent/0", ""), ("local/agent/0", "")
        ]

    def test_a_pinned_vendor_is_offered_alone(self, monkeypatch):
        monkeypatch.setattr(sr, "effective_tiers", _fake_tiers)
        cands = sr.all_candidates("claude", cross_vendor=False, backends=BACKENDS)
        assert {c["vendor"] for c in cands} == {"claude"}

    def test_backends_from_seats_then_native(self, monkeypatch):
        import kiro_crew.execution_seats as es

        monkeypatch.setattr(
            es,
            "declared_execution_seats",
            lambda cfg=None: {
                "antigravity": {"id": "antigravity", "runtime_agent": "crew-antigravity",
                                "runtime": "Antigravity"},
                "local-background": {"id": "local-background", "runtime_agent": "kirocrew-lite",
                                     "runtime": "local"},
                "local-fleet": {"id": "local-fleet", "runtime_agent": "crew-local",
                                "runtime": "local"},
            },
        )
        b = sr.route_backends("claude")
        assert b["antigravity"] == {
            "backend": "antigravity", "runtime_agent": "crew-antigravity", "native": ""
        }
        assert b["local/subagent"]["backend"] == "local-background"
        assert b["local/agent"]["backend"] == "local-fleet"
        assert b["codex"] == {"backend": "codex", "runtime_agent": "", "native": "codex"}
        assert "claude" not in b  # the parent's own vendor runs on its own seat
        assert "deepseek" not in b

    def test_unreadable_seats_still_offer_native(self, monkeypatch):
        import kiro_crew.execution_seats as es

        def boom(cfg=None):
            raise ValueError("bad seat")

        monkeypatch.setattr(es, "declared_execution_seats", boom)
        assert set(sr.route_backends("codex")) == {"claude"}

    @pytest.mark.parametrize(
        "model,vendor", [("opus", "claude"), ("gemini-4-hydrogen-medium", "antigravity")]
    )
    def test_vendor_from_model_family(self, model, vendor):
        assert sr.vendor_of_seat("derrick", model) == vendor


# ---------------------------------------------------------------------------
# Health in the state, the answer, the receipt
# ---------------------------------------------------------------------------


def test_merged_health_keeps_unknown_absent():
    tombs = sr.vendor_health([], now=NOW)
    limits = {
        "claude": {"out": False, "used_pct": 42.0, "window": "five_hour",
                   "resets_at": NOW + 10, "source": "claude_rate_limit_event", "as_of": NOW},
        "codex": {"out": True, "used_pct": 100.0, "window": "weekly", "resets_at": NOW + 99,
                  "source": "codex_rollout"},
    }
    h = sr.merged_health(tombs, limits)
    state = sr.build_state("b", vendor="claude", parent_model="", cands=[], health=h)["health"]
    assert set(state) == {"claude", "codex"}
    assert state["claude"]["used_pct"] == 42.0 and state["claude"]["out"] is False
    assert state["codex"]["out"] is True and state["codex"]["until"] == NOW + 99
    assert "used_pct" not in state.get("deepseek", {})


class _Oracle:
    def __init__(self, value, note="read/simple"):
        self.value, self.note, self.states = value, note, []

    async def ask(self, state, questions):
        self.states.append(state)
        return {q.id: Answer(id=q.id, value=self.value, p=0.8, note=self.note) for q in questions}


@pytest.fixture
def routed_env(monkeypatch):
    import kiro_crew.decisions.gate as gate_mod

    monkeypatch.setattr(
        gate_mod, "_snapshot", lambda: SimpleNamespace(decisions=DecisionsConfig(bucket=100))
    )
    monkeypatch.setattr(sr, "effective_tiers", _fake_tiers)
    monkeypatch.setattr(sr, "route_backends", lambda parent_vendor, cfg=None: dict(BACKENDS))
    monkeypatch.setattr(
        sr,
        "limit_health",
        lambda models=(), now=None: {
            "antigravity": {"out": False, "used_pct": 30.0, "window": "per_model",
                            "per_model": {"gemini-4-hydrogen-medium": 30.0}, "source": "agy_quota"},
            "codex": {"out": False, "used_pct": 87.0, "window": "weekly", "source": "codex_rollout"},
        },
    )

    def install(oracle):
        monkeypatch.setattr(gate_mod, "_oracle", lambda *a, **k: oracle)
        return oracle

    return install


class TestRoutedAcrossVendors:
    def test_another_vendor_comes_back_with_its_seat(self, routed_env):
        oracle = routed_env(_Oracle("antigravity/small/0"))
        got = asyncio.run(
            sr.routed_spawn("READ-ONLY: trace callers.", agent="crew-claude-atlas",
                            parent_model="claude-opus-5", session_key="s1")
        )
        assert got is not None
        assert (got["vendor"], got["model"], got["backend"], got["runtime_agent"]) == (
            "antigravity", "gemini-4-hydrogen-medium", "antigravity", "crew-antigravity"
        )
        state = oracle.states[0]
        assert state["parent_vendor"] == "claude"
        assert {c["vendor"] for c in state["candidates"]} == {"claude", "codex", "antigravity"}
        assert state["health"]["codex"]["used_pct"] == 87.0
        line = sr.reason_line(got)
        assert line.startswith(
            "spawn.route: read/simple -> antigravity small (gemini-4-hydrogen-medium) on antigravity"
        )
        assert "30% of its quota" in line and "low: codex" in line

    def test_a_native_backend_route(self, routed_env):
        routed_env(_Oracle("codex/large/0", note="trace/complex"))
        got = asyncio.run(sr.routed_spawn("x", agent="crew-claude", session_key="s1"))
        assert (got["backend"], got["native_backend"], got["runtime_agent"]) == ("codex", "codex", "")
        assert "87% of weekly" in sr.reason_line(got)

    def test_own_vendor_stays_on_its_seat(self, routed_env):
        routed_env(_Oracle("claude/small/0"))
        got = asyncio.run(sr.routed_spawn("x", agent="crew-claude", session_key="s1"))
        assert got["backend"] == "" and got["model"] == "claude-sonnet-5"
        assert " on " not in sr.reason_line(got)

    def test_pinned_vendor_offers_only_its_own(self, routed_env):
        oracle = routed_env(_Oracle("claude/small/0"))
        asyncio.run(sr.routed_spawn("x", agent="crew-claude", session_key="s1", cross_vendor=False))
        assert {c["vendor"] for c in oracle.states[0]["candidates"]} == {"claude"}


class TestApply:
    def _info(self, **kw):
        base = dict(backend=None, runtime_agent="", crew="", agent="", conversation_key="")
        base.update(kw)
        return SimpleNamespace(**base)

    def test_own_seat_needs_nothing(self):
        info = self._info()
        assert sr.apply_route_backend(info, {"backend": ""}) is True
        assert info.backend is None and info.runtime_agent == ""

    def test_execution_seat_sets_runtime_agent_after_the_member_check(self, monkeypatch):
        import kiro_crew.execution_seats as es

        checked = []
        monkeypatch.setattr(
            es, "validate_member_execution_seat", lambda ra, ex: checked.append((ra, ex))
        )
        info = self._info()
        routed = {"backend": "antigravity", "runtime_agent": "crew-antigravity"}
        assert sr.apply_route_backend(info, routed, execution="EX") is True
        assert info.runtime_agent == "crew-antigravity" and info.backend is None
        assert checked == [("crew-antigravity", "EX")]

    def test_a_seat_that_cannot_carry_the_member_refuses_the_route(self, monkeypatch):
        import kiro_crew.execution_seats as es

        def refuse(ra, ex):
            raise es.ExecutionSeatError("runtime_member_capabilities_unverified")

        monkeypatch.setattr(es, "validate_member_execution_seat", refuse)
        info = self._info()
        routed = {"backend": "antigravity", "runtime_agent": "crew-antigravity"}
        assert sr.apply_route_backend(info, routed, execution="EX") is False
        assert info.runtime_agent == ""

    def test_native_backend(self):
        info = self._info()
        assert sr.apply_route_backend(info, {"backend": "codex", "native_backend": "codex"})
        assert info.backend == "codex"

    @pytest.mark.parametrize(
        "pin",
        [
            {"backend": "claude"},
            {"backend": ""},
            {"runtime_agent": "x"},
            {"crew": "derrick"},
            {"agent": "crew-codex"},
            {"conversation_key": "k"},
        ],
    )
    def test_a_pin_is_never_overridden(self, pin):
        info = self._info(**pin)
        assert sr.pins_vendor(info) is True
        before = dict(vars(info))
        assert sr.apply_route_backend(info, {"backend": "codex", "native_backend": "codex"}) is False
        assert vars(info) == before

    def test_own_worker_pins(self):
        assert sr.pins_vendor(self._info(), own_worker=True) is True
        assert sr.pins_vendor(self._info()) is False


class TestNexusCompanionSeats:
    """The Nexus companion's declared seats resolve to a backend per vendor and local role.

    Runs only where ``kirocrew_nexus_companion`` is importable (Nexus, with
    ``PYTHONPATH`` at the companion's ``src``): it is the companion contract the
    resolver above was written against, not something upstream ships.
    """

    ENGINES = {
        "engines": {
            "claude-max": {"env": {}},
            "codex-sub": {"env": {"ACP_ENGINE": "codex", "CODEX_HOME": "/codex"}},
            "local-fleet": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8484",
                                    "ANTHROPIC_MODEL": "agent"}},
            "local-background": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8484",
                                         "ANTHROPIC_MODEL": "subagent"}},
            "deepseek-flash": {"env": {"ACP_ENGINE": "deepseek"}},
        },
        "agents": {
            "crew-claude": "claude-max",
            "crew-codex": "codex-sub",
            "crew-local": "local-fleet",
            "kirocrew-heartbeat": "local-background",
            "crew-deepseek": "deepseek-flash",
        },
    }

    @pytest.fixture
    def companion_cfg(self, tmp_path, monkeypatch):
        companion = pytest.importorskip("kirocrew_nexus_companion")
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.platform import context

        path = tmp_path / "engines.yaml"
        path.write_text(json.dumps(self.ENGINES), encoding="utf-8")
        monkeypatch.setenv("KIROCREW_ENGINES", str(path))
        registry = companion.NexusProviderRegistry()
        monkeypatch.setattr(
            context, "current_context", lambda: SimpleNamespace(providers=registry)
        )
        cfg = KiroCrewConfig()
        for alias in self.ENGINES["agents"]:
            cfg.agents[alias] = KiroCrewAgentConfig(kiro_agent=alias)
        return cfg

    def test_every_cross_vendor_role_resolves_to_its_seat(self, companion_cfg):
        b = sr.route_backends("antigravity", companion_cfg)
        assert {k: (v["backend"], v["runtime_agent"], v["native"]) for k, v in b.items()} == {
            "claude": ("claude-max", "crew-claude", ""),
            "codex": ("codex-sub", "crew-codex", ""),
            "local/agent": ("local-fleet", "crew-local", ""),
            "local/subagent": ("local-background", "kirocrew-heartbeat", ""),
        }

    def test_a_seat_wins_over_the_native_backend_of_its_vendor(self, companion_cfg):
        b = sr.route_backends("claude", companion_cfg)
        assert b["codex"]["backend"] == "codex-sub" and b["codex"]["native"] == ""
        assert "claude" not in b and "deepseek" not in b

    def test_ordinary_runs_are_admitted_onto_every_seat(self, companion_cfg):
        from kiro_crew.execution_seats import validate_member_execution_seat

        for alias in ("crew-claude", "crew-codex", "crew-local", "kirocrew-heartbeat"):
            validate_member_execution_seat(alias, SimpleNamespace(member_id=None))
