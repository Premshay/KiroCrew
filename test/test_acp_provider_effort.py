"""ACP provider effort controls."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from test_acp_provider import _build_provider

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_DEEPSEEK
from kiro_crew.providers.acp import AcpProvider


class TestEffortControl:
    """Provider-level effort orchestration: backend branch selection,
    capability gating, live-apply, and clear semantics."""

    def _effort_provider(self, backend: str, model: str) -> AcpProvider:
        provider = _build_provider(backend=backend)
        provider._client._model = model
        provider._client._work_dir = MagicMock()
        provider._client.send_command = AsyncMock()
        provider._client.set_config_option = AsyncMock()
        # Default: the session advertises an 'effort' option (modern adapter).
        provider._client.supports_config_option = MagicMock(return_value=True)
        provider._client.effort_config_option_id = MagicMock(return_value="effort")
        provider._client.get_valid_effort_levels = MagicMock(return_value=[])
        return provider

    @pytest.mark.asyncio
    async def test_kiro_change_effort_pushes_slash_command_and_overlay(self):
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        with patch("kiro_crew.providers.acp._write_cli_overlay") as wco:
            ok = await provider.change_effort("xhigh")
        assert ok is True
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "xhigh"})
        # kiro uses the overlay, never set_config_option
        provider._client.set_config_option.assert_not_awaited()
        wco.assert_called_once()
        assert provider._effort_per_model["claude-opus-4.7"] == "xhigh"

    @pytest.mark.asyncio
    async def test_claude_change_effort_uses_set_config_option(self):
        provider = self._effort_provider(
            backend=ACP_BACKEND_CLAUDE, model="global.anthropic.claude-opus-4-8[1m]"
        )
        ok = await provider.change_effort("high")
        assert ok is True
        provider._client.set_config_option.assert_awaited_once_with("effort", "high")
        # claude does NOT use the kiro /effort slash command
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_codex_change_effort_uses_reasoning_effort_config_option(self):
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="gpt-5.6-sol")
        provider._client.effort_config_option_id.return_value = "reasoning_effort"

        assert await provider.change_effort("high") is True

        provider._client.set_config_option.assert_awaited_once_with("reasoning_effort", "high")

    @pytest.mark.asyncio
    async def test_claude_change_effort_steps_down_when_max_unsupported(self):
        # Adapter rejects "max" for a model whose ceiling is "xhigh"; the push
        # must fall back down the ladder and land "xhigh" rather than failing
        # the whole change (which would reset the session and lose state).
        from kiro_crew.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")

        async def _reject_max(config_id, value):
            if value == "max":
                raise AcpError("Invalid value for config option effort: max")

        provider._client.set_config_option = AsyncMock(side_effect=_reject_max)
        ok = await provider.change_effort("max")
        assert ok is True
        calls = [c.args for c in provider._client.set_config_option.await_args_list]
        assert ("effort", "max") in calls  # tried the requested level first
        assert ("effort", "xhigh") in calls  # then stepped down and succeeded
        # The slot override keeps the requested level so a future
        # max-capable model would get it.
        assert provider._effort_per_model["claude-opus-4.7"] == "max"

    @pytest.mark.asyncio
    async def test_claude_change_effort_propagates_non_value_errors(self):
        # A transport/timeout error is NOT a value rejection — it must NOT be
        # swallowed by the ladder; it propagates so the caller rolls back.
        from kiro_crew.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.set_config_option = AsyncMock(side_effect=AcpError("transport died"))
        with pytest.raises(AcpError, match="transport died"):
            await provider.change_effort("high")

    @pytest.mark.asyncio
    async def test_change_effort_noop_on_incapable_model(self):
        # 'auto' is genuinely effort-incapable (no concrete model selected).
        provider = self._effort_provider(backend="", model="auto")
        ok = await provider.change_effort("high")
        assert ok is False
        provider._client.send_command.assert_not_awaited()
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_effort_rejects_invalid_level(self):
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        with pytest.raises(ValueError):
            await provider.change_effort("ultra")

    @pytest.mark.asyncio
    async def test_claude_apply_initial_effort_pushes_resolved_level(self):
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        await provider._apply_initial_effort()
        assert provider._client.set_config_option.await_args_list == [call("effort", "max")]
        assert provider._client.send_command.await_count == 0

    @pytest.mark.asyncio
    async def test_claude_start_pushes_effort_keyed_by_configured_model_spelling(self):
        # The factory keys the level by the configured spelling; startup records
        # the advertised one on the client.
        with patch("kiro_crew.providers.acp.AcpClient"):
            provider = AcpProvider(
                acp_backend=ACP_BACKEND_CLAUDE,
                model="claude-opus-5.5",
                effort_per_model={"claude-opus-5.5": "max"},
            )
        client = provider._client
        client.backend = ACP_BACKEND_CLAUDE
        client._model = "claude-opus-5.5"
        client.supports_config_option = MagicMock(return_value=True)
        client.effort_config_option_id = MagicMock(return_value="effort")
        client.set_config_option = AsyncMock()

        async def _ensure_ready():
            client._model = "claude-opus-5-5[1m]"

        client.ensure_ready = AsyncMock(side_effect=_ensure_ready)
        await provider.start()
        assert client._model == "claude-opus-5-5[1m]"
        assert client.set_config_option.await_args_list == [call("effort", "max")]

    @pytest.mark.asyncio
    async def test_apply_initial_effort_noop_on_kiro_backend(self):
        # kiro gets effort from the spawn-time overlay, not a live push.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        await provider._apply_initial_effort()
        # A level does resolve, so the missing push is the backend gate's doing.
        assert provider._resolve_effort() == "max"
        provider._client.set_config_option.assert_not_awaited()
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_apply_initial_effort_swallows_adapter_error(self, caplog):
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        provider._client.set_config_option = AsyncMock(side_effect=RuntimeError("bad"))
        # Must not raise — a rejected effort cannot break session start.
        with caplog.at_level("WARNING", logger="kiro_crew.providers.acp"):
            await provider._apply_initial_effort()
        assert provider._client.set_config_option.await_args_list == [call("effort", "max")]
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "kiro_crew.providers.acp" and r.levelname == "WARNING"
        ]
        assert any("initial effort apply failed" in m for m in warnings), warnings

    @pytest.mark.asyncio
    async def test_claude_initial_effort_skips_when_option_unsupported(self):
        # Older claude-agent-acp builds advertise no 'effort' config option;
        # the initial-effort push must be a silent no-op (no set_config_option,
        # no error) rather than spamming 'Unknown config option' every spawn.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        provider._client.effort_config_option_id.return_value = None
        await provider._apply_initial_effort()
        provider._client.effort_config_option_id.assert_called_with()
        provider._client.set_config_option.assert_not_awaited()
        assert provider._resolve_effort() == "max"

    @pytest.mark.asyncio
    async def test_claude_change_effort_returns_false_when_option_unsupported(self):
        # change_effort must report unsupported (False) instead of attempting a
        # push that fails with 'Unknown config option' and resets the session.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.effort_config_option_id.return_value = None
        ok = await provider.change_effort("high")
        assert ok is False
        provider._client.set_config_option.assert_not_awaited()
        # No poisoned override left behind.
        assert "claude-opus-4.7" not in provider._effort_per_model

    @pytest.mark.asyncio
    async def test_claude_set_effort_swallows_unknown_config_option(self):
        # Defense in depth: even if the capability guard is bypassed (e.g. the
        # option is advertised lazily), an 'Unknown config option' rejection
        # from the adapter must be skipped, not re-raised (which resets).
        from kiro_crew.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        # Force the guard open so the ladder runs and hits the adapter error.
        provider._client.supports_config_option = MagicMock(return_value=True)
        provider._client.set_config_option = AsyncMock(
            side_effect=AcpError(
                "JSON-RPC error: {'code': -32603, 'message': 'Internal error', "
                "'data': {'details': 'Unknown config option: effort'}}"
            )
        )
        # Must not raise, and must not walk down the ladder: an unknown option
        # is not a value rejection.
        assert await provider._set_effort_config_option("max") is None
        assert provider._client.set_config_option.await_args_list == [call("effort", "max")]

    @pytest.mark.asyncio
    async def test_kiro_clear_effort_no_default_returns_false_for_reset(self):
        # No workspace default resolves → overlay cleared, nothing pushed live,
        # and clear_effort returns FALSE so the handler resets the session
        # (kiro respawns at the model's built-in default). Returning True here
        # would leave the running session stuck at the old effort.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "high"}
        with patch("kiro_crew.providers.acp._clear_cli_overlay_effort") as cco:
            ok = await provider.clear_effort()
        assert ok is False
        assert "claude-opus-4.7" not in provider._effort_per_model
        cco.assert_called_once()
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kiro_clear_effort_keeps_the_entry_when_the_overlay_lock_is_busy(self):
        # Nothing changed: not the file, and not the map once the entry goes
        # back. Neither bool can say that -- both make the handler commit the
        # cleared slot value, so the UI would read "default" over an overlay
        # that still holds the level. None is the third outcome, which the
        # handler answers with a retryable 409 and no commit.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "high"}
        with patch("kiro_crew.providers.acp._clear_cli_overlay_effort", return_value=False):
            ok = await provider.clear_effort()
        assert ok is None
        assert provider._effort_per_model["claude-opus-4.7"] == "high"
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kiro_clear_to_default_does_not_push_when_the_overlay_write_fails(self):
        # Clearing to a workspace default writes the overlay and pushes live. The
        # FILE is what a respawn reads, so a live push over a write that did not
        # persist reports a default the next spawn replaces with the level just
        # cleared. Nothing is pushed, the override goes back, and the third
        # outcome tells the handler to commit nothing.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "high"}
        provider._effort_defaults = {"claude-opus-4.7": "low"}
        with patch.object(type(provider), "_apply_effort_overlay", return_value=False):
            ok = await provider.clear_effort()
        assert ok is None
        assert provider._effort_per_model["claude-opus-4.7"] == "high"
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_kiro_effort_rollback_follows_the_file_when_the_rewrite_fails(self):
        # The live push failed, so change_effort rolls the prior level back and
        # rewrites the overlay to match. When THAT rewrite also fails the file
        # keeps the attempted level and construction re-seeds from it, so the map
        # follows the file instead of reporting an undo the next spawn contradicts.
        from kiro_crew.acp.client import AcpError

        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "low"}
        provider._client.send_command = AsyncMock(side_effect=AcpError("push failed"))
        calls = []

        def _apply(self_, **kwargs):
            calls.append(kwargs)
            # The pre-push write persists; the rollback rewrite loses the lock.
            return len(calls) == 1

        with patch.object(type(provider), "_apply_effort_overlay", _apply):
            with pytest.raises(AcpError):
                await provider.change_effort("xhigh")
        assert provider._effort_per_model["claude-opus-4.7"] == "xhigh"

    @pytest.mark.asyncio
    async def test_kiro_change_effort_does_not_push_when_the_overlay_write_fails(self):
        # The FILE is what a respawn reads, so pushing live over a write that did
        # not persist reports a change the next reset undoes. The override is
        # rolled back and the caller is told, rather than the session running a
        # level the workspace does not hold.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {}
        with patch.object(type(provider), "_apply_effort_overlay", return_value=False):
            with pytest.raises(RuntimeError, match="could not persist effort"):
                await provider.change_effort("high")
        assert "claude-opus-4.7" not in provider._effort_per_model
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_effort_rolls_back_on_push_failure(self):
        # A failed live push must not leave a poisoned override/overlay that
        # would re-push the rejected level on every respawn.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.set_config_option = AsyncMock(side_effect=RuntimeError("rejected"))
        with pytest.raises(RuntimeError):
            await provider.change_effort("xhigh")
        # Override rolled back to unset.
        assert "claude-opus-4.7" not in provider._effort_per_model

    @pytest.mark.asyncio
    async def test_kiro_change_effort_gpt_uses_reasoning_overlay_key(self):
        # GPT models are effort-capable on the kiro backend; the live push is the
        # same /effort slash command, but the spawn overlay must use the GPT
        # `reasoning` key (verified against kiro 2.13) — not `output_config`.
        provider = self._effort_provider(backend="", model="gpt-5.6-luna")
        with patch("kiro_crew.providers.acp._write_cli_overlay") as wco:
            ok = await provider.change_effort("max")
        assert ok is True
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "max"})
        wco.assert_called_once()
        assert provider._effort_per_model["gpt-5.6-luna"] == "max"

    def test_supports_effort_reflects_model(self):
        assert self._effort_provider(backend="", model="claude-opus-4.7").supports_effort()
        # GPT models are effort-capable on the kiro backend.
        assert self._effort_provider(backend="", model="gpt-5.6-luna").supports_effort()
        # 'auto' is genuinely effort-incapable (no concrete model selected). A raw
        # kiro 'claude-haiku-4.5' is also incapable — the haiku guard wins over
        # the registry's Sonnet fold (see test_effort.py). deepseek is a
        # third-party model kiro does not offer effort on.
        assert not self._effort_provider(backend="", model="auto").supports_effort()
        assert not self._effort_provider(backend="", model="claude-haiku-4.5").supports_effort()
        assert not self._effort_provider(backend="", model="deepseek-3.2").supports_effort()

    # ── Composite-id harness (DeepSeek Harness) ──────────────────────────
    # dsh advertises its models as JSON route pairs, so no id reaching these
    # methods is a name the kiro/claude allowlist can classify. The session's
    # own advertised selector is the only evidence there is, and these three
    # pin that it is what decides.

    _DSH_MODEL = '["deepseek-official","deepseek-flash"]'

    def test_deepseek_supports_effort_follows_the_advertised_option(self):
        provider = self._effort_provider(backend=ACP_BACKEND_DEEPSEEK, model=self._DSH_MODEL)
        provider._client.effort_config_option_id.return_value = "reasoning_effort"
        assert provider.supports_effort() is True
        # An older adapter build that advertises no selector must not claim it.
        provider._client.effort_config_option_id.return_value = None
        assert provider.supports_effort() is False

    @pytest.mark.asyncio
    async def test_deepseek_change_effort_uses_the_advertised_option(self):
        provider = self._effort_provider(backend=ACP_BACKEND_DEEPSEEK, model=self._DSH_MODEL)
        provider._client.effort_config_option_id.return_value = "reasoning_effort"
        assert await provider.change_effort("low") is True
        provider._client.set_config_option.assert_awaited_once_with("reasoning_effort", "low")
        provider._client.send_command.assert_not_awaited()
        assert provider._effort_per_model[self._DSH_MODEL] == "low"

    @pytest.mark.asyncio
    async def test_deepseek_initial_effort_applies_a_slot_override(self):
        provider = self._effort_provider(backend=ACP_BACKEND_DEEPSEEK, model=self._DSH_MODEL)
        provider._client.effort_config_option_id.return_value = "reasoning_effort"
        provider._client.get_valid_effort_levels.return_value = ["low", "high"]
        provider._effort_per_model = {self._DSH_MODEL: "low"}
        await provider._apply_initial_effort()
        provider._client.set_config_option.assert_awaited_once_with("reasoning_effort", "low")
