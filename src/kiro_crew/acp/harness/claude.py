"""claude-agent-acp, driven through the shared-process runtime.

``AcpProvider`` does not put claude on ``AcpRuntime``: ``ACP_BACKENDS_ACP_RUNTIME``
does not name it, and a dashboard claude session keeps its own ``AcpClient``. This
harness exists for the DIRECT runtime consumers -- the background session path and
app worker pools -- that construct ``AcpRuntime`` themselves and bind it to a
configured engine through ``platform.acp_binding``. Without an entry in the harness
registry those constructions are refused at spawn, which is the registry doing its
job; with one, they get the adapter protocol rather than kiro-cli's RPC shapes.

What this harness does NOT carry, stated so nobody reads it as parity with
``AcpClient``'s claude arm: it seeds no ``settings.local.json`` and builds no
mirror projection. ``AcpRuntime._refuse_unprojected_pooled_servers`` therefore
refuses a pooled MCP array for claude on this path, and the permission posture is
whatever the adapter resolves from its own settings sources.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from kiro_crew import acp_tool_gate
from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_CLIENT_CAPABILITIES,
    METHOD_CANCEL,
    METHOD_SESSION_UPDATE,
)

logger = logging.getLogger(__name__)

__all__ = ["ClaudeHarness"]


class ClaudeHarness(MembershipHarness):
    """The claude-agent-acp host."""

    backend = ACP_BACKEND_CLAUDE

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """The adapter's resolved entry, with the model and envelope it needs per session.

        No ``--agent`` and no ``--model``: the adapter takes neither on its command
        line. The model is applied to each session after ``session/new`` instead
        (:attr:`SpawnPlan.session_model`), because a direct runtime consumer has no
        provider behind it to re-apply one.

        The binary search is delegated to :mod:`kiro_crew.acp.client`, whose order
        (``CLAUDE_AGENT_ACP_BIN``, vendored copy, mise, PATH) is the operator-facing
        contract; a second copy here would drift into a second answer.
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.session_handle import AcpRuntimeError

        argv, search_path = await asyncio.to_thread(client_mod._resolve_claude_acp_bin)
        if not argv:
            raise AcpRuntimeError(
                f"{client_mod.CLAUDE_ACP_BIN} not found "
                f"({client_mod.describe_search_path(search_path)}). Install it with "
                f"'npm i -g {client_mod.CLAUDE_ACP_NPM_PKG}' or set CLAUDE_AGENT_ACP_BIN."
            )
        # The same refuse-then-mask preflight codex and ``AcpClient`` run, keyed on
        # the ROUTING rather than on this harness's identity. claude's seeded-settings
        # routing is not one this core enforces today, so the floor returns and the
        # mask comes back empty -- the spawn is unchanged. It is asked anyway so the
        # answer stays the gate's: if that routing ever becomes enforced, this spawn
        # refuses an unmasked tier and carries the mask without an edit here.
        hidden = await client_mod._run_preflight_bounded(
            client_mod._sandbox_preflight, ACP_BACKEND_CLAUDE, ctx.sandbox_mode
        )
        expose = acp_tool_gate.adapter_expose_files(ACP_BACKEND_CLAUDE, hidden)
        return SpawnPlan(
            argv=list(argv),
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
            session_model=ctx.model or None,
            session_meta={"claudeCode": {"options": {}}},
        )

    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Strip kiro-cli's API key and point the adapter at a ``claude`` binary.

        The key goes because a foreign adapter must never receive it. The
        executable is resolved because the adapter's SDK needs a native Claude
        binary it does not search PATH for; an operator-set value (ambient or from
        ``extra_env``) always wins. Runs inside the runtime's off-loop env hop, so
        the search does not block the loop.
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.config import loader as loader_mod

        loader_mod.strip_kiro_cli_api_key(env)
        if env.get("CLAUDE_CODE_EXECUTABLE"):
            return
        claude_exe = client_mod._resolve_claude_code_executable()
        if claude_exe:
            env["CLAUDE_CODE_EXECUTABLE"] = claude_exe
        else:
            logger.warning(
                "%s not found on PATH; set CLAUDE_CODE_EXECUTABLE for the adapter.",
                client_mod.CLAUDE_CODE_BIN,
            )

    @property
    def verifies_agent_activation(self) -> bool:
        """No -- nothing was selected at spawn for a later check to confirm."""
        return False

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        from kiro_crew.acp.client import PROTOCOL_VERSION_CLAUDE

        return PROTOCOL_VERSION_CLAUDE

    @property
    def client_capabilities(self) -> dict[str, Any]:
        return ACP_CLIENT_CAPABILITIES

    # ── Seam 3: session/new and session/load extras ──

    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: Any = None,
        member_dispatch: bool = False,
        session_key: str = "",
    ) -> SessionExtras:
        """Empty. The adapter has no custom-agent channel to register anything on."""
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The caller's list, unchanged -- what this path sent before the harness layer."""
        return requested

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None. The adapter holds its own credential and asks Crew for nothing."""
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        raise NotImplementedError(
            f"claude harness answers no inbound request, including {method!r}"
        )

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        """Plain ACP. The adapter sends no ``_kiro.dev`` spelling and no subagent roster."""
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """``session/cancel`` as a notification, as for codex.

        The adapter implements no kiro-family evict or delete verb, so sending one
        draws method-not-found while the session stays on the process. A cancel
        stops a turn still in flight, and the caller then drops the session locally.
        """
        return TeardownPolicy(method=METHOD_CANCEL, notification=True)
