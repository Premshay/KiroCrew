"""DeepseekHarness delegates shared-runtime launches to the verified gate adapter.

The adapter's gate read-back is covered by the dedicated DeepSeek backend suite;
these tests pin the shared wrapper's context and plan handoff without booting dsh.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnContext, SpawnPlan, TeardownPolicy
from kiro_crew.acp.types import (
    ACP_BACKEND_DEEPSEEK,
    ACP_CLIENT_CAPABILITIES,
    METHOD_CANCEL,
)


def _ctx(tmp_path, *, model: str | None = None) -> SpawnContext:
    return SpawnContext(
        agent="crew-deepseek-pro",
        work_dir=str(tmp_path),
        model=model,
        environ={},
        home=tmp_path,
    )


# ── Seam 1: spawn ──


@pytest.mark.asyncio
async def test_spawn_uses_the_verified_plan_and_runtime_environment(monkeypatch, tmp_path):
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    verified = SpawnPlan(
        ["/bin/dsh", "--profile", "acp", "--patch", "/sealed/patch"],
        extra_hidden_dirs=("/masked-home",),
    )
    resolve = AsyncMock(return_value=verified)
    monkeypatch.setattr(harness._launch, "resolve_spawn", resolve)
    ctx = _ctx(tmp_path)
    ctx = dataclasses.replace(ctx, extra_env={"DSH_HOME": str(tmp_path / "dsh")})
    plan = await harness.resolve_spawn(ctx)
    assert plan.argv == verified.argv
    assert plan.extra_hidden_dirs == verified.extra_hidden_dirs
    passed = resolve.await_args.args[0]
    assert passed.session._extra_env == {"DSH_HOME": str(tmp_path / "dsh")}
    assert passed.session._spawn_work_dir == str(tmp_path)


@pytest.mark.asyncio
async def test_the_model_rides_the_plan_not_argv(monkeypatch, tmp_path):
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    resolve = AsyncMock(return_value=SpawnPlan(["/bin/dsh", "--profile", "acp"]))
    monkeypatch.setattr(harness._launch, "resolve_spawn", resolve)
    plan = await harness.resolve_spawn(_ctx(tmp_path, model="deepseek-v4-pro"))
    assert "--model" not in plan.argv
    assert plan.session_model == "deepseek-v4-pro"


def test_spawn_env_strips_the_kiro_key_and_pins_the_permission_mode(monkeypatch):
    from kiro_crew.config import loader as loader_mod

    calls: list[str] = []
    monkeypatch.setattr(
        loader_mod, "strip_kiro_cli_api_key", lambda env: calls.append("strip"), raising=False
    )
    env = {"KIRO_API_KEY": "secret", "DSH_PERMISSION_MODE": "unconfined"}
    harness_for(ACP_BACKEND_DEEPSEEK).apply_spawn_env(env)
    assert calls == ["strip"]
    assert env["DSH_PERMISSION_MODE"] == "workspace-write"


# ── Seam 2: initialize ──


def test_protocol_version_is_the_spec_dialect():
    version = harness_for(ACP_BACKEND_DEEPSEEK).protocol_version
    assert isinstance(version, int) and version == 1


def test_client_capabilities_are_the_shared_constant():
    assert harness_for(ACP_BACKEND_DEEPSEEK).client_capabilities == ACP_CLIENT_CAPABILITIES


# ── Seam 3: sessions ──


@pytest.mark.asyncio
async def test_session_extras_are_empty(tmp_path):
    from kiro_crew.acp.harness.base import SessionExtras

    extras = await harness_for(ACP_BACKEND_DEEPSEEK).session_extras(
        "crew-deepseek-pro", work_dir=str(tmp_path)
    )
    assert extras == SessionExtras(custom_agents=None)


def test_the_mcp_array_passes_through_unchanged():
    requested = [{"name": "a", "command": "x"}]
    out = harness_for(ACP_BACKEND_DEEPSEEK).session_mcp_servers(requested, agent_capabilities={})
    assert out is requested


# ── Seams 4-6: callbacks, aliases, teardown ──


def test_the_host_answers_no_inbound_request():
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    assert harness.host_answered_methods == ()


@pytest.mark.asyncio
async def test_answer_request_raises_for_an_unclaimed_method():
    with pytest.raises(NotImplementedError):
        await harness_for(ACP_BACKEND_DEEPSEEK).answer_request("some/other")


def test_teardown_is_a_cancel_notification():
    teardown = harness_for(ACP_BACKEND_DEEPSEEK).teardown
    assert teardown == TeardownPolicy(method=METHOD_CANCEL, notification=True)


# ── Membership seams (lookup-shaped, answered by the sets) ──


def test_membership_seams_read_the_sets():
    """The three membership answers the contract does not already parametrise
    for every host; ``reads_markdown_agent_specs`` is covered there."""
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    assert harness.internal_sandbox is False
    assert harness.pod_home_remap is False
    assert harness.verifies_agent_activation is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
