"""History consolidation runs under its own agent identity.

The identity exists so deployments can route consolidation's whole-session-
tail payload to a large-context engine while kirocrew-lite's micro-jobs
(titles, link labels) stay on the cheap seat. Observed 2026-08-11: on the
shared lite identity, 4/4 consolidation attempts were rejected as oversized
by a 32k lane while lite's titles succeeded on the same lane.
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
        assert spec["prompt"] == ""

    def test_consolidation_call_uses_its_own_identity(self) -> None:
        """Pin the identity at the call site: falling back to the shared lite
        identity would silently re-inherit lite's small-context routing."""
        import kiro_crew.history as history

        src = inspect.getsource(history)
        assert 'agent="kirocrew-consolidate"' in src

    def test_consolidation_uses_its_own_session_key(self) -> None:
        """Pin the dedicated session key: under the shared BACKGROUND_KEY the
        agent identity never binds (the ``_bg`` session is cold-started at
        gateway startup as kirocrew-lite, and get_or_create ignores ``agent``
        for an existing key) — verified live 2026-08-11 when the rerouted
        identity produced zero factory traffic."""
        from kiro_crew.history import _CONSOLIDATE_SESSION_KEY
        from kiro_crew.session import BACKGROUND_KEY

        assert _CONSOLIDATE_SESSION_KEY != BACKGROUND_KEY
        # ``history`` re-exports the name, but the ``background_turn`` call that
        # binds it is in ``history_consolidation`` — read the module that holds
        # the call site, or the assertion inspects a facade and always passes.
        import kiro_crew.history_consolidation as history_consolidation

        src = inspect.getsource(history_consolidation)
        assert "session_key=_CONSOLIDATE_SESSION_KEY" in src

    def test_consolidation_session_is_bounded(self) -> None:
        import kiro_crew.history as history
        from kiro_crew.history import HistoryConsolidator
        from kiro_crew.session import CONSOLIDATE_KEY

        call_llm_src = inspect.getsource(HistoryConsolidator._call_llm)
        assert "reset_conversation=True" in call_llm_src
        assert history._CONSOLIDATE_SESSION_KEY == CONSOLIDATE_KEY

    def test_consolidate_key_registered_stateless(self) -> None:
        # The skip-resume decision lives in the allocation service and names the
        # key through the constants it is handed, so both halves are pinned: the
        # allocator consults ``consolidate_key``, and the manager binds that slot
        # to ``CONSOLIDATE_KEY``. Either half alone lets the key resume.
        from kiro_crew.session_allocation import SessionAllocationService

        assert "constants.consolidate_key" in inspect.getsource(
            SessionAllocationService.get_or_create
        )

        import kiro_crew.session as session

        assert "consolidate_key=CONSOLIDATE_KEY" in inspect.getsource(
            session.SessionManager._allocation_deps
        )
