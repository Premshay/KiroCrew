"""Refuse a known adapter that contradicts the selected harness at initialize."""

from __future__ import annotations

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
    POLICY_ID_BY_BACKEND,
)
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

_REPORTED_BACKENDS = {
    "Kiro CLI Agent": ACP_BACKEND_KIRO,
    "@agentclientprotocol/claude-agent-acp": ACP_BACKEND_CLAUDE,
    "@agentclientprotocol/codex-acp": ACP_BACKEND_CODEX,
    "OpenCode": ACP_BACKEND_OPENCODE,
    "goose": ACP_BACKEND_GOOSE,
    "pi-acp": ACP_BACKEND_PI,
    "deepseek-harness-acp": ACP_BACKEND_DEEPSEEK,
    "Copilot": ACP_BACKEND_COPILOT,
    "@strands-agents/cli": ACP_BACKEND_STRANDS,
}


def refuse_conflicting_handshake(backend: str, result: object) -> None:
    """Known conflicting identity is a configuration refusal, never a reconnect.

    Names are adapter declarations, not authentication. Missing/unknown names
    preserve compatibility with KAS and operator adapters; they prove no identity.
    An honest dispatcher reporting another known harness must not continue under
    the selected harness's permission route or populate its model cache.
    """
    info = result.get("agentInfo") if isinstance(result, dict) else None
    name = info.get("name") if isinstance(info, dict) else None
    if not isinstance(name, str) or name not in _REPORTED_BACKENDS:
        return
    if _REPORTED_BACKENDS[name] == backend:
        return
    selected = POLICY_ID_BY_BACKEND.get(backend, backend)
    raise AcpToolGateUnroutable(
        f"ACP adapter reports {name!r}, but this launch selected {selected!r}. "
        "Correct the configured adapter wrapper or select the matching harness; "
        "no session was opened."
    )
