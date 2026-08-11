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
        import kiro_crew.history as history

        src = inspect.getsource(history)
        assert "session_key = _CONSOLIDATE_SESSION_KEY" in src

    def test_consolidation_session_is_bounded(self) -> None:
        """Pins for the two adversarially-confirmed defects (2026-08-12):
        (1) the _consolidate session must not accumulate transcripts — the key
        is registered stateless and each reused turn resets the conversation;
        (2) _call_llm must not recycle the _bg session it no longer rides —
        that call could shut down _bg mid-turn under a bystander consumer."""
        import kiro_crew.history as history
        from kiro_crew.history import HistoryConsolidator
        from kiro_crew.session import CONSOLIDATE_KEY

        call_llm_src = inspect.getsource(HistoryConsolidator._call_llm)
        assert "self._sessions.recycle_background" not in call_llm_src
        assert "new_conversation" in call_llm_src
        assert history._CONSOLIDATE_SESSION_KEY == CONSOLIDATE_KEY

    def test_consolidate_key_registered_stateless(self) -> None:
        """The key must sit in get_or_create's stateless set: a session_map
        entry would resume the accumulated transcript across restarts."""
        import kiro_crew.session as session

        src = inspect.getsource(session)
        assert "key in (BACKGROUND_KEY, HEARTBEAT_KEY, CONSOLIDATE_KEY)" in src
