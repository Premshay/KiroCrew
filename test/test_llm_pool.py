"""Unit tests for the unified LLM pool.

This file pins the pool's own mechanics -- sends and batches, acquire and release under the
semaphore, dead-worker replacement, the provider, sandbox and shared config reads, start and
the context manager -- and the ``AcpWorker``'s send, start and shutdown, its backend
selection and its zero-tool confirmation. Reasoning effort is pinned in
``test_llm_pool_effort.py``, the ``CCWorker`` subprocess worker in
``test_llm_pool_ccworker.py``, and ``fetch_url_content``, the idle-TTL reader and reaper,
and conversation recycling in ``test_llm_pool_lifecycle.py``. The shared fakes and the
config-dir fixture live in ``llm_pool_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from llm_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    FakeWorker,
    _config_dir_tracks_patched_home,
    _make_pool_with_fake_workers,
    _zero_tool_client,
)

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)
from kiro_crew.knowledge.llm_pool import (
    AcpWorker,
    LLMPool,
    Worker,
    _clean_binding,
    _get_acp_backend,
    _get_provider_type,
    _get_sandbox_mode,
    _read_config,
    _resolve_client_binding,
)
from kiro_crew.testing.wait import await_until

# ---------------------------------------------------------------------------
# Fixtures — mock workers that don't spawn real processes
# ---------------------------------------------------------------------------


class DeadOnSecondCallWorker(Worker):
    """Dies after first send_message call."""

    def __init__(self) -> None:
        self._alive = True
        self._called = False

    async def start(self) -> None:
        self._alive = True

    async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
        if self._called:
            self._alive = False
            raise RuntimeError("process died")
        self._called = True
        return "first_response"

    async def shutdown(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    async def reset_conversation(self) -> None:
        self.calls_since_reset = 0


# ---------------------------------------------------------------------------
# Tests: Pool basics
# ---------------------------------------------------------------------------


class TestLLMPoolBasics:
    def test_init_defaults(self):
        pool = LLMPool()
        assert pool._pool_size == 3
        assert pool._started is False

    def test_init_custom_size(self):
        pool = LLMPool(pool_size=5)
        assert pool._pool_size == 5

    @pytest.mark.asyncio
    async def test_send_returns_response(self):
        pool = _make_pool_with_fake_workers(pool_size=2, responses=["hello"])
        result = await pool.send("prompt")
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_send_batch_returns_ordered(self):
        pool = _make_pool_with_fake_workers(pool_size=2, responses=["r"])
        results = await pool.send_batch(["a", "b", "c"])
        assert results == ["r", "r", "r"]

    @pytest.mark.asyncio
    async def test_send_batch_empty(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        results = await pool.send_batch([])
        assert results == []

    @pytest.mark.asyncio
    async def test_shutdown_clears_workers(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        await pool.shutdown()
        assert pool._workers == []
        assert pool._started is False


# ---------------------------------------------------------------------------
# Tests: Semaphore and concurrency
# ---------------------------------------------------------------------------


class TestLLMPoolConcurrency:
    @pytest.mark.asyncio
    async def test_acquire_release_cycle(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        idx, worker = await pool.acquire()
        assert isinstance(worker, FakeWorker)
        pool.release(idx)

    @pytest.mark.asyncio
    async def test_semaphore_blocks_when_all_busy(self):
        pool = _make_pool_with_fake_workers(pool_size=1, responses=["slow"])
        # Acquire the only worker
        idx, worker = await pool.acquire()

        # Second acquire should block
        acquired = asyncio.Event()

        async def _try_acquire():
            await pool.acquire()
            acquired.set()

        task = asyncio.create_task(_try_acquire())
        await asyncio.sleep(0.05)
        assert not acquired.is_set()

        # Release unblocks
        pool.release(idx)
        await asyncio.sleep(0.05)
        assert acquired.is_set()
        task.cancel()

    @pytest.mark.asyncio
    async def test_concurrent_sends_bounded_by_pool_size(self):
        """Pool size=2, 4 concurrent sends — max 2 in-flight at any time."""
        in_flight = 0
        max_in_flight = 0

        class CountingWorker(Worker):
            async def start(self) -> None:
                pass

            async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
                nonlocal in_flight, max_in_flight
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
                await asyncio.sleep(0.02)
                in_flight -= 1
                return "done"

            async def shutdown(self) -> None:
                pass

            def is_alive(self) -> bool:
                return True

            async def reset_conversation(self) -> None:
                self.calls_since_reset = 0

        pool = LLMPool(pool_size=2)
        pool._started = True
        pool._provider_type = "test"
        for i in range(2):
            pool._workers.append(CountingWorker())
            pool._available.put_nowait(i)

        await pool.send_batch(["a", "b", "c", "d"])
        assert max_in_flight <= 2


# ---------------------------------------------------------------------------
# Tests: Dead worker replacement
# ---------------------------------------------------------------------------


class TestLLMPoolWorkerReplacement:
    @pytest.mark.asyncio
    async def test_dead_worker_gets_replaced(self):
        pool = _make_pool_with_fake_workers(pool_size=1, responses=["alive"])
        # Kill the worker
        fake = pool._workers[0]
        assert isinstance(fake, FakeWorker)
        fake._alive = False

        replacement_created = False

        async def _mock_create_worker():
            nonlocal replacement_created
            replacement_created = True
            w = FakeWorker(responses=["replaced"])
            w._started = True
            return w

        pool._create_worker = _mock_create_worker  # type: ignore[assignment]
        idx, worker = await pool.acquire()
        assert replacement_created
        result = await worker.send_message("test")
        assert result == "replaced"
        pool.release(idx)

    @pytest.mark.asyncio
    async def test_send_with_dead_worker_still_succeeds(self):
        pool = _make_pool_with_fake_workers(pool_size=1, responses=["alive"])
        fake = pool._workers[0]
        assert isinstance(fake, FakeWorker)
        fake._alive = False

        async def _mock_create_worker():
            w = FakeWorker(responses=["recovered"])
            w._started = True
            return w

        pool._create_worker = _mock_create_worker  # type: ignore[assignment]
        result = await pool.send("test")
        assert result == "recovered"


class TestAcpWorkerFailureRetiresClient:
    """A failed turn must not leave its kiro-cli child in the pool."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", ["timeout", "acp"])
    async def test_failure_shuts_down_so_next_send_gets_fresh_worker(self, error):
        from kiro_crew.acp.client import AcpError, AcpTimeoutError

        exc = AcpTimeoutError() if error == "timeout" else AcpError("boom")
        stuck = MagicMock()
        stuck.is_ready = True
        stuck.is_process_alive = lambda: True
        stuck.send_message = AsyncMock(side_effect=exc)
        stuck.shutdown = AsyncMock()
        worker = AcpWorker()
        worker._client = stuck
        pool = LLMPool(pool_size=1)
        pool._started = True
        pool._provider_type = "test"
        pool._workers.append(worker)
        pool._available.put_nowait(0)

        async def _mock_create_worker():
            w = FakeWorker(responses=["fresh"])
            w._started = True
            return w

        pool._create_worker = _mock_create_worker  # type: ignore[assignment]

        with pytest.raises(type(exc)):
            await pool.send("first", timeout=1.0)
        stuck.shutdown.assert_awaited_once()
        assert worker.is_alive() is False

        assert await pool.send("second") == "fresh"
        assert stuck.send_message.await_count == 1


# ---------------------------------------------------------------------------
# Tests: send_batch error handling
# ---------------------------------------------------------------------------


