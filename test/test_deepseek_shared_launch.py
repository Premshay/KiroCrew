"""Shared DeepSeek launches use the native launch preparation and credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import deepseek as native
from kiro_crew.acp.harness.base import SpawnContext, SpawnPlan
from kiro_crew.acp.harness.deepseek_shared import DeepseekHarness
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable


@pytest.mark.asyncio
async def test_shared_launch_supplies_the_native_preparation_surface(tmp_path, monkeypatch):
    harness = DeepseekHarness()
    seen = []

    async def prepare(ctx):
        session = ctx.session
        assert isinstance(session, AcpClient)
        assert callable(session._prepare_session_mcp)
        assert callable(session._agent_env_for_spawn)
        assert session.backend == harness.backend
        assert session._agent == "reviewer"
        assert session._spawn_work_dir == str(tmp_path)
        assert session._extra_env == {"TEST_ROUTE": "local"}
        assert session._process is None
        seen.append(session)
        return SpawnPlan(argv=["dsh", "acp"], spawn_label="dsh acp", stderr_label="dsh")

    monkeypatch.setattr(harness._launch, "resolve_spawn", prepare)
    plan = await harness.resolve_spawn(
        SpawnContext(
            agent="reviewer",
            work_dir=tmp_path,
            model="selected-model",
            environ={},
            home=Path(tmp_path),
            extra_env={"TEST_ROUTE": "local"},
        )
    )
    assert len(seen) == 1
    assert plan.session_model == "selected-model"


def test_shared_launch_injects_vault_values_and_releases_the_resolver_copy(monkeypatch):
    resolved = {"DEEPSEEK_API_KEY": "unit-test-value"}
    calls = []

    def resolve():
        calls.append(True)
        return resolved, ()

    monkeypatch.setattr(native, "_deepseek_vault_env", resolve)
    env = {"KIRO_API_KEY": "unrelated-host-value"}
    DeepseekHarness().apply_spawn_env(env)
    assert "KIRO_API_KEY" not in env
    assert env["DEEPSEEK_API_KEY"] == "unit-test-value"
    assert resolved == {}
    assert calls == [True]


def test_shared_launch_refuses_a_broken_vault_mapping(monkeypatch):
    def refuse():
        raise ValueError("configured provider key is unavailable")

    monkeypatch.setattr(native, "_deepseek_vault_env", refuse)
    with pytest.raises(AcpToolGateUnroutable, match="configured provider key"):
        DeepseekHarness().apply_spawn_env({})
