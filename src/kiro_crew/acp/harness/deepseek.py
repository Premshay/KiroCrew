"""The DeepSeek Harness ACP profile: one ``dsh`` host, N sessions, own sandbox.

``dsh`` is a plugin host and ``acp`` is one of the profiles it boots, so the
argv is the harness's own binary plus the profile selector -- no adapter entry
script, no Node package to resolve at spawn time. The shipped profile is created
on first use and its bundles live inside the installed package's own dependency
closure, so a global install needs no workspace checkout.

**Routing is UNVERIFIED, and this harness inherits that posture.** ``dsh``'s own
workspace-write sandbox decides its tool calls; Kiro Crew's PreToolUse gate never
sees them, and ``session/request_permission`` carries only a model-initiated ask
to escalate past that sandbox. Crew pins ``DSH_PERMISSION_MODE`` so a variable
inherited from the operator's shell cannot select the unconfined mode, but that
is defence in depth: it does not make a tool call reach Crew's gate. The client
transport (:mod:`kiro_crew.acp.client`) starts the same host with no credential
mask and no permission overlay, and this harness deliberately matches it -- a
mask here would read as a control nothing enforces. This is also why deepseek is
absent from the selectable backend switch and reachable only through an engine
map entry, which the operator owns.

**The model rides the pipe, not argv.** dsh is a member of
``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION``, so a per-session model override is
pushed over ``session/set_config_option`` exactly like codex. The seat's
environment (``KIROCREW_DSH_MODEL``) supplies the boot default; without a
provider behind a direct runtime consumer to apply one, the harness carries the
caller's model through ``SpawnPlan.session_model``.

**Sessions, not agents.** dsh reads no ``~/.kiro/agents/<name>.json``: the
session's whole tool surface is the harness's own shipped tool set plus the
shared broker append the runtime composes for every mirror-less backend. So
there is no ``--agent`` flag to verify after spawn, and no spec to refuse over.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_DEEPSEEK,
    ACP_CLIENT_CAPABILITIES,
    METHOD_CANCEL,
    METHOD_SESSION_UPDATE,
)

__all__ = ["PROTOCOL_VERSION_DEEPSEEK", "DeepseekHarness"]

#: dsh answers ``initialize`` with an integer ``protocolVersion`` of 1, so it
#: speaks the SPEC dialect. Verified off its own wire; kept as this harness's OWN
#: literal even though the integer matches the sibling adapters' today: a
#: divergence should be a one-line edit here rather than a silent downgrade of
#: whichever harness moved first (harness-parity H10).
PROTOCOL_VERSION_DEEPSEEK = 1


class DeepseekHarness(MembershipHarness):
    """The dsh ACP host."""

    backend = ACP_BACKEND_DEEPSEEK

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """The host binary plus the profile selector, no mask.

        The binary resolution is delegated to :mod:`kiro_crew.acp.client` rather
        than copied. Its order is an operator-facing contract -- explicit
        ``DSH_BIN``, then mise, then PATH -- and two copies of it would drift
        into two different answers to "why did it pick that one?".

        No credential mask is resolved here: deepseek's routing is UNVERIFIED, so
        the gate's mask would be empty anyway, and resolving one would read as a
        control the host does not honour. The client transport starts the same
        argv with the same absence (its ``elif self._is_deepseek:`` branch), so a
        dsh process started through either transport sees the same environment
        and the same confinement tier.

        The model still goes over the pipe, never argv (dsh is a member of
        ``ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION``). A direct runtime consumer
        (Sage's review pool) has no provider behind it to apply one, so without
        ``session_model`` every review would silently run the seat's boot model.
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.session_handle import AcpRuntimeError

        deepseek_bin, search_path = await asyncio.to_thread(client_mod._resolve_deepseek_bin)
        if not isinstance(deepseek_bin, str) or not deepseek_bin:
            # The wording mirrors the client's own not-found message, so an
            # operator reads one instruction from either transport.
            raise AcpRuntimeError(
                f"{client_mod.DEEPSEEK_BIN} not found "
                f"({client_mod.describe_search_path(search_path)}). Install it with "
                f"'{client_mod.DEEPSEEK_INSTALL_COMMAND}', or set "
                f"{client_mod._ENV_DEEPSEEK_BIN} to the executable. The ACP plugin "
                f"package alone does not serve ACP: it is a plugin, and this binary "
                f"is the host that boots the profile it lives in."
            )
        return SpawnPlan(
            argv=[deepseek_bin, *client_mod.DEEPSEEK_ACP_PROFILE_ARGS],
            session_model=ctx.model or None,
        )

    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Take kiro-cli's API key OUT, and pin the harness's own sandbox mode.

        A foreign host must never receive kiro-cli's key, and removing it is the
        positive action here rather than an omission -- the same thing
        ``AcpClient._resolve_spawn_env`` already does for this backend, so a dsh
        process started through either transport sees the same environment.

        ``DSH_PERMISSION_MODE`` is pinned rather than left to the ambient value:
        a variable inherited from the operator's shell must not be able to select
        the unconfined mode. Defence in depth and nothing more, because it does
        not route a tool call to Crew's gate.
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.config import loader as loader_mod

        loader_mod.strip_kiro_cli_api_key(env)
        env[client_mod._ENV_DEEPSEEK_PERMISSION_MODE] = client_mod.DEEPSEEK_PERMISSION_MODE

    @property
    def verifies_agent_activation(self) -> bool:
        """No -- there is no spawn flag whose effect could go unconfirmed.

        dsh takes no ``--agent``, so nothing was selected at spawn for a later
        check to confirm.
        """
        return False

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        return PROTOCOL_VERSION_DEEPSEEK

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
    ) -> SessionExtras:
        """Empty. dsh has no custom-agent channel to register anything on."""
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Pass the caller's array through unchanged.

        dsh has no agent-spec mirror to subtract from (``providers/mirrors``
        declares no projection for it), and what reaches the session is the
        shared broker append the runtime composes for every mirror-less backend.
        The direct runtime consumers this harness serves (Sage's review pool)
        request no per-session servers at all, so this seam answers with exactly
        what the caller asked.
        """
        return requested

    @property
    def wants_session_file_on_load(self) -> bool:
        """No -- the host locates the session from its id, like the codex adapter.

        It keeps its own session records, so there is no Crew-side transcript to
        name, and sending a path it cannot read would advertise a file that is
        not there.
        """
        return False

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None. dsh asks Crew for nothing at the connection level.

        Empty rather than absent, so a reader can tell "this host needs no
        callback" from "nobody has checked". KAS is the contrast: it raises
        ``_kiro/auth/getAccessToken`` when Crew owns the credential.
        """
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        """Never called: :attr:`host_answered_methods` is empty.

        Raises rather than returning ``{}`` -- an empty result would let a frame
        this harness never claimed be answered as if it had.
        """
        raise NotImplementedError(f"deepseek harness answers no inbound request, including {method!r}")

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        """Plain ACP, with no forked spellings.

        ``session/update`` only: dsh sends no ``_kiro.dev`` alias, announces no
        subagent roster, and stages no MCP-init frame. Declared explicitly rather
        than inherited from the kiro family, whose ``_kiro.dev/*`` vocabulary
        exists because KAS is reached THROUGH kiro-cli's relay -- dsh is not.
        """
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """An ordinary ``session/cancel``; the caller then drops the session.

        There is no delete verb to send. kiro-cli's ``_kiro.dev/session/terminate``
        and KAS's ``_kiro/session/delete`` are both kiro-family extensions, and
        sending either would draw a method-not-found while the session stayed on
        the process.

        **A NOTIFICATION, and that is not cosmetic.** ``session/cancel`` carries no
        id in ACP and the host sends nothing back, so a caller that waits for a
        reply waits out its whole teardown budget on every eviction and then logs
        the wait as a control-plane timeout. The same answer the codex harness
        gives, for the same reason.
        """
        return TeardownPolicy(method=METHOD_CANCEL, notification=True)