class TestLLMPoolBatchErrors:
    @pytest.mark.asyncio
    async def test_batch_item_failure_returns_empty_string(self):
        class FailOnSecondWorker(Worker):
            def __init__(self) -> None:
                self._count = 0

            async def start(self) -> None:
                pass

            async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
                self._count += 1
                if self._count == 2:
                    raise RuntimeError("boom")
                return f"ok-{self._count}"

            async def shutdown(self) -> None:
                pass

            def is_alive(self) -> bool:
                return True

            async def reset_conversation(self) -> None:
                self.calls_since_reset = 0

        pool = LLMPool(pool_size=1)
        pool._started = True
        pool._provider_type = "test"
        pool._workers.append(FailOnSecondWorker())
        pool._available.put_nowait(0)

        results = await pool.send_batch(["a", "b", "c"])
        # Second item failed, gets ""
        assert results[1] == ""
        # Others succeed (order may vary due to serial with pool_size=1)
        assert "ok" in results[0] or results[0] == ""


# ---------------------------------------------------------------------------
# Tests: Provider detection
# ---------------------------------------------------------------------------


class TestProviderDetection:
    def test_default_is_acp(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_provider_type() == "acp"

    def test_reads_claude_code_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "claude_code"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_provider_type() == "claude_code"

    def test_reads_acp_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "acp"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_provider_type() == "acp"

    def test_handles_malformed_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_provider_type() == "acp"


# ---------------------------------------------------------------------------
# Tests: sandbox mode (knowledge workers honour agent.sandbox; default auto)
# ---------------------------------------------------------------------------


class TestSandboxMode:
    """Knowledge workers (kiro + claude) run under the same OS-level sandbox as
    chat, honouring ``agent.sandbox`` (default ``"auto"`` — engages OS-level
    isolation and defers to kiro-cli's internal sandbox on macOS when enabled).
    These lock in the shipped behaviour."""

    def test_default_is_auto(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "auto"

    def test_reads_sandbox_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"sandbox": "off"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "off"

    def test_unparseable_config_defaults_auto(self, tmp_path):
        # A file that isn't valid JSON parses to {} → sandbox UNSET → the
        # intended default "auto" (OS-level isolation engaged).
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "auto"

    def test_unknown_mode_falls_back_to_auto_fail_secure(self, tmp_path):
        """A PRESENT but unrecognised value is a config error → fail SECURE to
        'auto' (never silently unsandboxed). Distinct from an absent value, which
        takes the intended 'off' default."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"sandbox": "bogus"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "auto"

    @pytest.mark.parametrize("mode", ["auto", "standard", "strict", "cc", "off"])
    def test_all_valid_modes_pass_through(self, mode, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"agent": {"sandbox": mode}}))
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == mode

    def test_accepts_prereadm_config_dict(self):
        """Pure-parser path: a passed dict is used without touching disk.
        Present-but-invalid fails secure to 'auto'; absent takes 'auto'."""
        assert _get_sandbox_mode({"agent": {"sandbox": "strict"}}) == "strict"
        assert (
            _get_sandbox_mode({"agent": {"sandbox": "nope"}}) == "auto"
        )  # malformed → fail secure
        assert _get_sandbox_mode({}) == "auto"  # unset → intended default


# ---------------------------------------------------------------------------
# Tests: shared config read (single disk read threaded into pure parsers)
# ---------------------------------------------------------------------------


class TestReadConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _read_config() == {}

    def test_reads_dict(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "claude_code"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _read_config() == {"agent": {"provider": "claude_code"}}

    def test_malformed_returns_empty(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _read_config() == {}

    def test_non_dict_json_returns_empty(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("[1, 2, 3]")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _read_config() == {}

    def test_parsers_accept_config_dict(self):
        """provider parser reads the passed dict, no disk access."""
        data = {
            "agent": {"provider": "claude_code"},
            "knowledge": {},
        }
        assert _get_provider_type(data) == "claude_code"

    @pytest.mark.parametrize(
        "bad",
        [
            {"agent": "acp"},
            {"agent": None},
            {"agent": 42},
            {"knowledge": "claude_code"},
            {"knowledge": None},
            {"agent": ["acp"]},
        ],
    )
    def test_parsers_survive_non_dict_sections(self, bad):
        """A hand-edited config with a non-dict ``agent``/``knowledge`` section
        must not crash the pure parsers — they fall back to defaults, matching
        the no-op-on-malformed-config contract of ``_read_config``."""
        assert _get_provider_type(bad) == "acp"
        assert _get_sandbox_mode(bad) == "auto"
        assert _get_acp_backend(bad) == ACP_BACKEND_KIRO

    def test_read_config_coerces_non_dict_sections(self, tmp_path):
        """``_read_config`` normalises non-dict ``agent``/``knowledge`` to ``{}``
        so downstream ``.get(...).get(...)`` chains are always dict-safe."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": "acp", "knowledge": 7}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            data = _read_config()
        assert data["agent"] == {}
        assert data["knowledge"] == {}

    @pytest.mark.asyncio
    async def test_start_passes_configured_sandbox_to_client(self, tmp_path):
        """AcpWorker.start wires the configured sandbox mode into AcpClient."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"sandbox": "off"}}')
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["sandbox_mode"] == "off"

    @pytest.mark.asyncio
    async def test_start_defaults_sandbox_to_auto(self, tmp_path):
        # With no config, the sandbox mode defaults to "auto" — engaging
        # OS-level isolation (namespace on Linux, seatbelt on macOS) and
        # automatically deferring to kiro-cli's internal sandbox when enabled.
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["sandbox_mode"] == "auto"

    @pytest.mark.parametrize(
        ("persisted", "expected"),
        [
            (ACP_BACKEND_KIRO, ACP_BACKEND_KIRO),
            (None, ACP_BACKEND_KIRO),
            ("no-such-harness", ACP_BACKEND_KIRO),
            (42, ACP_BACKEND_KIRO),
            # claude's routing (SEEDED_SETTINGS) is declared but not enforced by
            # this core -- an inherited ~/.claude pre-approval can skip
            # session/request_permission entirely, so honors_zero_tool_ban answers
            # False and it degrades to kiro.
            (ACP_BACKEND_CLAUDE, ACP_BACKEND_KIRO),
            # Selectable through the gate but outside this pool's zero-tool
            # allowlist -- codex is served by AcpRuntime, which carries no
            # zero-tool refusal -- so it degrades to kiro the same as an
            # unselectable value.
            (ACP_BACKEND_CODEX, ACP_BACKEND_KIRO),
            # Answers both capabilities _get_acp_backend checks:
            # honors_zero_tool_ban (AcpClient._deny_zero_tools refuses every
            # permission request on this backend's mirror) and
            # acp_client_spawnable -- passes through.
            (ACP_BACKEND_OPENCODE, ACP_BACKEND_OPENCODE),
            # pi has no mirror; its zero_tools is read beside its member mounts
            # from the same spec parse, and every pi tool call reaches
            # session/request_permission through the gate extension, so
            # _deny_zero_tools refuses it -- passes through.
            (ACP_BACKEND_PI, ACP_BACKEND_PI),
        ],
    )
    def test_acp_backend_goes_through_the_selection_gate(self, persisted, expected):
        agent = {} if persisted is None else {"acp_backend": persisted}
        assert _get_acp_backend({"agent": agent}) == expected

    @pytest.mark.asyncio
    async def test_start_passes_the_resolved_backend_to_client(self, tmp_path):
        """The resolved backend must reach ``AcpClient`` explicitly, not ride
        the client's own default -- the pool's zero-tool restriction has to be
        decided HERE, once, rather than re-derived at the spawn site."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"agent": {"acp_backend": ACP_BACKEND_KIRO}}))
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["acp_backend"] == ACP_BACKEND_KIRO

    @pytest.mark.asyncio
    async def test_start_passes_a_zero_tool_enforced_opencode_backend(self, tmp_path):
        """A configured ``opencode`` backend reaches ``AcpClient`` for this pool:
        ``AcpClient._deny_zero_tools`` refuses every permission request (MCP or
        native) on a mirrored session whose spec declares ``tools: []``, so the
        pool's zero-tool guarantee holds on opencode too -- both capabilities
        ``_get_acp_backend`` checks (``honors_zero_tool_ban``,
        ``acp_client_spawnable``) answer True for it."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"agent": {"acp_backend": ACP_BACKEND_OPENCODE}}))
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["acp_backend"] == ACP_BACKEND_OPENCODE

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "backend", [ACP_BACKEND_PI, ACP_BACKEND_OPENCODE, ACP_BACKEND_GOOSE, ACP_BACKEND_DEEPSEEK]
    )
    async def test_an_adapter_worker_carries_the_extraction_model_pin(self, tmp_path, backend):
        """None of these harnesses reads a spec of Crew's, so the knowledge model pin
        reaches the client explicitly, the model kiro-cli reads from the spec;
        without it extraction silently ran on the harness default."""
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("kiro_crew.knowledge.llm_pool._get_acp_backend", return_value=backend),
            patch(
                "kiro_crew.knowledge.llm_pool._knowledge_spec_model",
                return_value="local/extract-small",
            ),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["acp_backend"] == backend
        assert mk.call_args.kwargs["model"] == "local/extract-small"

    @pytest.mark.asyncio
    async def test_a_kiro_worker_reads_the_pin_from_its_spec(self, tmp_path):
        """kiro-cli reads the spec's own model, so its construction is unchanged."""
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("kiro_crew.knowledge.llm_pool._get_acp_backend", return_value=ACP_BACKEND_KIRO),
            patch(
                "kiro_crew.knowledge.llm_pool._knowledge_spec_model",
                side_effect=AssertionError("kiro must not read the pin here"),
            ),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert "model" not in mk.call_args.kwargs

    @pytest.mark.parametrize(
        ("pin", "expected"),
        [
            ("local/extract-small", "local/extract-small"),
            (" local/x ", "local/x"),
            ("auto", None),
            ("", None),
        ],
    )
    def test_the_pin_is_the_installers_rule(self, pin, expected):
        from kiro_crew.knowledge.llm_pool import _knowledge_spec_model

        with patch("kiro_crew.agent.knowledge_agent_model", return_value=pin):
            assert _knowledge_spec_model() == expected

    def test_an_unreadable_pin_is_no_pin(self):
        from kiro_crew.knowledge.llm_pool import _knowledge_spec_model

        with patch("kiro_crew.agent.knowledge_agent_model", side_effect=OSError("boom")):
            assert _knowledge_spec_model() is None

    @pytest.mark.asyncio
    async def test_start_degrades_a_non_enforced_backend_to_kiro(self, tmp_path):
        """A configured ``codex`` backend must not reach ``AcpClient`` for this
        pool: codex needs ``AcpRuntime``, which ``AcpClient`` doesn't drive, so
        it would fall through to a bare kiro-cli spawn under the wrong identity
        -- ``acp_client_spawnable`` answers False for it."""
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"agent": {"acp_backend": ACP_BACKEND_CODEX}}))
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["acp_backend"] == ACP_BACKEND_KIRO

    def test_unsafe_backend_fallback_warns_once_naming_the_capability(self, caplog):
        """A configured backend that fails a capability check logs a warning
        naming the configured backend and which capability failed, and does so
        ONCE per backend per process -- this pool's workers are recreated
        often, so a warning on every ``start()`` would spam the log."""
        from kiro_crew.knowledge import llm_pool as llm_pool_mod

        llm_pool_mod._WARNED_UNSAFE_BACKENDS.clear()
        with caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"):
            # codex fails honors_zero_tool_ban (AcpRuntime carries no zero-tool
            # refusal).
            assert _get_acp_backend({"agent": {"acp_backend": ACP_BACKEND_CODEX}}) == (
                ACP_BACKEND_KIRO
            )
            assert _get_acp_backend({"agent": {"acp_backend": ACP_BACKEND_CODEX}}) == (
                ACP_BACKEND_KIRO
            )
            # kas passes honors_zero_tool_ban (native) but fails
            # acp_client_spawnable (needs AcpRuntime, no arm in AcpClient._spawn).
            assert _get_acp_backend({"agent": {"acp_backend": ACP_BACKEND_KAS}}) == (
                ACP_BACKEND_KIRO
            )
        codex_records = [r for r in caplog.records if ACP_BACKEND_CODEX in r.getMessage()]
        kas_records = [r for r in caplog.records if ACP_BACKEND_KAS in r.getMessage()]
        assert len(codex_records) == 1, "codex fallback warned more than once"
        assert "honors_zero_tool_ban" in codex_records[0].getMessage()
        assert len(kas_records) == 1, "kas fallback warned more than once"
        assert "acp_client_spawnable" in kas_records[0].getMessage()

    @pytest.mark.parametrize("agent", [{}, {"acp_backend": ""}])
    def test_undeclared_backend_falls_back_to_kiro_without_warning(self, caplog, agent):
        """No declared ``agent.acp_backend`` resolves to the KAS default, which
        is not ``acp_client_spawnable``. The pool still uses Kiro, but the user
        chose nothing, so no WARNING names a backend they never configured; a
        later declared ``kas`` still warns once."""
        from kiro_crew.knowledge import llm_pool as llm_pool_mod

        llm_pool_mod._WARNED_UNSAFE_BACKENDS.clear()
        with caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"):
            assert _get_acp_backend({"agent": agent}) == ACP_BACKEND_KIRO
            assert _get_acp_backend({}) == ACP_BACKEND_KIRO
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []

        with caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"):
            assert _get_acp_backend({"agent": {"acp_backend": ACP_BACKEND_KAS}}) == (
                ACP_BACKEND_KIRO
            )
            assert _get_acp_backend({"agent": {"acp_backend": ACP_BACKEND_KAS}}) == (
                ACP_BACKEND_KIRO
            )
        kas_warnings = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and ACP_BACKEND_KAS in r.getMessage()
        ]
        assert len(kas_warnings) == 1

    @pytest.mark.asyncio
    async def test_start_defaults_backend_to_kiro(self, tmp_path):
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["acp_backend"] == ACP_BACKEND_KIRO

    @pytest.mark.asyncio
    async def test_pool_worker_resolves_backend_through_get_acp_backend(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"agent": {"acp_backend": ACP_BACKEND_KIRO}}))
        mock_client = _zero_tool_client()
        mock_client.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk,
        ):
            pool = LLMPool(pool_size=1)
            await pool.start()
            await pool.shutdown()
        assert mk.call_args.kwargs["acp_backend"] == ACP_BACKEND_KIRO


