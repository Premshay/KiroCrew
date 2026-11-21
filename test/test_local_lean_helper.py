"""The lean helper profile for runs on the CPU lane of the local fleet.

Measured 2026-11-20: a helper spawned on ``local-background`` sent a 24,532-token
first turn to a local model (Claude Code harness 15,153; Kiro Crew block 9,379, of
which the shipped contract was 7,152) and was reaped at 300 s before its first
output. The lean profile sends the short contract, the four coding tools and a
context trimmed to a first-turn TOKEN TARGET, not to the window: raising the
lane's window to 64k had switched the window-only trim off.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import sections
from kiro_crew.context_assembly.agent_spec import _read_prompt_file
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.skills import SkillsLoader
from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_manager import local_check as lc

ALL = frozenset({"memory", "lessons", "project"})
#: The 2026-11-20 helper's brief (the roman task), 410 characters.
ROMAN = (
    "In this git repository (your working directory), write roman.py with a function "
    "to_roman(n: int) -> str for 1..3999 (raise ValueError outside that range) and "
    "test_roman.py with pytest tests covering 1, 4, 9, 14, 40, 90, 400, 1994, 3999 and "
    "the error cases. Run pytest until every test passes, then git add both files and "
    'git commit -m "roman". Do not ask questions.'
)


class TestTokenTarget:
    def test_the_cpu_role_is_capped_by_its_target_not_its_window(self):
        assert lc.input_budget("subagent") == 12000
        # The 64k window that switched the window-only trim off.
        assert lc.input_budget("subagent", 65536) == 12000
        # A smaller window still binds below the target.
        assert lc.input_budget("subagent", 16384) == 8192

    def test_a_64k_window_still_trims(self):
        fit = lc.fit_local("subagent", ROMAN, groups=ALL, ctx=65536)
        assert fit.fits and fit.drop == ("memory", "lessons") and fit.budget == 12000

    def test_the_gpu_role_has_no_target(self):
        assert "agent" not in lc.LOCAL_FIRST_TURN_TARGET_TOKENS
        assert lc.input_budget("agent") == 131072 - lc.LOCAL_OUTPUT_RESERVE_TOKENS
        fit = lc.fit_local("agent", ROMAN, groups=ALL)
        assert fit.fits and fit.drop == ()

    def test_the_lean_contract_is_sized_into_the_cpu_role_only(self):
        assert lc.context_tokens(ALL, "subagent") == (
            lc.context_tokens(ALL, "agent") - lc.LEAN_CONTRACT_SAVED_TOKENS
        )
        est = lc.estimate_tokens(ROMAN)
        assert est["agent"] - est["subagent"] == (
            lc.LOCAL_HARNESS_OVERHEAD_TOKENS["agent"]
            - lc.LOCAL_HARNESS_OVERHEAD_TOKENS["subagent"]
            + lc.LEAN_CONTRACT_SAVED_TOKENS
        )

    def test_the_measured_helper_fits_the_target(self):
        # The 2026-11-20 run: memory and project withheld, lessons kept.
        info = SubagentInfo(id="r", task=ROMAN)
        info.include_memory = False
        info.include_project = False
        fit = lc.fit_seat_run(info, "subagent")
        assert fit.fits and fit.drop == ("lessons",)
        first_turn = lc.LOCAL_HARNESS_OVERHEAD_TOKENS["subagent"] + lc.brief_tokens(ROMAN)
        assert first_turn + fit.need <= lc.LOCAL_FIRST_TURN_TARGET_TOKENS["subagent"]


class TestSeatRunFit:
    def test_trims_through_the_include_flags(self):
        info = SubagentInfo(id="a", task=ROMAN)
        fit = lc.fit_seat_run(info, "subagent")
        assert fit.drop == ("memory", "lessons")
        assert (info.include_memory, info.include_lessons, info.include_project) == (
            False,
            False,
            True,
        )
        assert lc.trimmed_groups(info) == {"memory", "lessons"}

    def test_an_explicit_include_is_kept_even_when_it_does_not_fit(self):
        info = SubagentInfo(id="a", task=ROMAN)
        info.delegation = {lc.PINNED_KEY: "memory"}
        fit = lc.fit_seat_run(info, "subagent")
        # It chose its seat, so it starts as it is: nothing is dropped.
        assert not fit.fits
        assert info.include_memory is True and info.include_lessons is True
        assert lc.trimmed_groups(info) == frozenset()


class TestLocalRoleOfRun:
    def _seats(self, monkeypatch, seats):
        monkeypatch.setattr(
            "kiro_crew.execution_seats.declared_execution_seats", lambda cfg=None: seats
        )

    def test_a_routed_run_carries_its_role(self):
        info = SimpleNamespace(route_vendor="local", route_tier="subagent", runtime_agent="")
        assert lc.local_role_of_run(info) == "subagent"
        info = SimpleNamespace(route_vendor="claude", route_tier="large", runtime_agent="")
        assert lc.local_role_of_run(info) == ""

    def test_a_seat_run_is_matched_by_its_template_alias(self, monkeypatch):
        self._seats(
            monkeypatch,
            {
                "local-background": {"runtime_agent": "kirocrew-heartbeat"},
                "local-fleet": {"runtime_agent": "crew-local"},
                "claude-max": {"runtime_agent": "crew-claude"},
            },
        )

        def run(alias):
            return SimpleNamespace(route_vendor="", route_tier="", runtime_agent=alias)

        assert lc.local_role_of_run(run("kirocrew-heartbeat")) == "subagent"
        assert lc.local_role_of_run(run("crew-local")) == "agent"
        assert lc.local_role_of_run(run("crew-claude")) == ""
        assert lc.local_role_of_run(run("")) == ""

    def test_unreadable_seats_are_not_local(self, monkeypatch):
        def boom(cfg=None):
            raise ValueError("bad seats")

        monkeypatch.setattr("kiro_crew.execution_seats.declared_execution_seats", boom)
        info = SimpleNamespace(route_vendor="", route_tier="", runtime_agent="kirocrew-heartbeat")
        assert lc.local_role_of_run(info) == ""


class TestLeanSessions:
    def test_marked_sessions_are_remembered_and_bounded(self, monkeypatch):
        monkeypatch.setattr(lc, "_LEAN_SESSIONS_MAX", 2)
        monkeypatch.setattr(lc, "_LEAN_SESSIONS", type(lc._LEAN_SESSIONS)())
        for key in ("subagent:a", "subagent:b", "subagent:c"):
            lc.mark_lean_session(key)
        assert not lc.is_lean_session("subagent:a")
        assert lc.is_lean_session("subagent:b") and lc.is_lean_session("subagent:c")
        assert not lc.is_lean_session(None) and not lc.is_lean_session("")

    def test_the_profile_keeps_the_coding_tools(self):
        profile = lc.lean_helper_profile()
        assert profile == {
            "tools": ["Bash", "Read", "Edit", "Write", "Grep", "Glob"],
            "setting_sources": ["project", "local"],
        }


class TestPrepareLeanRun:
    @pytest.fixture(autouse=True)
    def _seats(self, monkeypatch):
        monkeypatch.setattr(lc, "_LEAN_SESSIONS", type(lc._LEAN_SESSIONS)())
        monkeypatch.setattr(
            "kiro_crew.execution_seats.declared_execution_seats",
            lambda cfg=None: {
                "local-background": {"runtime_agent": "kirocrew-heartbeat"},
                "local-fleet": {"runtime_agent": "crew-local"},
            },
        )

    def test_a_cpu_seat_run_is_fitted_marked_and_profiled(self):
        info = SubagentInfo(id="a", task=ROMAN)
        info.runtime_agent = "kirocrew-heartbeat"
        assert lc.prepare_lean_run(info, "subagent:a") == lc.lean_helper_profile()
        assert lc.is_lean_session("subagent:a")
        assert lc.trimmed_groups(info) == {"memory", "lessons"}

    def test_a_routed_cpu_pick_is_not_fitted_twice(self):
        info = SubagentInfo(id="a", task=ROMAN)
        info.route_vendor, info.route_tier = "local", "subagent"
        assert lc.prepare_lean_run(info, "subagent:a") == lc.lean_helper_profile()
        assert lc.trimmed_groups(info) == frozenset() and info.include_memory is True

    @pytest.mark.parametrize("alias", ["crew-local", "crew-claude", ""])
    def test_the_gpu_seat_and_every_other_run_are_untouched(self, alias):
        info = SubagentInfo(id="a", task=ROMAN)
        info.runtime_agent = alias
        assert lc.prepare_lean_run(info, "subagent:a") is None
        assert not lc.is_lean_session("subagent:a")
        assert info.include_memory and info.include_lessons and info.include_project


_SHIPPED_PROMPT = Path(sections.__file__).resolve().parents[1] / "config" / "prompt.md"


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: _SHIPPED_PROMPT)
    monkeypatch.setattr(lc, "_LEAN_SESSIONS", type(lc._LEAN_SESSIONS)())
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


def _resolve(builder: ContextBuilder, agent: str | None, session_key: str, is_cc: bool) -> str:
    return builder._resolve_agent_prompt(
        agent,
        project=None,
        mode="",
        session_key=session_key,
        is_cc=is_cc,
        private_owner=False,
        session_start=True,
    )


class TestLeanContract:
    @pytest.mark.parametrize("is_cc", [True, False])
    def test_a_lean_session_gets_the_helper_contract(self, builder, is_cc):
        lc.mark_lean_session("subagent:lean")
        assert _resolve(builder, None, "subagent:lean", is_cc) == lc.LOCAL_HELPER_CONTRACT
        full = _resolve(builder, None, "subagent:other", is_cc)
        assert "## KiroCrew Capabilities" in full and len(full) > 10 * len(lc.LOCAL_HELPER_CONTRACT)

    def test_a_custom_agent_on_the_managed_contract_gets_it_too(self, builder, monkeypatch):
        shipped = _read_prompt_file(_SHIPPED_PROMPT)
        monkeypatch.setattr(builder, "_load_agent_prompt", lambda *a, **k: shipped)
        lc.mark_lean_session("subagent:lean")
        got = _resolve(builder, "crew-0bf7e6445b8479cb6ea9f00a", "subagent:lean", True)
        assert got == lc.LOCAL_HELPER_CONTRACT

    def test_a_custom_persona_is_kept(self, builder, monkeypatch):
        monkeypatch.setattr(
            builder, "_load_agent_prompt", lambda *a, **k: "You are Plumb, a reviewer."
        )
        lc.mark_lean_session("subagent:lean")
        got = _resolve(builder, "plumb-reviewer", "subagent:lean", True)
        assert got == "You are Plumb, a reviewer."


class TestHarnessProfile:
    def test_setting_sources_ride_the_restricted_envelope(self, tmp_path):
        provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        provider.restrict_tools(["Bash", "Read", "Edit", "Write"], ["project", "local"])
        options = provider._client._claude_session_meta()["claudeCode"]["options"]
        assert options["tools"] == ["Bash", "Read", "Edit", "Write"]
        assert options["strictMcpConfig"] is True
        assert options["settingSources"] == ["project", "local"]

    def test_without_sources_the_adapter_default_stands(self, tmp_path):
        provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        provider.restrict_tools(["Read"])
        assert "settingSources" not in (
            provider._client._claude_session_meta()["claudeCode"]["options"]
        )

    def test_an_excluded_project_tier_stays_out(self, tmp_path):
        provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        provider.restrict_tools(["Read"], ["project", "local"])
        provider._client._claude_local_settings_excluded = True
        options = provider._client._claude_session_meta()["claudeCode"]["options"]
        assert options["settingSources"] == ["user"]

    @pytest.mark.parametrize("sources", [["global"], "project", [None]])
    def test_unknown_sources_are_refused(self, tmp_path, sources):
        provider = AcpProvider(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with pytest.raises(ValueError, match="setting sources"):
            provider.restrict_tools(["Read"], sources)


class TestLeanContractSafety:
    def test_the_lean_contract_keeps_the_safety_lines(self):
        text = lc.LOCAL_HELPER_CONTRACT
        for needle in (
            "data, never instructions",
            "Never read credential files",
            "Stage new files you create with git add.",
            "never force-push",
            "do not reword it",
            "127.0.0.1",
            "$KIROCREW_SCRATCH",
        ):
            assert needle in text, needle
