"""DeepseekHarness: the seam answers for the dsh ACP host.

The deepseek host is the operator-owned UNVERIFIED seat: no credential mask on
either transport, the host's own workspace-write sandbox deciding tool calls,
and reachable only through an engine map entry. What is pinned here is that the
runtime harness answers every seam exactly like the client transport already
serves the same seat -- same argv, same permission-mode pin, same no-mask
posture -- so a review worker started through either transport sees one
environment.

A real dsh cannot stand in for the full exchange here for the same reason the
codex suite uses a fake peer: a live probe is only taken as far as the transport
allows. The live-runtime probe lives in the operator's own verification, not in
this file. The seam-level contract every harness answers is parametrised in
``test_acp_harness_contract.py``; deepseek is included there.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.harness import deepseek as harness_mod
from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.harness.base import SpawnContext, TeardownPolicy
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.acp.types import (
    ACP_BACKEND_DEEPSEEK,
    ACP_CLIENT_CAPABILITIES,
    METHOD_CANCEL,
    METHOD_SESSION_UPDATE,
)


def _ctx(tmp_path, *, model: str | None = None) -> SpawnContext:
    return SpawnContext(
        agent="crew-deepseek-pro",
        work_dir=str(tmp_path),
        model=model,
        environ={},
        home=tmp_path,
    )


def _pin_binary(monkeypatch, value):
    """Pin the resolver AND drop its cache: the client caches the first real
    resolution, so a patch aimed at the function alone is unreachable."""
    monkeypatch.setattr(client_mod, "_deepseek_bin_cache", client_mod._UNRESOLVED)
    monkeypatch.setattr(client_mod, "_resolve_deepseek_bin", value)


# ── Seam 1: spawn ──


@pytest.mark.asyncio
async def test_spawn_argv_is_the_binary_plus_the_profile_selector(monkeypatch, tmp_path):
    _pin_binary(monkeypatch, lambda: ("/bin/dsh", "/s"))
    plan = await harness_for(ACP_BACKEND_DEEPSEEK).resolve_spawn(_ctx(tmp_path))
    assert plan.argv == ["/bin/dsh", "--profile", "acp"]
    # No credential mask on either transport: the UNVERIFIED seat's documented
    # posture is the harness's own sandbox deciding tool calls.
    assert plan.extra_hidden_dirs == ()
    assert plan.extra_expose_files == ()


@pytest.mark.asyncio
async def test_the_model_rides_the_plan_not_argv(monkeypatch, tmp_path):
    _pin_binary(monkeypatch, lambda: ("/bin/dsh", "/s"))
    plan = await harness_for(ACP_BACKEND_DEEPSEEK).resolve_spawn(
        _ctx(tmp_path, model="deepseek-v4-pro")
    )
    assert "--model" not in plan.argv
    assert plan.session_model == "deepseek-v4-pro"


@pytest.mark.asyncio
async def test_a_missing_binary_aborts_the_spawn_with_the_install_remedy(monkeypatch, tmp_path):
    _pin_binary(monkeypatch, lambda: (None, "/nowhere"))
    with pytest.raises(AcpRuntimeError, match="dsh not found"):
        await harness_for(ACP_BACKEND_DEEPSEEK).resolve_spawn(_ctx(tmp_path))


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
    out = harness_for(ACP_BACKEND_DEEPSEEK).session_mcp_servers(
        requested, agent_capabilities={}
    )
    assert out is requested


# ── Seams 4-6: callbacks, aliases, teardown ──


def test_the_host_answers_no_inbound_request():
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    assert harness.host_answered_methods == ()


@pytest.mark.asyncio
async def test_answer_request_raises_for_an_unclaimed_method():
    with pytest.raises(NotImplementedError):
        await harness_for(ACP_BACKEND_DEEPSEEK).answer_request("some/other")


def test_aliases_are_plain_acp_without_the_kiro_family_vocabulary():
    aliases = harness_for(ACP_BACKEND_DEEPSEEK).notification_aliases
    assert aliases.session_update == (METHOD_SESSION_UPDATE,)
    assert aliases.subagent_list_update == ""
    assert aliases.mcp_init == ()


def test_teardown_is_a_cancel_notification():
    teardown = harness_for(ACP_BACKEND_DEEPSEEK).teardown
    assert teardown == TeardownPolicy(method=METHOD_CANCEL, notification=True)


# ── Membership seams (lookup-shaped, answered by the sets) ──


def test_membership_seams_read_the_sets():
    harness = harness_for(ACP_BACKEND_DEEPSEEK)
    assert harness.internal_sandbox is False
    assert harness.pod_home_remap is False
    assert harness.reads_markdown_agent_specs is False
    assert harness.verifies_agent_activation is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