# ---------------------------------------------------------------------------
# Tests: Pool start (mocked workers)
# ---------------------------------------------------------------------------


class TestLLMPoolStart:
    @pytest.mark.asyncio
    async def test_start_creates_workers(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "acp"}}')

        with patch("pathlib.Path.home", return_value=tmp_path):
            pool = LLMPool(pool_size=2)

            # Mock _create_worker to avoid spawning real processes
            workers_created = []

            async def _mock_create():
                w = FakeWorker(responses=["ok"])
                w._started = True
                workers_created.append(w)
                return w

            pool._create_worker = _mock_create  # type: ignore[assignment]
            await pool.start()

        assert pool._started is True
        assert len(pool._workers) == 2
        assert len(workers_created) == 2

    @pytest.mark.asyncio
    async def test_start_idempotent(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "acp"}}')

        with patch("pathlib.Path.home", return_value=tmp_path):
            pool = LLMPool(pool_size=1)
            call_count = 0

            async def _mock_create():
                nonlocal call_count
                call_count += 1
                w = FakeWorker()
                w._started = True
                return w

            pool._create_worker = _mock_create  # type: ignore[assignment]
            await pool.start()
            await pool.start()  # second call should no-op

        assert call_count == 1


# ---------------------------------------------------------------------------
# Tests: Context manager
# ---------------------------------------------------------------------------


