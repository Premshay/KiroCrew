"""Unit tests for the unified LLM pool."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.knowledge.llm_pool import (
    DEFAULT_IDLE_TTL_SECS,
    AcpWorker,
    CCWorker,
    LLMPool,
    Worker,
    _clean_binding,
    _get_idle_ttl,
    _get_provider_type,
    _get_sandbox_mode,
    _read_config,
    _resolve_client_binding,
)


def _ctx_with(registry):
    return type("Ctx", (), {"providers": registry})()


class TestCleanBinding:
    """A companion describes an engine here; it does not get to hand arbitrary
    kwargs to a constructor."""

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
        assert _clean_binding(
            {"model": 7, "acp_backend": "", "extra_env": [], "model_switch_method": None}
        ) == {}


class TestResolveClientBinding:
    """The pool cannot take a factory-built provider, so it reads the binding
    through the narrow seam instead. These cover both of its outcomes."""

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
                return {"acp_backend": "claude", "extra_env": {"ANTHROPIC_MODEL": "fast"}}

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

    def test_raising_lookup_is_reported_not_propagated(self):
        class NexusProviderRegistry:
            def agent_client_binding(self, agent_name):
                raise RuntimeError("bad engine map")

        with patch(
            "kiro_crew.platform.context.current_context",
            return_value=_ctx_with(NexusProviderRegistry()),
        ):
            assert _resolve_client_binding() == ({}, "NexusProviderRegistry")

    def test_missing_context_stays_silent(self):
        # Resolving a binding must never break the pool that needs one.
        with patch(
            "kiro_crew.platform.context.current_context",
            side_effect=RuntimeError("no context"),
        ):
            assert _resolve_client_binding() == ({}, "")


class TestWorkerAppliesBinding:
    """The wiring, not just the helpers."""

    @pytest.mark.asyncio
    async def test_binding_reaches_the_client_without_displacing_pool_kwargs(self):
        worker = AcpWorker(sandbox_mode="off")
        binding = {"acp_backend": "claude", "extra_env": {"ANTHROPIC_MODEL": "fast"}}
        with patch(
            "kiro_crew.knowledge.llm_pool._resolve_client_binding",
            return_value=(binding, ""),
        ), patch("kiro_crew.knowledge.llm_pool.AcpClient") as client_cls:
            client_cls.return_value = AsyncMock()
            await worker.start()

        kwargs = client_cls.call_args.kwargs
        assert kwargs["acp_backend"] == "claude"
        assert kwargs["extra_env"] == {"ANTHROPIC_MODEL": "fast"}
        # The three the pool owns survive the binding.
        assert kwargs["agent"] == "kirocrew-knowledge"
        assert kwargs["sandbox_mode"] == "off"
        assert kwargs["audit_source"] == "subagent"

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
        with patch(
            "kiro_crew.knowledge.llm_pool._resolve_client_binding",
            return_value=({"model": "fast"}, ""),
        ), patch("kiro_crew.knowledge.llm_pool.AcpClient") as client_cls:
            client_cls.return_value = AsyncMock()
            await worker.start()

        assert client_cls.call_args.kwargs["model"] == "fast"

    @pytest.mark.asyncio
    async def test_unbound_registry_warns_naming_registry_and_agent(self, caplog):
        worker = AcpWorker(sandbox_mode="off")
        with patch(
            "kiro_crew.knowledge.llm_pool._resolve_client_binding",
            return_value=({}, "NexusProviderRegistry"),
        ), patch("kiro_crew.knowledge.llm_pool.AcpClient") as client_cls:
            client_cls.return_value = AsyncMock()
            with caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"):
                await worker.start()

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "NexusProviderRegistry" in m and "kirocrew-knowledge" in m for m in warnings
        )
        # No binding to apply: the client is built exactly as it was before.
        assert set(client_cls.call_args.kwargs) == {
            "agent",
            "sandbox_mode",
            "audit_source",
        }

    @pytest.mark.asyncio
    async def test_default_registry_is_quiet(self, caplog):
        worker = AcpWorker(sandbox_mode="off")
        with patch(
            "kiro_crew.knowledge.llm_pool._resolve_client_binding", return_value=({}, "")
        ), patch("kiro_crew.knowledge.llm_pool.AcpClient") as client_cls:
            client_cls.return_value = AsyncMock()
            with caplog.at_level("WARNING", logger="kiro_crew.knowledge.llm_pool"):
                await worker.start()

        assert [r.getMessage() for r in caplog.records if r.levelname == "WARNING"] == []


@pytest.fixture(autouse=True)
def _config_dir_tracks_patched_home(monkeypatch):
    """Keep ``llm_pool.config_dir()`` pointed at ``<patched home>/.kirocrew``.

    The data home moved from ``~/.kirocrew`` to ``~/.kiro/crew`` (``config_dir()``),
    and ``_read_config`` now reads ``config_dir()/config.json`` rather than
    ``Path.home()/".kirocrew"/"config.json"``. These tests patch
    ``llm_pool.Path.home`` per-test and write ``config.json`` under
    ``<home>/.kirocrew`` — but ``config_dir()`` reads ``KIROCREW_HOME`` (pinned to
    a *different* tmp dir by the conftest ``_isolate_kirocrew_home`` fixture), so
    without this redirect the config would never be found. Redirect
    ``config_dir`` to ``Path.home()/".kirocrew"`` (evaluated lazily, so it tracks
    whatever ``Path.home()`` each test patches), preserving the existing
    ``.kirocrew/config.json`` layout the tests build.
    """
    monkeypatch.setattr(
        "kiro_crew.knowledge.llm_pool.config_dir", lambda: Path.home() / ".kirocrew"
    )

# ---------------------------------------------------------------------------
# Fixtures — mock workers that don't spawn real processes
# ---------------------------------------------------------------------------


class FakeWorker(Worker):
    """In-memory worker for testing pool mechanics."""

    def __init__(self, responses: list[str] | None = None):
        self._responses = list(responses or ["ok"])
        self._call_count = 0
        self._alive = True
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
        if not self._alive:
            raise RuntimeError("worker is dead")
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        return self._responses[idx]

    async def shutdown(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


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


def _make_pool_with_fake_workers(
    pool_size: int = 3, responses: list[str] | None = None
) -> LLMPool:
    """Create a pool pre-loaded with FakeWorkers (skips real process spawn)."""
    pool = LLMPool(pool_size=pool_size)
    pool._started = True
    pool._provider_type = "test"
    for i in range(pool_size):
        worker = FakeWorker(responses=responses)
        worker._started = True
        pool._workers.append(worker)
        pool._available.put_nowait(i)
    return pool


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
    chat, honouring ``agent.sandbox`` (default ``"off"`` — defers to kiro-cli's
    internal agent sandbox). The earlier hardcoded ``"off"`` bypassed
    least-privilege; these lock in the restored behaviour."""

    def test_default_is_off(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "off"

    def test_reads_sandbox_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"agent": {"sandbox": "off"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "off"

    def test_unparseable_config_defaults_off(self, tmp_path):
        # A file that isn't valid JSON parses to {} → sandbox UNSET → the
        # intended default "off" (not a present-but-malformed value).
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text("not json")
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_sandbox_mode() == "off"

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
        Present-but-invalid fails secure to 'auto'; absent takes 'off'."""
        assert _get_sandbox_mode({"agent": {"sandbox": "strict"}}) == "strict"
        assert _get_sandbox_mode({"agent": {"sandbox": "nope"}}) == "auto"  # malformed → fail secure
        assert _get_sandbox_mode({}) == "off"  # unset → intended default


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
        assert _get_sandbox_mode(bad) == "off"

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
        mock_client = AsyncMock()
        mock_client.is_ready = True
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk:
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["sandbox_mode"] == "off"

    @pytest.mark.asyncio
    async def test_start_defaults_sandbox_to_off(self, tmp_path):
        # With no config, the sandbox mode defaults to "off" — deferring
        # isolation to kiro-cli's internal agent sandbox (kiro-cli >= 2.13).
        mock_client = AsyncMock()
        mock_client.is_ready = True
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=mock_client) as mk:
            worker = AcpWorker()
            await worker.start()
        assert mk.call_args.kwargs["sandbox_mode"] == "off"


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
        fresh = AsyncMock()
        fresh.is_ready = True
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh):
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
        fresh = AsyncMock()
        fresh.is_ready = True
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh):
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
        fresh = AsyncMock()
        fresh.is_ready = True
        fresh._pid = 7777
        registered: list[int] = []
        unregistered: list[int] = []
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", return_value=fresh), \
             patch("kiro_crew.knowledge.llm_pool.register_protected_pid",
                   side_effect=registered.append), \
             patch("kiro_crew.knowledge.llm_pool.unregister_protected_pid",
                   side_effect=unregistered.append):
            worker = AcpWorker()
            await worker.start()
            assert registered == [7777], "worker did not shield its PID on start"
            await worker.shutdown()
            assert unregistered == [7777], "worker did not release its PID on shutdown"

    @pytest.mark.asyncio
    async def test_respawn_reshields_new_pid(self, tmp_path):
        """A re-``start`` (respawn under a new PID) must release the old PID's
        shield and register the new one, so a dead PID is never left shielded."""
        first = AsyncMock()
        first.is_ready = True
        first._pid = 100
        second = AsyncMock()
        second.is_ready = True
        second._pid = 200
        registered: list[int] = []
        unregistered: list[int] = []
        with patch("pathlib.Path.home", return_value=tmp_path), \
             patch("kiro_crew.knowledge.llm_pool.AcpClient", side_effect=[first, second]), \
             patch("kiro_crew.knowledge.llm_pool.register_protected_pid",
                   side_effect=registered.append), \
             patch("kiro_crew.knowledge.llm_pool.unregister_protected_pid",
                   side_effect=unregistered.append):
            worker = AcpWorker()
            await worker.start()     # register 100
            await worker.start()     # stale-drop: unregister 100, then register 200
        assert registered == [100, 200]
        assert unregistered == [100]


# ---------------------------------------------------------------------------
# Tests: CCWorker (mocked subprocess)
# ---------------------------------------------------------------------------


class TestCCWorker:
    @pytest.mark.asyncio
    async def test_is_alive_no_proc(self):
        worker = CCWorker()
        assert worker.is_alive() is False

    @pytest.mark.asyncio
    async def test_shutdown_no_proc(self):
        worker = CCWorker()
        await worker.shutdown()  # should not raise

    @pytest.mark.asyncio
    async def test_start_raises_without_claude(self):
        with patch("kiro_crew.knowledge.llm_pool.shutil.which", return_value=None):
            worker = CCWorker()
            with pytest.raises(RuntimeError, match="claude CLI not found"):
                await worker.start()


# ---------------------------------------------------------------------------
# Tests: fetch_url_content with pool
# ---------------------------------------------------------------------------


class TestFetchUrlContent:
    @pytest.mark.asyncio
    async def test_fetch_returns_stripped_content(self):
        from kiro_crew.knowledge.agent_fetch import fetch_url_content

        pool = _make_pool_with_fake_workers(pool_size=1, responses=["  This is a document with enough content to pass the minimum length validation check.  "])
        result = await fetch_url_content("https://example.com/doc", pool)
        assert result == "This is a document with enough content to pass the minimum length validation check."

    @pytest.mark.asyncio
    async def test_fetch_raises_on_empty(self):
        from kiro_crew.knowledge.agent_fetch import fetch_url_content

        pool = _make_pool_with_fake_workers(pool_size=1, responses=[""])
        with pytest.raises(RuntimeError, match="empty content"):
            await fetch_url_content("https://example.com/doc", pool)

    @pytest.mark.asyncio
    async def test_fetch_raises_on_whitespace_only(self):
        from kiro_crew.knowledge.agent_fetch import fetch_url_content

        pool = _make_pool_with_fake_workers(pool_size=1, responses=["   \n  "])
        with pytest.raises(RuntimeError, match="empty content"):
            await fetch_url_content("https://example.com/doc", pool)


# ---------------------------------------------------------------------------
# Tests: idle-TTL config reader
# ---------------------------------------------------------------------------


class TestIdleTtlConfig:
    """``knowledge.pool_idle_ttl_secs`` reader: default, override, 0-disable,
    and rejection of bad/typed-wrong values back to the default."""

    def test_default_is_300(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == DEFAULT_IDLE_TTL_SECS == 300.0

    def test_reads_value_from_config(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": 60}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == 60.0

    def test_zero_disables(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": 0}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == 0.0

    def test_negative_falls_back_to_default(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": -5}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == DEFAULT_IDLE_TTL_SECS

    def test_bool_falls_back_to_default(self, tmp_path):
        # JSON ``true`` is an int subclass in Python; must not read as 1s TTL.
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": true}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == DEFAULT_IDLE_TTL_SECS

    def test_string_falls_back_to_default(self, tmp_path):
        config = tmp_path / ".kirocrew" / "config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"knowledge": {"pool_idle_ttl_secs": "600"}}')
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert _get_idle_ttl() == DEFAULT_IDLE_TTL_SECS


# ---------------------------------------------------------------------------
# Tests: idle-TTL reaper (scale-to-zero)
# ---------------------------------------------------------------------------


class TestIdleReaper:
    """The reaper scales a fully-idle pool to zero after the TTL and the pool
    transparently respawns on the next acquire."""

    @pytest.mark.asyncio
    async def test_reaps_when_idle_past_ttl(self):
        pool = _make_pool_with_fake_workers(pool_size=3)
        pool._idle_ttl = 0.01
        pool._in_use = 0
        pool._idle_since = time.monotonic() - 1.0  # well past the TTL
        workers = list(pool._workers)

        reaped = await pool._maybe_scale_to_zero()

        assert reaped is True
        assert pool._started is False
        assert pool._workers == []
        assert all(not w.is_alive() for w in workers)  # every worker shut down
        assert pool._idle_since is None
        assert pool._in_use == 0

    @pytest.mark.asyncio
    async def test_no_reap_while_busy(self):
        pool = _make_pool_with_fake_workers(pool_size=3)
        pool._idle_ttl = 0.01
        pool._in_use = 1  # a worker is checked out
        pool._idle_since = None

        reaped = await pool._maybe_scale_to_zero()

        assert reaped is False
        assert pool._started is True
        assert len(pool._workers) == 3

    @pytest.mark.asyncio
    async def test_no_reap_before_ttl(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        pool._idle_ttl = 100.0
        pool._in_use = 0
        pool._idle_since = time.monotonic()  # just went idle

        reaped = await pool._maybe_scale_to_zero()

        assert reaped is False
        assert pool._started is True
        assert len(pool._workers) == 2

    @pytest.mark.asyncio
    async def test_ttl_zero_never_reaps(self):
        pool = _make_pool_with_fake_workers(pool_size=1)
        pool._idle_ttl = 0.0  # disabled
        pool._in_use = 0
        pool._idle_since = time.monotonic() - 10_000

        reaped = await pool._maybe_scale_to_zero()

        assert reaped is False
        assert pool._started is True

    @pytest.mark.asyncio
    async def test_release_marks_idle_transition(self):
        pool = _make_pool_with_fake_workers(pool_size=2)
        idx, _ = await pool.acquire()
        assert pool._in_use == 1
        assert pool._idle_since is None  # busy → no idle clock

        pool.release(idx)
        assert pool._in_use == 0
        assert pool._idle_since is not None  # idle clock started

    @pytest.mark.asyncio
    async def test_acquire_respawns_after_reap(self, tmp_path):
        # Simulate a pool the reaper already scaled to zero.
        pool = LLMPool(pool_size=2)
        pool._started = False
        pool._workers = []

        created: list[FakeWorker] = []

        async def _fake_create():
            w = FakeWorker(responses=["respawned"])
            w._started = True
            created.append(w)
            return w

        pool._create_worker = _fake_create  # type: ignore[assignment]
        # No config on disk → idle_ttl defaults to 300 (>0) → a reaper is armed.
        with patch("pathlib.Path.home", return_value=tmp_path):
            idx, worker = await pool.acquire()

        assert pool._started is True
        assert isinstance(worker, FakeWorker)
        assert worker.is_alive()
        assert len(created) == 2  # pool respawned to full size
        assert pool._reaper_task is not None

        pool.release(idx)
        await pool.shutdown()  # cancels the armed reaper task
        assert pool._reaper_task is None

    @pytest.mark.asyncio
    async def test_shutdown_cancels_reaper(self, tmp_path):
        pool = LLMPool(pool_size=1)

        async def _fake_create():
            w = FakeWorker()
            w._started = True
            return w

        pool._create_worker = _fake_create  # type: ignore[assignment]
        with patch("pathlib.Path.home", return_value=tmp_path):
            await pool.start()
        assert pool._reaper_task is not None
        task = pool._reaper_task

        await pool.shutdown()

        assert task.cancelled() or task.done()
        assert pool._reaper_task is None
        assert pool._started is False

    @pytest.mark.asyncio
    async def test_shutdown_drains_abandoned_reaping_workers(self):
        # Simulate a reaper that shutdown() cancelled mid-teardown: it left the
        # workers it was shutting down stashed on _reaping_workers. shutdown()
        # must still drain them (review-bot post 3).
        pool = _make_pool_with_fake_workers(pool_size=2)
        abandoned = [FakeWorker(), FakeWorker()]
        for w in abandoned:
            w._started = True
        pool._reaping_workers = abandoned
        live = list(pool._workers)

        await pool.shutdown()

        assert all(not w.is_alive() for w in abandoned)  # abandoned set drained
        assert all(not w.is_alive() for w in live)  # live set drained too
        assert pool._reaping_workers is None
        assert pool._started is False


class _TimeoutRecordingWorker(FakeWorker):
    """FakeWorker that records the timeout each send arrived with."""

    def __init__(self):
        super().__init__()
        self.timeouts: list[float] = []

    async def send_message(self, prompt: str, timeout: float = 60.0) -> str:
        self.timeouts.append(timeout)
        return await super().send_message(prompt, timeout=timeout)


class TestBoundTimeoutFloor:
    """Bound pools floor caller timeouts; unbound pools leave them alone.

    Every production caller passes an explicit cloud-tuned timeout (30-120 s),
    so a bound pool cannot fix the local-lane queueing problem by changing
    defaults — it has to raise the effective value at the send site. Unbound
    pools must stay byte-identical: a fast-fail intent against a cloud
    backend is deliberate.
    """

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
