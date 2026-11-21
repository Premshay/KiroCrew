"""Budget-aware context trimming for spawns routed to a local lane (fork, Nexus).

Operator ruling 2026-11-20: a local worker keeps skills, the seat/crew spec and
the project group; only what does not fit its lane is trimmed, memory first,
then lessons. Cloud picks and explicit ``include_*=true`` are never trimmed; a
run whose minimal set does not fit skips the local candidate (``too big``).
The CPU role fits a first-turn token target (12,000), not its window, and
carries the lean helper contract (``test_local_lean_helper``).
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
from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_manager import local_check as lc
from kiro_crew.subagent_manager import quota_switch as qs

ALL = frozenset({"memory", "lessons", "project"})

#: Brief sizes against the measured figures (subagent room = 12,000 - 4,050 = 7,950
#: before the brief; lean context 13,835 in full, 9,968 without memory, 4,167
#: without lessons too): the CPU role keeps memory or lessons only when they are
#: smaller than the medians, so a median run drops both; minus both it fits a
#: brief <= 3,783 tokens.
SHORT = "x" * 320  # 101 tokens -> drop memory, then lessons
MID = "x" * 6400  # 2,001 tokens -> drop memory, then lessons
BIG = "x" * 16000  # 5,001 tokens -> too big for the CPU target even trimmed
HUGE = "x" * 32000  # 10,001 tokens -> too big even trimmed


class TestFit:
    def test_measured_inputs(self):
        assert lc.input_budget("subagent") == 12000
        assert lc.input_budget("subagent", 65536) == 12000
        assert lc.input_budget("agent") == 131072 - 8192
        assert lc.input_budget("subagent", 16384) == 8192
        assert lc.context_tokens(None) == lc.INJECTED_CONTEXT_TOKENS
        assert lc.context_tokens(frozenset({"project"})) == lc.KEPT_CONTEXT_TOKENS

    def test_gpu_lane_keeps_everything(self):
        fit = lc.fit_local("agent", MID, groups=ALL)
        assert fit.fits and fit.drop == ()

    def test_cpu_lane_drops_memory_first(self):
        # Without lessons in the run, dropping memory alone fits.
        fit = lc.fit_local("subagent", SHORT, groups=frozenset({"memory", "project"}))
        assert fit.fits and fit.drop == ("memory",)

    def test_cpu_lane_then_drops_lessons(self):
        for brief in (SHORT, MID):
            fit = lc.fit_local("subagent", brief, groups=ALL)
            assert fit.fits and fit.drop == ("memory", "lessons")
            assert fit.need == lc.context_tokens(frozenset({"project"}), "subagent")

    def test_never_drops_project(self):
        for brief in (BIG, HUGE):
            fit = lc.fit_local("subagent", brief, groups=ALL)
            assert not fit.fits and fit.drop == ()

    def test_explicit_include_is_kept_and_then_too_big(self):
        fit = lc.fit_local("subagent", SHORT, groups=ALL, pinned=frozenset({"lessons"}))
        assert not fit.fits
        fit = lc.fit_local("subagent", SHORT, groups=ALL, pinned=frozenset({"memory"}))
        assert not fit.fits

    def test_a_group_already_withheld_is_not_reported_trimmed(self):
        fit = lc.fit_local("subagent", SHORT, groups=frozenset({"project"}))
        assert fit.fits and fit.drop == ()

    def test_live_ctx_is_read(self):
        assert lc.fit_local("agent", SHORT, groups=ALL).fits
        # a 32k reading leaves the agent seat no room for its own harness
        assert not lc.fit_local("agent", SHORT, groups=ALL, ctx=32768).fits

    def test_trim_line(self):
        assert lc.trim_line_for(("memory", "lessons"), 12000) == (
            "local: trimmed memory, lessons to fit 12.0k"
        )


class TestApplyTrim:
    def test_drops_through_the_include_flags_and_records_it(self):
        info = SubagentInfo(id="a", task="t")
        lc.apply_trim(info, ["memory", "lessons"])
        assert (info.include_memory, info.include_lessons, info.include_project) == (
            False,
            False,
            True,
        )
        assert lc.trimmed_groups(info) == {"memory", "lessons"}

    def test_pinned_group_survives(self):
        info = SubagentInfo(id="a", task="t")
        info.delegation = {lc.PINNED_KEY: "memory"}
        lc.apply_trim(info, ["memory", "lessons"])
        assert info.include_memory is True and info.include_lessons is False
        assert lc.trimmed_groups(info) == {"lessons"}


class TestExplicitFlags:
    def test_api_delegation_pins_only_explicit_true(self):
        assert lc.spawn_delegation({}, "") == {}
        got = lc.spawn_delegation({"include_memory": True, "include_project": False}, "pytest -q")
        assert got == {"delegation": {"check": "pytest -q", lc.PINNED_KEY: "memory"}}

    def test_mcp_reads_explicit_true_before_defaults(self):
        from kiro_crew.mcp_tools.spawn import _explicit_true_groups

        assert _explicit_true_groups({"task": "x"}) == ()
        assert _explicit_true_groups({"include_lessons": True, "include_memory": False}) == (
            "include_lessons",
        )


# ---------------------------------------------------------------------------
# Through routed_spawn
# ---------------------------------------------------------------------------


def _fake_tiers(vendor, config=None):
    return {
        "claude": {"top": [], "large": ["claude-opus-5"], "small": ["claude-sonnet-5"], "mini": []},
    }.get(vendor, {})


BACKENDS = {
    "local/subagent": {"backend": "claude", "runtime_agent": "local-background", "native": ""},
    "local/agent": {"backend": "claude", "runtime_agent": "local-fleet", "native": ""},
}


class _Oracle:
    def __init__(self, value):
        self.value, self.states = value, []

    async def ask(self, state, questions):
        self.states.append(state)
        return {q.id: Answer(id=q.id, value=self.value, p=0.8, note="") for q in questions}


@pytest.fixture
def routed_env(tmp_path, monkeypatch):
    import kiro_crew.decisions.gate as gate_mod

    monkeypatch.setattr(vl, "_data_home", lambda: tmp_path)
    vl._reset_for_tests()
    monkeypatch.setattr(sr, "_limits_cache", None)
    monkeypatch.setattr(sr, "_agy_models_cache", None)
    consent = tmp_path / "decisions_consent.json"
    consent.write_text(
        json.dumps({"enabled": True, "endpoint": DECISION_PROVIDER_ENDPOINT_DEFAULT}),
        encoding="utf-8",
    )
    monkeypatch.setattr("kiro_crew.config.loader.decisions_consent_path", lambda: consent)
    monkeypatch.setattr(log_mod, "log_dir", lambda: tmp_path / "decisions")
    monkeypatch.setattr(
        gate_mod, "_snapshot", lambda: SimpleNamespace(decisions=DecisionsConfig(bucket=100))
    )
    monkeypatch.setattr(sr, "effective_tiers", _fake_tiers)
    monkeypatch.setattr(sr, "route_backends", lambda parent_vendor, cfg=None: dict(BACKENDS))
    monkeypatch.setattr(
        sr,
        "limit_health",
        lambda models=(), now=None: {
            "local": {
                "out": False,
                "lanes": {"subagent": True, "agent": True},
                "ctx": {"subagent": 32768, "agent": 131072},
            },
        },
    )

    def install(oracle):
        monkeypatch.setattr(gate_mod, "_oracle", lambda *a, **k: oracle)
        return oracle

    return install


def _route(brief, **kw):
    return asyncio.run(
        sr.routed_spawn(
            brief, agent="crew-claude", parent_model="claude-opus-5", session_key="s1", **kw
        )
    )


class TestRouted:
    def test_gpu_pick_keeps_everything(self, routed_env):
        routed_env(_Oracle("local/agent/0"))
        got = _route(MID, context_groups=ALL)
        assert got["vendor"] == "local" and got["tier"] == "agent"
        assert got["trim"] == [] and "trimmed" not in sr.reason_line(got)

    def test_cpu_pick_trims_memory_then_lessons(self, routed_env):
        oracle = routed_env(_Oracle("local/subagent/0"))
        got = _route(SHORT, context_groups=frozenset({"memory", "project"}))
        assert got["trim"] == ["memory"]
        got = _route(MID, context_groups=ALL)
        assert got["trim"] == ["memory", "lessons"]
        assert "local: trimmed memory, lessons to fit 12.0k" in sr.reason_line(got)
        # the oracle read the fitted size, not the untrimmed one
        est = oracle.states[-1]["estimate_tokens"]["subagent"]
        assert est == lc.brief_tokens(MID) + 4050 + lc.context_tokens(
            frozenset({"project"}), "subagent"
        )

    def test_explicit_include_is_respected(self, routed_env):
        oracle = routed_env(_Oracle("local/subagent/0"))
        # memory pinned: it stays, and the run no longer fits the CPU target
        got = _route(SHORT, context_groups=ALL, pinned_groups=frozenset({"memory"}))
        keys = {c["key"] for c in oracle.states[-1]["candidates"]}
        assert "local/subagent/0" not in keys and "local/agent/0" in keys
        assert got is None or got.get("tier") != "subagent"
        # both pinned: the same
        got = _route(SHORT, context_groups=ALL, pinned_groups=frozenset({"memory", "lessons"}))
        keys = {c["key"] for c in oracle.states[-1]["candidates"]}
        assert "local/subagent/0" not in keys and "local/agent/0" in keys
        assert got is None or got.get("tier") != "subagent"

    def test_too_big_after_trimming_is_skipped(self, routed_env):
        oracle = routed_env(_Oracle("claude/large/0"))
        got = _route(HUGE, context_groups=ALL)
        keys = {c["key"] for c in oracle.states[-1]["candidates"]}
        assert "local/subagent/0" not in keys
        assert got["vendor"] == "claude" and got["too_big"] == ["subagent"]
        assert "local subagent skipped: too big" in sr.reason_line(got)

    def test_cloud_pick_is_untouched(self, routed_env):
        routed_env(_Oracle("claude/small/0"))
        got = _route(MID, context_groups=ALL)
        assert got["vendor"] == "claude" and got["trim"] == [] and got["trim_budget"] == 0
        assert "trimmed" not in sr.reason_line(got)


class TestSuccessorScope:
    def _info(self):
        info = SubagentInfo(id="a", task="t")
        info.execution_context = SimpleNamespace(app="", memory_mode="", to_record=lambda: {})
        return info

    def _spawn(self, info, routed):
        seen = {}

        class _Mgr:
            async def spawn_async(self, brief, **kw):
                seen.update(kw)
                return SimpleNamespace(done=False, error="", id="b")

        asyncio.run(
            qs.spawn_successor(
                _Mgr(), info, routed, "handoff", chain=("local",), origin="t", switch="s"
            )
        )
        return seen

    def test_cloud_successor_gets_the_trimmed_groups_back(self):
        info = self._info()
        lc.apply_trim(info, ["memory", "lessons"])
        seen = self._spawn(info, {"model": "claude-opus-5", "tier": "large", "vendor": "claude"})
        assert seen["include_memory"] is True and seen["include_lessons"] is True
        assert lc.TRIMMED_KEY not in seen["delegation"]

    def test_local_successor_is_trimmed_before_it_starts(self):
        info = self._info()
        info.delegation = {lc.PINNED_KEY: "lessons"}
        seen = self._spawn(
            info,
            {
                "model": "subagent",
                "tier": "subagent",
                "vendor": "local",
                "trim": ["memory", "lessons"],
            },
        )
        assert seen["include_memory"] is False and seen["include_lessons"] is True
        assert seen["delegation"][lc.TRIMMED_KEY] == "memory"