class TestLLMPoolContextManager:
    @pytest.mark.asyncio
    async def test_async_context_manager(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"provider": "acp"}}')

        with patch("pathlib.Path.home", return_value=tmp_path):
            pool = LLMPool(pool_size=1)

            async def _mock_create():
                w = FakeWorker(responses=["ctx"])
                w._started = True
                return w

            pool._create_worker = _mock_create  # type: ignore[assignment]

            async with pool as p:
                assert p._started is True
                result = await p.send("test")
                assert result == "ctx"

            assert p._started is False


# ---------------------------------------------------------------------------
# Tests: AcpWorker (mocked AcpClient)
# ---------------------------------------------------------------------------


class TestAcpWorker:
    @pytest.mark.asyncio
    async def test_send_message(self):
        mock_client = AsyncMock()
        mock_client.is_ready = True
        mock_client.send_message = AsyncMock(return_value="response")
        mock_client.is_process_alive = lambda: True

        worker = AcpWorker()
        worker._client = mock_client

        result = await worker.send_message("hello", timeout=30.0)
        assert result == "response"
        mock_client.send_message.assert_called_once_with("hello", timeout=30.0)

    @pytest.mark.asyncio
    async def test_is_alive_true(self):
        mock_client = AsyncMock()
        mock_client.is_process_alive = lambda: True

        worker = AcpWorker()
        worker._client = mock_client
        assert worker.is_alive() is True

    @pytest.mark.asyncio
    async def test_is_alive_false_no_client(self):
        worker = AcpWorker()
        assert worker.is_alive() is False

    @pytest.mark.asyncio
    async def test_shutdown(self):
        mock_client = AsyncMock()
        worker = AcpWorker()
        worker._client = mock_client

        await worker.shutdown()
        mock_client.shutdown.assert_called_once()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_start_shuts_down_stale_client_before_respawn(self, tmp_path):
        """A re-``start`` (e.g. ``send_message`` after a stalled handshake) must
        shut the previous client down before creating a new one, so the prior
        subprocess is not orphaned."""
        stale = AsyncMock()
        fresh = _zero_tool_client()
        fresh.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh),
        ):
            worker = AcpWorker()
            worker._client = stale
            await worker.start()
        stale.shutdown.assert_called_once()
        assert worker._client is fresh

    @pytest.mark.asyncio
    async def test_start_swallows_stale_shutdown_error(self, tmp_path):
        """A failure shutting the stale client down must not abort the respawn."""
        stale = AsyncMock()
        stale.shutdown.side_effect = RuntimeError("boom")
        fresh = _zero_tool_client()
        fresh.is_ready = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh),
        ):
            worker = AcpWorker()
            worker._client = stale
            await worker.start()
        assert worker._client is fresh

    @pytest.mark.asyncio
    async def test_start_registers_pid_shutdown_unregisters(self, tmp_path):
        """AcpWorker must shield its live kiro-cli PID from the gateway orphan
        sweep (register on start, unregister on shutdown) — otherwise a busy
        knowledge worker is SIGKILLed mid-task as a false orphan ("ACP process
        exited (code=1)")."""
        fresh = _zero_tool_client()
        fresh.is_ready = True
        fresh._pid = 7777
        registered: list[int] = []
        unregistered: list[int] = []
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh),
            patch(
                "kiro_crew.knowledge.llm_pool.register_protected_pid",
                side_effect=registered.append,
            ),
            patch(
                "kiro_crew.knowledge.llm_pool.unregister_protected_pid",
                side_effect=unregistered.append,
            ),
        ):
            worker = AcpWorker()
            await worker.start()
            assert registered == [7777], "worker did not shield its PID on start"
            await worker.shutdown()
            assert unregistered == [7777], "worker did not release its PID on shutdown"

    @pytest.mark.asyncio
    async def test_respawn_reshields_new_pid(self, tmp_path):
        """A re-``start`` (respawn under a new PID) must release the old PID's
        shield and register the new one, so a dead PID is never left shielded."""
        first = _zero_tool_client()
        first.is_ready = True
        first._pid = 100
        second = _zero_tool_client()
        second.is_ready = True
        second._pid = 200
        registered: list[int] = []
        unregistered: list[int] = []
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]),
            patch(
                "kiro_crew.knowledge.llm_pool.register_protected_pid",
                side_effect=registered.append,
            ),
            patch(
                "kiro_crew.knowledge.llm_pool.unregister_protected_pid",
                side_effect=unregistered.append,
            ),
        ):
            worker = AcpWorker()
            await worker.start()  # register 100
            await worker.start()  # stale-drop: unregister 100, then register 200
        assert registered == [100, 200]
        assert unregistered == [100]


