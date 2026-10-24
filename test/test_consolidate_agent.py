"""History consolidation runs under its own agent identity.

The identity exists so deployments can route consolidation's whole-session-
tail payload to a large-context engine while kirocrew-lite's micro-jobs
(titles, link labels) stay on the cheap seat. Whole-session tails require a
larger context budget than those micro-jobs.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.agent import _CONSOLIDATE_AGENT_FILENAME, _install_consolidate_agent


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    return d


class TestConsolidateAgent:
    def test_installer_writes_bare_toolless_spec(self, agents_dir: Path) -> None:
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            _install_consolidate_agent()

        spec = json.loads((agents_dir / _CONSOLIDATE_AGENT_FILENAME).read_text())
        assert spec["name"] == "kirocrew-consolidate"
        assert spec["tools"] == []
        assert spec["mcpServers"] == {}
        assert spec["includeMcpJson"] is False
        assert spec["prompt"] == ""

    def test_consolidation_call_uses_its_own_identity(self) -> None:
        """Pin the identity at the call site: falling back to the shared lite
        identity would silently re-inherit lite's small-context routing."""
        # The identity is a constructor default the call site forwards, and both
        # live in ``history_consolidation`` -- ``history`` is only a facade.
        import kiro_crew.history_consolidation as history_consolidation
        from kiro_crew.history import HistoryConsolidator

        src = inspect.getsource(history_consolidation)
        assert 'consolidation_agent: str = "kirocrew-consolidate"' in src
        assert "agent=self._consolidation_agent" in inspect.getsource(HistoryConsolidator._call_llm)

    def test_consolidation_uses_its_own_session_key(self) -> None:
        """Keep a dedicated key so consolidation binds its own agent identity.

        The shared ``_bg`` session starts as kirocrew-lite; get_or_create keeps
        the existing key's agent rather than applying a new ``agent`` argument.
        """
        from kiro_crew.history import _CONSOLIDATE_SESSION_KEY
        from kiro_crew.session import BACKGROUND_KEY

        assert _CONSOLIDATE_SESSION_KEY != BACKGROUND_KEY
        from kiro_crew.history import HistoryConsolidator

        # Shared-store turns bind the dedicated key; private V2 turns let
        # background_turn mint and retire their store-bound key.
        src = inspect.getsource(HistoryConsolidator._call_llm)
        assert "session_key=_CONSOLIDATE_SESSION_KEY if not memory_store else None" in src

    def test_consolidation_session_is_bounded(self) -> None:
        import kiro_crew.history as history
        from kiro_crew.history import HistoryConsolidator
        from kiro_crew.session import CONSOLIDATE_KEY

        call_llm_src = inspect.getsource(HistoryConsolidator._call_llm)
        assert "fresh_conversation=True" in call_llm_src
        assert history._CONSOLIDATE_SESSION_KEY == CONSOLIDATE_KEY

    def test_consolidate_key_registered_stateless(self) -> None:
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.session import CONSOLIDATE_KEY, SessionManager

        manager = SessionManager(KiroCrewConfig())
        assert CONSOLIDATE_KEY in manager._allocation_deps().constants.stateless_keys