class TestAcpWorkerZeroToolConfirmation:
    """``start()`` requires the CONSUMED projection to confirm the zero-tool
    spec on any backend not natively routed through ``Routing.AGENT_SPEC``,
    falling back to Kiro before any prompt is sent when it doesn't. A worker
    that ends on ``Routing.AGENT_SPEC`` (kiro) must also find the spec it
    resolves declaring ``"tools": []``, or it refuses to start."""

    @pytest.mark.asyncio
    async def test_unconfirmed_opencode_falls_back_to_kiro_and_warns(self, tmp_path, caplog):
        from kiro_crew.knowledge import llm_pool as llm_pool_mod

        llm_pool_mod._WARNED_UNSAFE_BACKENDS.clear()
        first = _zero_tool_client()
        first.is_ready = True
        first.backend = ACP_BACKEND_OPENCODE
        first.spec_zero_tools = False
        second = _zero_tool_client()
        second.is_ready = True
        second.backend = ACP_BACKEND_KIRO
        second.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]) as mk,
            caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_OPENCODE)
            await worker.start()
        assert mk.call_count == 2
        assert mk.call_args_list[0].kwargs["acp_backend"] == ACP_BACKEND_OPENCODE
        assert mk.call_args_list[1].kwargs["acp_backend"] == ACP_BACKEND_KIRO
        first.shutdown.assert_awaited_once()
        assert worker._client is second
        assert worker._acp_backend == ACP_BACKEND_KIRO
        assert any("did not confirm the zero-tool spec" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unconfirmed_opencode_fallback_survives_a_failing_shutdown(self, tmp_path):
        first = _zero_tool_client()
        first.is_ready = True
        first.backend = ACP_BACKEND_OPENCODE
        first.spec_zero_tools = False
        first.shutdown.side_effect = RuntimeError("shutdown failed")
        second = _zero_tool_client()
        second.is_ready = True
        second.backend = ACP_BACKEND_KIRO
        second.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_OPENCODE)
            await worker.start()
        first.shutdown.assert_awaited_once()
        assert worker._client is second
        assert worker._acp_backend == ACP_BACKEND_KIRO

    @pytest.mark.asyncio
    async def test_confirmed_opencode_stays_on_opencode_no_warning(self, tmp_path, caplog):
        from kiro_crew.knowledge import llm_pool as llm_pool_mod

        llm_pool_mod._WARNED_UNSAFE_BACKENDS.clear()
        client = _zero_tool_client()
        client.is_ready = True
        client.backend = ACP_BACKEND_OPENCODE
        client.spec_zero_tools = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client) as mk,
            caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_OPENCODE)
            await worker.start()
        assert mk.call_count == 1
        assert worker._client is client
        assert worker._acp_backend == ACP_BACKEND_OPENCODE
        assert not any(
            "did not confirm the zero-tool spec" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_kiro_backend_never_falls_back_even_when_unconfirmed(self, tmp_path, caplog):
        """kiro-cli's routing is ``Routing.AGENT_SPEC`` -- the harness enforces
        the ban natively, so an unset ``spec_zero_tools`` (no mirror at all on
        this backend) must not trigger a restart."""
        client = _zero_tool_client()
        client.is_ready = True
        client.backend = ACP_BACKEND_KIRO
        client.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client) as mk,
            caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO)
            await worker.start()
        assert mk.call_count == 1
        assert worker._client is client
        assert not any(
            "did not confirm the zero-tool spec" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_kiro_confirms_the_resolved_spec_before_the_worker_is_ready(self, tmp_path):
        client = _zero_tool_client()
        client.is_ready = True
        client.backend = ACP_BACKEND_KIRO
        client.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO)
            await worker.start()
        client.authored_spec_declares_zero_tools.assert_called_once_with()
        client.effective_spec_declares_zero_tools.assert_called_once_with(
            authored_before_spawn=True
        )
        assert worker._client is client

    @pytest.mark.asyncio
    async def test_confirmed_opencode_does_not_reread_the_spec(self, tmp_path):
        client = _zero_tool_client(spec_declares_zero_tools=False)
        client.is_ready = True
        client.backend = ACP_BACKEND_OPENCODE
        client.spec_zero_tools = True
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_OPENCODE)
            await worker.start()
        client.effective_spec_declares_zero_tools.assert_not_called()
        client.authored_spec_declares_zero_tools.assert_not_called()
        assert worker._client is client

    @pytest.mark.asyncio
    async def test_kiro_with_a_shadowed_spec_refuses_to_start_and_sends_nothing(
        self, tmp_path, caplog
    ):
        """A project-level spec of the same name that carries tools shadows the
        installed zero-tool one on kiro, which resolves ``--agent`` against the
        workspace first. Nothing native confirms the ban there, so the worker
        must not start, and no prompt may reach the shadowing spec."""
        client = _zero_tool_client(spec_declares_zero_tools=False)
        client.is_ready = True
        client.backend = ACP_BACKEND_KIRO
        client.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client) as mk,
            caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO)
            with pytest.raises(RuntimeError, match="zero-tool spec"):
                await worker.send_message("untrusted document text")
        assert mk.call_count == 1
        client.shutdown.assert_awaited_once()
        client.send_message.assert_not_awaited()
        assert worker._client is None
        assert any(
            "does not declare" in r.getMessage() and "kirocrew-knowledge" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_unconfirmed_spec_refusal_survives_a_failing_shutdown(self, tmp_path):
        client = _zero_tool_client(spec_declares_zero_tools=False)
        client.is_ready = True
        client.backend = ACP_BACKEND_KIRO
        client.spec_zero_tools = False
        client.shutdown.side_effect = RuntimeError("boom")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO)
            with pytest.raises(RuntimeError, match="zero-tool spec"):
                await worker.start()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_kiro_fallback_with_a_shadowed_spec_also_refuses_to_start(self, tmp_path):
        """The restart on Kiro is not a safe harbour by itself: the fallback client
        resolves the same shadowed spec, so it is confirmed too."""
        first = _zero_tool_client()
        first.is_ready = True
        first.backend = ACP_BACKEND_OPENCODE
        first.spec_zero_tools = False
        second = _zero_tool_client(spec_declares_zero_tools=False)
        second.is_ready = True
        second.backend = ACP_BACKEND_KIRO
        second.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]) as mk,
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_OPENCODE)
            with pytest.raises(RuntimeError, match="zero-tool spec"):
                await worker.send_message("untrusted document text")
        assert mk.call_count == 2
        first.shutdown.assert_awaited_once()
        second.shutdown.assert_awaited_once()
        second.send_message.assert_not_awaited()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_kiro_without_a_projection_confirms_through_the_bracketed_authored_read(
        self, tmp_path
    ):
        """With no prepared projection the effective check answers True only when the
        pre-spawn authored read is handed back to it."""
        client = _zero_tool_client()
        client.is_ready = True
        client.backend = ACP_BACKEND_KIRO
        client.spec_zero_tools = False
        client.authored_spec_declares_zero_tools = MagicMock(return_value=True)
        client.effective_spec_declares_zero_tools = MagicMock(
            side_effect=lambda *, authored_before_spawn=False: authored_before_spawn
        )
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO)
            await worker.start()
        client.effective_spec_declares_zero_tools.assert_called_once_with(
            authored_before_spawn=True
        )
        assert worker._client is client

    @pytest.mark.asyncio
    async def test_kiro_whose_pre_spawn_authored_read_is_unconfirmed_refuses_to_start(
        self, tmp_path
    ):
        client = _zero_tool_client()
        client.is_ready = True
        client.backend = ACP_BACKEND_KIRO
        client.spec_zero_tools = False
        client.authored_spec_declares_zero_tools = MagicMock(return_value=False)
        client.effective_spec_declares_zero_tools = MagicMock(
            side_effect=lambda *, authored_before_spawn=False: authored_before_spawn
        )
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_KIRO)
            with pytest.raises(RuntimeError, match="could not be confirmed"):
                await worker.send_message("untrusted document text")
        client.effective_spec_declares_zero_tools.assert_called_once_with(
            authored_before_spawn=False
        )
        client.shutdown.assert_awaited_once()
        client.send_message.assert_not_awaited()
        assert worker._client is None

    @pytest.mark.asyncio
    async def test_the_authored_read_precedes_the_spawn_of_the_kiro_client(self, tmp_path):
        calls: list[str] = []
        client = _zero_tool_client()
        client.is_ready = True
        client.backend = ACP_BACKEND_KIRO
        client.spec_zero_tools = False
        client.authored_spec_declares_zero_tools = MagicMock(
            side_effect=lambda: calls.append("authored") or True
        )
        client.ensure_ready.side_effect = lambda: calls.append("ready")
        client.effective_spec_declares_zero_tools = MagicMock(
            side_effect=lambda **_kwargs: calls.append("effective") or True
        )
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            await AcpWorker(acp_backend=ACP_BACKEND_KIRO).start()
        assert calls == ["authored", "ready", "effective"]

    @pytest.mark.asyncio
    async def test_the_fallback_client_is_read_before_its_own_spawn(self, tmp_path):
        """The Kiro client that replaces an unconfirmed opencode session reads the
        authored spec before ITS ensure_ready; the opencode client reads nothing."""
        calls: list[str] = []
        first = _zero_tool_client()
        first.is_ready = True
        first.backend = ACP_BACKEND_OPENCODE
        first.spec_zero_tools = False
        first.authored_spec_declares_zero_tools = MagicMock(
            side_effect=lambda: calls.append("first-authored") or True
        )
        first.ensure_ready.side_effect = lambda: calls.append("first-ready")
        second = _zero_tool_client()
        second.is_ready = True
        second.backend = ACP_BACKEND_KIRO
        second.spec_zero_tools = False
        second.authored_spec_declares_zero_tools = MagicMock(
            side_effect=lambda: calls.append("second-authored") or True
        )
        second.ensure_ready.side_effect = lambda: calls.append("second-ready")
        second.effective_spec_declares_zero_tools = MagicMock(
            side_effect=lambda **_kwargs: calls.append("second-effective") or True
        )
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]),
        ):
            await AcpWorker(acp_backend=ACP_BACKEND_OPENCODE).start()
        assert calls == ["first-ready", "second-authored", "second-ready", "second-effective"]
        first.authored_spec_declares_zero_tools.assert_not_called()
        second.effective_spec_declares_zero_tools.assert_called_once_with(
            authored_before_spawn=True
        )

    @pytest.mark.asyncio
    async def test_fallback_is_sticky_across_reset_conversation(self, tmp_path):
        """Once a worker has fallen back to Kiro, ``reset_conversation`` (which
        just re-runs ``start()``) must stay on Kiro rather than re-resolving
        the originally configured backend and falling back again."""
        first = _zero_tool_client()
        first.is_ready = True
        first.backend = ACP_BACKEND_OPENCODE
        first.spec_zero_tools = False
        second = _zero_tool_client()
        second.is_ready = True
        second.backend = ACP_BACKEND_KIRO
        second.spec_zero_tools = False
        third = _zero_tool_client()
        third.is_ready = True
        third.backend = ACP_BACKEND_KIRO
        third.spec_zero_tools = False
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "kiro_crew.knowledge.llm_pool.AcpClient",
                side_effect=[first, second, third],
            ) as mk,
        ):
            worker = AcpWorker(acp_backend=ACP_BACKEND_OPENCODE)
            await worker.start()
            assert worker._acp_backend == ACP_BACKEND_KIRO
            await worker.reset_conversation()
        # Third construction (the reset) must be Kiro, not opencode again.
        assert mk.call_args_list[2].kwargs["acp_backend"] == ACP_BACKEND_KIRO
        assert worker._acp_backend == ACP_BACKEND_KIRO
        assert worker._client is third


def _ctx_with(registry):
    return type("Ctx", (), {"providers": registry})()


class TestCleanBindingForkContracts:
    def test_keeps_the_four_binding_keys(self):
        binding = _clean_binding(
            {
                "acp_backend": "claude",
                "extra_env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8484"},
                "model": "fast",
                "model_switch_method": "session_set_model",
            }
        )
        assert binding == {
            "acp_backend": "claude",
            "extra_env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8484"},
            "model": "fast",
            "model_switch_method": "session_set_model",
        }


class TestRuntimeWorkerForkContracts:
    @pytest.mark.asyncio
    async def test_kas_uses_runtime_provider_and_collects_text(self):
        from kiro_crew.acp.types import AcpEvent

        provider = MagicMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.client = MagicMock()
        provider.client.ensure_ready = AsyncMock()
        provider.client._pid = None
        provider.client._handle.consumed_agent_spec = {"tools": []}
        provider.is_alive.return_value = True
        provider.stream = None

        async def stream(prompt):
            assert prompt == "extract this"
            yield AcpEvent(kind="thinking_chunk", text="omit")
            yield AcpEvent(kind="text_chunk", text='{"entities":')
            yield AcpEvent(kind="text_chunk", text="[]}")

        provider.stream = stream
        binding = {"acp_backend": "kas", "model": "test-model"}
        worker = AcpWorker(sandbox_mode="auto")
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=(binding, ""),
            ),
            patch("kiro_crew.providers.acp.AcpProvider", return_value=provider) as factory,
            patch("kiro_crew.knowledge.llm_pool.AcpClient") as legacy,
        ):
            await worker.start()
            assert await worker.send_message("extract this") == '{"entities":[]}'
            factory.assert_called_once()
            for name, value in {
                "agent": "kirocrew-knowledge",
                "sandbox_mode": "auto",
                **binding,
            }.items():
                assert factory.call_args.kwargs[name] == value
            legacy.assert_not_called()
            await worker.shutdown()
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_failed_runtime_start_is_closed_without_legacy_fallback(self):
        provider = MagicMock()
        provider.start = AsyncMock(side_effect=RuntimeError("adapter failed"))
        provider.shutdown = AsyncMock()
        worker = AcpWorker(sandbox_mode="auto")
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=({"acp_backend": "kas"}, ""),
            ),
            patch("kiro_crew.providers.acp.AcpProvider", return_value=provider),
            patch("kiro_crew.knowledge.llm_pool.AcpClient") as legacy,
        ):
            with pytest.raises(RuntimeError, match="adapter failed"):
                await worker.start()
            legacy.assert_not_called()
        provider.shutdown.assert_awaited_once()
        assert not worker.is_alive()

    @pytest.mark.asyncio
    async def test_codex_without_a_verified_zero_tool_ban_is_refused(self):
        worker = AcpWorker(sandbox_mode="auto")
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=({"acp_backend": "codex"}, ""),
            ),
            patch("kiro_crew.providers.acp.AcpProvider") as provider,
        ):
            with pytest.raises(RuntimeError, match="cannot enforce a zero-tool session"):
                await worker.start()
            provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_with_a_nonempty_consumed_tool_list_is_closed(self):
        provider = MagicMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.client._handle.consumed_agent_spec = {"tools": ["bash"]}
        worker = AcpWorker(sandbox_mode="auto")
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=({"acp_backend": "kas"}, ""),
            ),
            patch("kiro_crew.providers.acp.AcpProvider", return_value=provider),
        ):
            with pytest.raises(RuntimeError, match="did not confirm a zero-tool spec"):
                await worker.start()
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kiro_refuses_a_spec_that_changed_during_startup(self):
        client = AsyncMock()
        client.authored_spec_declares_zero_tools = MagicMock(return_value=True)
        client.effective_spec_declares_zero_tools = MagicMock(return_value=False)
        worker = AcpWorker(sandbox_mode="auto")
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=({}, ""),
            ),
            patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=client),
        ):
            with pytest.raises(RuntimeError, match="zero-tool spec"):
                await worker.start()
        client.authored_spec_declares_zero_tools.assert_called_once_with()
        client.effective_spec_declares_zero_tools.assert_called_once_with(
            authored_before_spawn=True
        )
        client.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["timeout", "error", "cancel"])
    async def test_unfinished_runtime_turn_is_closed(self, failure):
        from kiro_crew.acp.types import AcpEvent

        entered = asyncio.Event()
        provider = MagicMock()
        provider.is_alive.return_value = True
        provider.shutdown = AsyncMock()

        async def stream(prompt):
            entered.set()
            if failure == "error":
                raise RuntimeError("provider failed")
            else:
                await asyncio.Event().wait()
            yield AcpEvent(kind="complete")

        provider.stream = stream
        worker = AcpWorker()
        worker._provider = provider
        worker._client = MagicMock()
        task = asyncio.create_task(worker.send_message("extract", timeout=0.05))
        await await_until(entered, what="the runtime turn entered its provider")
        if failure == "cancel":
            task.cancel()
        error = {
            "timeout": asyncio.TimeoutError,
            "error": RuntimeError,
            "cancel": asyncio.CancelledError,
        }[failure]
        with pytest.raises(error):
            await task
        provider.shutdown.assert_awaited_once()
        assert not worker.is_alive()


class TestBindingValidationForkContracts:
    def test_drops_keys_outside_the_whitelist(self):
        # audit_source and sandbox_mode belong to the build site, and work_dir
        # would move the worker's whole session. None may arrive from a registry.
        binding = _clean_binding(
            {
                "model": "fast",
                "audit_source": None,
                "sandbox_mode": "off",
                "work_dir": "/tmp/elsewhere",
                "permission_mode": "bypassPermissions",
            }
        )
        assert binding == {"model": "fast"}

    def test_coerces_env_values_to_strings(self):
        binding = _clean_binding({"extra_env": {"API_TIMEOUT_MS": 3000000}})
        assert binding == {"extra_env": {"API_TIMEOUT_MS": "3000000"}}

    def test_drops_wrong_typed_and_empty_values(self):
        # A hand-edited engine map must not reach AcpClient as an int or a list
        # and fail somewhere far from its cause.
        assert (
            _clean_binding(
                {
                    "model": 7,
                    "acp_backend": "",
                    "extra_env": [],
                    "model_switch_method": None,
                }
            )
            == {}
        )


class TestResolveClientBindingForkContracts:
    def test_default_registry_has_nothing_to_miss(self):
        from kiro_crew.platform.defaults import DefaultProviderRegistry

        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=_ctx_with(DefaultProviderRegistry()),
        ):
            assert _resolve_client_binding() == ({}, "")

    def test_binding_is_returned_with_no_warning(self):
        class NexusProviderRegistry:
            def agent_client_binding(self, agent_name):
                assert agent_name == "kirocrew-knowledge"
                return {
                    "acp_backend": "claude",
                    "extra_env": {"ANTHROPIC_MODEL": "fast"},
                }

        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=_ctx_with(NexusProviderRegistry()),
        ):
            binding, unbound = _resolve_client_binding()
        assert unbound == ""
        assert binding == {
            "acp_backend": "claude",
            "extra_env": {"ANTHROPIC_MODEL": "fast"},
        }

    def test_registry_with_no_binding_for_this_agent_is_named(self):
        class NexusProviderRegistry:
            def agent_client_binding(self, agent_name):
                return None

        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=_ctx_with(NexusProviderRegistry()),
        ):
            assert _resolve_client_binding() == ({}, "NexusProviderRegistry")

    def test_registry_predating_the_seam_is_named(self):
        # An older companion has no agent_client_binding at all. It must be
        # reported, not crashed on.
        class NexusProviderRegistry:
            pass

        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=_ctx_with(NexusProviderRegistry()),
        ):
            assert _resolve_client_binding() == ({}, "NexusProviderRegistry")

    def test_raising_lookup_cannot_silently_change_the_execution_seat(self):
        class NexusProviderRegistry:
            def agent_client_binding(self, agent_name):
                raise RuntimeError("bad engine map")

        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=_ctx_with(NexusProviderRegistry()),
        ):
            with pytest.raises(RuntimeError, match="bad engine map"):
                _resolve_client_binding()

    def test_missing_context_is_reported_to_the_caller(self):
        with patch(
            "kiro_crew.platform.context.current_context",
            side_effect=RuntimeError("no context"),
        ):
            with pytest.raises(RuntimeError, match="no context"):
                _resolve_client_binding()


class TestWorkerAppliesBindingForkContracts:
    @pytest.mark.asyncio
    async def test_binding_reaches_the_client_without_displacing_pool_kwargs(self):
        worker = AcpWorker(sandbox_mode="off")
        binding = {"acp_backend": "claude", "extra_env": {"ANTHROPIC_MODEL": "fast"}}
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=(binding, ""),
            ),
            patch("kiro_crew.knowledge.llm_pool.AcpClient") as client_cls,
        ):
            client_cls.return_value = _zero_tool_client()
            client_cls.return_value.restrict_tools = MagicMock()
            await worker.start()

        kwargs = client_cls.call_args.kwargs
        assert kwargs["acp_backend"] == "claude"
        assert kwargs["extra_env"] == {"ANTHROPIC_MODEL": "fast"}
        # The three the pool owns survive the binding.
        assert kwargs["agent"] == "kirocrew-knowledge"
        assert kwargs["sandbox_mode"] == "off"
        assert kwargs["audit_source"] == "subagent"
        client_cls.return_value.restrict_tools.assert_called_once_with([])

    @pytest.mark.asyncio
    async def test_binding_model_beats_the_agent_spec_pin(self):
        """A bound engine's model must reach the wire, not the agent spec's.

        ``~/.kiro/agents/kirocrew-knowledge.json`` pins a backend model id; a
        mapped engine may name something else entirely (a fleet-router ROLE, not
        an Anthropic id). AcpClient sends ``self._model`` and falls back to the
        agent spec only when it is unset, so passing the binding's model through
        is what makes the engine authoritative. Pinned here because an ambiguous
        merge would surface as an invalid-model error at runtime, on whichever
        edition resolved the other way.
        """
        worker = AcpWorker(sandbox_mode="off")
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=({"model": "fast"}, ""),
            ),
            patch("kiro_crew.knowledge.llm_pool.AcpClient") as client_cls,
        ):
            client_cls.return_value = _zero_tool_client()
            await worker.start()

        assert client_cls.call_args.kwargs["model"] == "fast"

    @pytest.mark.asyncio
    async def test_unbound_registry_warns_naming_registry_and_agent(self, caplog):
        worker = AcpWorker(sandbox_mode="off")
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=({}, "NexusProviderRegistry"),
            ),
            patch("kiro_crew.knowledge.llm_pool.AcpClient") as client_cls,
        ):
            client_cls.return_value = _zero_tool_client()
            with caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"):
                await worker.start()

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("NexusProviderRegistry" in m and "kirocrew-knowledge" in m for m in warnings)
        # No binding to apply: the client is built exactly as it was before.
        assert set(client_cls.call_args.kwargs) == {
            "agent",
            "sandbox_mode",
            "audit_source",
            "acp_backend",
        }

    @pytest.mark.asyncio
    async def test_default_registry_is_quiet(self, caplog):
        worker = AcpWorker(sandbox_mode="off")
        with (
            patch(
                "kiro_crew.knowledge.llm_pool._resolve_client_binding",
                return_value=({}, ""),
            ),
            patch("kiro_crew.knowledge.llm_pool.AcpClient") as client_cls,
        ):
            client_cls.return_value = _zero_tool_client()
            with caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"):
                await worker.start()

        assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []


class TestLLMPoolBasicsForkContracts:
    @pytest.mark.asyncio
    async def test_send_batch_abandons_after_terminal_error(self):
        """A backend the ACP layer called terminal is not asked again.

        An exhausted usage allowance arrives as ``transient=False``; re-sending
        the rest of the batch reproduces it once per prompt and spends the
        allowance's error budget for nothing.
        """
        pool = _make_pool_with_fake_workers(pool_size=1)
        worker = _FailingWorker(_TerminalError("monthly usage limit reached"))
        pool._workers[0] = worker

        results = await pool.send_batch(["a", "b", "c"])

        assert results == ["", "", ""]
        assert worker.calls == 1

    @pytest.mark.asyncio
    async def test_send_batch_continues_after_transient_error(self):
        """A transient failure still costs only its own item."""
        pool = _make_pool_with_fake_workers(pool_size=1)
        worker = _FailingWorker(_TransientError("bedrock 503"), fail_times=1)
        pool._workers[0] = worker

        results = await pool.send_batch(["a", "b", "c"])

        assert worker.calls == 3
        assert results.count("ok") == 2


class _TerminalError(Exception):
    """Mirrors an ``AcpError`` the ACP layer classified as non-retryable."""

    transient = False


class _TransientError(Exception):
    """Mirrors an ``AcpError`` a retry could still satisfy."""

    transient = True


class _FailingWorker(Worker):
    """Raises *error* for the first *fail_times* calls, then answers 'ok'."""

    def __init__(self, error: Exception, fail_times: int | None = None):
        self._error = error
        self._fail_times = fail_times
        self.calls = 0
        self.calls_since_reset = 0
        self._alive = True

    async def start(self) -> None:
        self._alive = True

    async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
        self.calls += 1
        if self._fail_times is None or self.calls <= self._fail_times:
            raise self._error
        return "ok"

    async def shutdown(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    async def reset_conversation(self) -> None:
        self.calls_since_reset = 0


class _TimeoutRecordingWorker(FakeWorker):
    """FakeWorker that records the timeout each send arrived with."""

    def __init__(self):
        super().__init__()
        self.timeouts: list[float] = []

    async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
        self.timeouts.append(timeout)
        return await super().send_message(prompt, timeout=timeout)


class TestBoundTimeoutFloorForkContracts:
    def test_unbound_floor_is_zero_even_with_config(self):
        from kiro_crew.knowledge.llm_pool import _get_timeout_floor

        assert _get_timeout_floor({"knowledge": {"llm_timeout_secs": 45}}, bound=False) == 0.0

    def test_bound_default_floor(self):
        from kiro_crew.knowledge.llm_pool import BOUND_TIMEOUT_FLOOR, _get_timeout_floor

        assert _get_timeout_floor({}, bound=True) == BOUND_TIMEOUT_FLOOR

    @pytest.mark.parametrize("raw,expected", [(45, 45.0), (12.5, 12.5), (900, 900.0)])
    def test_bound_config_override(self, raw, expected):
        from kiro_crew.knowledge.llm_pool import _get_timeout_floor

        cfg = {"knowledge": {"llm_timeout_secs": raw}}
        assert _get_timeout_floor(cfg, bound=True) == expected

    @pytest.mark.parametrize("raw", ["x", -1, 0, True, None, [300]])
    def test_bound_malformed_config_falls_back_not_off(self, raw):
        """A broken knob must not silently disable the floor."""
        from kiro_crew.knowledge.llm_pool import BOUND_TIMEOUT_FLOOR, _get_timeout_floor

        cfg = {"knowledge": {"llm_timeout_secs": raw}}
        assert _get_timeout_floor(cfg, bound=True) == BOUND_TIMEOUT_FLOOR

    @pytest.mark.asyncio
    async def test_bound_pool_floors_caller_timeout(self):
        pool = _make_pool_with_fake_workers(pool_size=1)
        pool._timeout_floor = 300.0
        worker = _TimeoutRecordingWorker()
        pool._workers[0] = worker

        await pool.send("p", timeout=30.0)

        assert worker.timeouts == [300.0]

    @pytest.mark.asyncio
    async def test_bound_pool_respects_larger_caller_timeout(self):
        pool = _make_pool_with_fake_workers(pool_size=1)
        pool._timeout_floor = 300.0
        worker = _TimeoutRecordingWorker()
        pool._workers[0] = worker

        await pool.send("p", timeout=600.0)

        assert worker.timeouts == [600.0]

    @pytest.mark.asyncio
    async def test_unbound_pool_leaves_caller_timeout(self):
        pool = _make_pool_with_fake_workers(pool_size=1)
        worker = _TimeoutRecordingWorker()
        pool._workers[0] = worker

        await pool.send("p", timeout=30.0)

        assert worker.timeouts == [30.0]

    @pytest.mark.asyncio
    async def test_send_batch_floors_every_item(self):
        pool = _make_pool_with_fake_workers(pool_size=1)
        pool._timeout_floor = 300.0
        worker = _TimeoutRecordingWorker()
        pool._workers[0] = worker

        await pool.send_batch(["a", "b"], timeout=60.0)

        assert worker.timeouts == [300.0, 300.0]


class TestExtractionPoolSizeForkContracts:
    def test_config_size_accepts_a_single_lane_worker(self):
        from kiro_crew.knowledge.llm_pool import _get_pool_size

        assert _get_pool_size({"knowledge": {"extraction_pool_size": 1}}) == 1

    def test_absent_config_keeps_default(self):
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size

        assert _get_pool_size({}) == DEFAULT_POOL_SIZE

    @pytest.mark.parametrize("raw", [0, -1, 11, "2", 2.5, True, None, [1]])
    def test_malformed_or_out_of_range_falls_back(self, raw):
        """A broken knob must not starve or explode the pool."""
        from kiro_crew.knowledge.llm_pool import DEFAULT_POOL_SIZE, _get_pool_size

        cfg = {"knowledge": {"extraction_pool_size": raw}}
        assert _get_pool_size(cfg) == DEFAULT_POOL_SIZE

    def test_fetch_pool_can_opt_out_of_extraction_sizing(self):
        pool = LLMPool(pool_size=1, use_config_pool_size=False)
        assert pool._pool_size == 1
        assert pool._use_config_pool_size is False

    def test_default_construction_is_config_driven(self):
        pool = LLMPool()
        assert pool._use_config_pool_size is True


class TestFloorHitAlarmForkContracts:
    @pytest.mark.asyncio
    async def test_bound_pool_abandons_batch_on_floor_timeout(self, caplog):
        from kiro_crew.acp.client import AcpTimeoutError

        pool = _make_pool_with_fake_workers(pool_size=1)
        pool._timeout_floor = 300.0
        worker = _FailingWorker(AcpTimeoutError())
        pool._workers[0] = worker

        with caplog.at_level(logging.WARNING):
            results = await pool.send_batch(["a", "b", "c"])

        assert results == ["", "", ""]
        assert worker.calls == 1
        assert "alarm, not a budget" in caplog.text

    @pytest.mark.asyncio
    async def test_unbound_pool_keeps_per_item_timeout_failures(self):
        from kiro_crew.acp.client import AcpTimeoutError

        pool = _make_pool_with_fake_workers(pool_size=1)
        worker = _FailingWorker(AcpTimeoutError())
        pool._workers[0] = worker

        results = await pool.send_batch(["a", "b", "c"])

        assert results == ["", "", ""]
        assert worker.calls == 3

    @pytest.mark.asyncio
    async def test_bound_pool_non_timeout_transient_still_per_item(self):
        """The abandon path is timeout-specific; transient errors keep retrying."""
        pool = _make_pool_with_fake_workers(pool_size=1)
        pool._timeout_floor = 300.0
        worker = _FailingWorker(_TransientError("connection reset"), fail_times=1)
        pool._workers[0] = worker

        results = await pool.send_batch(["a", "b", "c"])

        assert worker.calls == 3
        assert results.count("ok") == 2
