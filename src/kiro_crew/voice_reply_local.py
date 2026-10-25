"""Local Piper and Pocket synthesis, streaming and model validation.

Re-exported by voice_reply; its mirror preserves patches through that import path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import shutil
import struct
from typing import TYPE_CHECKING, AsyncGenerator

from kiro_crew import piper_runtime
from kiro_crew.piper_runtime import REQUEST_TIMEOUT_SECONDS as _PIPER_STREAM_TIMEOUT_SECONDS
from kiro_crew.piper_worker import MAX_AUDIO_BYTES as _PIPER_MAX_AUDIO_BYTES
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.platform_compat import IS_WINDOWS
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_env,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.security import (
    PathResolutionStalled,
    is_sensitive_path,
    redact,
    redact_credentials,
    redact_exfiltration_urls,
)

if TYPE_CHECKING:
    from kiro_crew.piper_runtime import PiperRuntime
    from kiro_crew.voice_reply import (
        VoiceSynthesisError,
        _new_wav_path,
        _run_tts_subprocess,
        split_sentences,
        strip_markdown,
    )

logger = logging.getLogger("kiro_crew.voice_reply")

DEFAULT_LENGTH_SCALE = 1.0

_PIPER_MAX_STDERR_BYTES = 4096

_PIPER_MAX_CONFIG_BYTES = 64 * 1024

_PIPER_REAP_TIMEOUT_SECONDS = 5

_PIPER_MAX_PHRASE_CHARS = 240

_PIPER_CHUNK_SECONDS = 0.2

POCKET_STREAM_READ_TIMEOUT = 60


async def _synthesize_piper(
    text: str,
    piper_binary: str = "",
    piper_model: str = "",
    piper_model_config: str = "",
    length_scale: float = 1.0,
    runtime_env: dict[str, str] | None = None,
) -> str | None:
    """Call local piper TTS to generate WAV. Returns temp file path or None.

    Piper doesn't understand SSML; it takes plain text on stdin. ``length_scale``
    controls speed (<1 faster, >1 slower).
    """
    bin_path = await asyncio.to_thread(_resolve_piper_binary, piper_binary)
    if not bin_path:
        logger.warning("piper binary not found (configured=%r)", piper_binary)
        return None
    model = os.path.expanduser(piper_model) if piper_model else ""
    if not model or not await asyncio.to_thread(os.path.isfile, model):
        logger.error("piper model not found: %r", piper_model)
        return None
    # Model config is optional — piper auto-detects `<model>.onnx.json` adjacent
    # to the .onnx file when -c is not supplied.
    cfg = os.path.expanduser(piper_model_config) if piper_model_config else ""

    path = await asyncio.to_thread(_new_wav_path)
    succeeded = False
    try:
        cmd: list[str] = [bin_path, "-m", model, "-f", path]
        if cfg:
            cmd += ["-c", cfg]
        if length_scale != 1.0:
            cmd += ["--length-scale", str(length_scale)]
        succeeded = await _run_tts_subprocess(
            cmd,
            path,
            label="piper",
            display="Piper TTS",
            stdin_text=text,
            # Pocket runs through this same binary and selects its engine/voice
            # by environment, so the overlay travels with the spawn.
            extra_env=runtime_env,
        )
        return path if succeeded else None
    finally:
        if not succeeded:
            try:
                os.unlink(path)
            except OSError:
                pass


async def stream_pocket_speech(
    text: str,
    *,
    voice_id: str,
    piper_binary: str,
    piper_model: str,
    piper_model_config: str = "",
    length_scale: float = 1.0,
) -> AsyncGenerator[bytes, None]:
    """Yield Pocket's progressive Ogg Opus output through the local shim."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    plain = strip_markdown(text).strip()
    if not plain:
        raise RuntimeError("no speakable text")
    bin_path = await asyncio.to_thread(_resolve_piper_binary, piper_binary)
    model = os.path.expanduser(piper_model) if piper_model else ""
    if not bin_path or not model or not await asyncio.to_thread(os.path.isfile, model):
        raise RuntimeError("Pocket TTS is not configured")
    cfg = os.path.expanduser(piper_model_config) if piper_model_config else ""
    cmd: list[str] = [bin_path, "--stream", "-m", model]
    if cfg:
        cmd += ["-c", cfg]
    if length_scale != 1.0:
        cmd += ["--length-scale", str(length_scale)]

    sandbox_cleanup: str | None = None
    proc: asyncio.subprocess.Process | None = None
    stderr_task: asyncio.Task[bytes] | None = None
    yielded = False
    try:
        cmd, sandbox_cleanup = await wrap_argv_async(cmd, mode="standard", _prepare=wrap_argv)
        proc = await create_subprocess_limited(
            *cgroup_scope_argv(cmd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "KIROCREW_TTS_ENGINE": "pocket",
                "KIROCREW_TTS_VOICE": voice_id,
            },
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        proc.stdin.write(plain.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        stderr_task = asyncio.create_task(proc.stderr.read())
        while chunk := await asyncio.wait_for(
            proc.stdout.read(64 * 1024), timeout=POCKET_STREAM_READ_TIMEOUT
        ):
            yielded = True
            yield chunk
        returncode = await proc.wait()
        stderr = await stderr_task
        if returncode != 0 or not yielded:
            detail = redact_log_via_context(stderr.decode(errors="replace"))[-500:].strip()
            raise RuntimeError(f"Pocket stream failed (rc={returncode}): {detail}")
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Pocket stream timed out") from exc
    except SandboxUnavailableError as exc:
        logger.error("Pocket TTS refused by sandbox (%s): %s", exc.kind, exc)
        raise RuntimeError(
            "Pocket TTS is unavailable because its sandbox is not configured"
        ) from exc
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        if stderr_task is not None and not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
        if sandbox_cleanup:
            with contextlib.suppress(OSError):
                os.unlink(sandbox_cleanup)


def _resolve_piper_binary(configured: str = "") -> str | None:
    """Return piper binary path or None if not found.

    Resolution order: explicit ``configured`` path → ``piper`` on PATH →
    the console script inside a conventional ``~/piper-venv``. The venv's
    script directory differs by platform (``Scripts`` on Windows, ``bin``
    elsewhere), so the fallback is built from the platform rather than assumed.
    """
    if configured:
        p = os.path.expanduser(configured)
        return p if _is_executable_file(p) else None
    found = shutil.which("piper")
    if found:
        return found
    scripts = "Scripts" if IS_WINDOWS else "bin"
    exe = "piper.exe" if IS_WINDOWS else "piper"
    fallback = os.path.expanduser(os.path.join("~", "piper-venv", scripts, exe))
    return fallback if _is_executable_file(fallback) else None


def _is_executable_file(path: str) -> bool:
    """Return True when *path* is a file this platform can launch.

    ``os.access(X_OK)`` is not a usable test on Windows — it reports any
    readable file as executable — so there a file that HAS an extension must
    carry a launchable one. Without that, a configured
    ``piper_model_notes.txt`` passes ``is_available``, the caller skips its
    "voice unavailable" notice, and the spawn then fails with no explanation.
    ``PATHEXT`` is read rather than hardcoded because an operator can extend it.
    """
    if not os.path.isfile(path):
        return False
    if IS_WINDOWS:
        suffix = os.path.splitext(path)[1].lower()
        if not suffix:
            # PATHEXT governs how a bare NAME resolves against PATH, not what
            # CreateProcess will launch from an explicit path, and a PE image
            # needs no extension — so an extensionless configured path stands.
            return True
        # PATHEXT is always ';'-separated, which is not `os.pathsep` when this
        # branch is exercised from a POSIX host's tests.
        exts = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        return suffix in {e.strip().lower() for e in exts.split(";") if e.strip()}
    return os.access(path, os.X_OK)


def validate_length_scale(value: object) -> float:
    """Coerce *value* to a finite, positive Piper length-scale.

    Returns :data:`DEFAULT_LENGTH_SCALE` for anything non-numeric, non-finite
    (``inf``/``nan``), zero, or negative. ``float()`` of a very large int can
    raise ``OverflowError``, so that is caught too. Shared by the config loader
    and the dashboard PUT handler so a bad value can never reach synthesis or be
    persisted as unserializable JSON (``Infinity``/``NaN`` would break the
    browser's ``JSON.parse`` of the config GET).
    """
    try:
        scale = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_LENGTH_SCALE
    if not math.isfinite(scale) or scale <= 0:
        return DEFAULT_LENGTH_SCALE
    return scale


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Frame mono signed little-endian 16-bit PCM as an independently playable WAV."""
    if len(pcm) % 2:
        raise ValueError("PCM contains an incomplete sample")
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            len(pcm) + 36,
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            sample_rate,
            sample_rate * 2,
            2,
            16,
            b"data",
            len(pcm),
        )
        + pcm
    )


def _piper_stream_settings(binary: str, model: str, config: str) -> tuple[str, str, str, int]:
    """Resolve the CLI and read its declared PCM format off the event loop."""
    resolved = _resolve_piper_binary(binary)
    if not resolved:
        raise VoiceSynthesisError(
            "voice_unavailable",
            "Piper is unavailable; check its binary and model in Voice settings.",
        )
    model, config, sample_rate = _piper_model_settings(model, config)
    return resolved, model, config, sample_rate


def _piper_model_settings(model: str, config: str) -> tuple[str, str, int]:
    model = os.path.abspath(os.path.expanduser(model)) if model else ""
    if not model:
        raise VoiceSynthesisError("voice_unavailable", "Piper voice model is unavailable.")
    config = os.path.abspath(os.path.expanduser(config)) if config else model + ".json"
    try:
        protected = is_sensitive_path(model) or is_sensitive_path(config)
    except PathResolutionStalled:
        protected = True
    if protected:
        raise VoiceSynthesisError(
            "voice_model_path_forbidden", "Piper model paths are protected or cannot be verified."
        )
    if not os.path.isfile(model):
        raise VoiceSynthesisError("voice_unavailable", "Piper voice model is unavailable.")
    try:
        with open(config, encoding="utf-8") as handle:
            data = handle.read(_PIPER_MAX_CONFIG_BYTES + 1)
        if len(data) > _PIPER_MAX_CONFIG_BYTES:
            raise ValueError("model configuration is too large")
        sample_rate = json.loads(data)["audio"]["sample_rate"]
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("invalid sample rate")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VoiceSynthesisError(
            "voice_model_config_invalid",
            "Piper model configuration has no valid audio sample rate.",
        ) from exc
    return model, config, sample_rate


def _piper_phrases(text: str) -> list[str]:
    """Bound each inference without dropping an unpunctuated or CJK tail."""
    phrases: list[str] = []
    for sentence in split_sentences(text):
        while len(sentence) > _PIPER_MAX_PHRASE_CHARS:
            prefix = sentence[:_PIPER_MAX_PHRASE_CHARS]
            boundaries = list(re.finditer(r"[,，、;；:]|\s+", prefix))
            cut = boundaries[-1].end() if boundaries else len(prefix)
            phrases.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            phrases.append(sentence)
    return phrases


async def streaming_piper_reply(
    text: str,
    *,
    piper_binary: str = "",
    piper_model: str = "",
    piper_model_config: str = "",
    length_scale: float = DEFAULT_LENGTH_SCALE,
    runtime: PiperRuntime | None = None,
    request_id: str = "",
):
    """Yield (index, sample_rate, PCM) from a sandboxed Piper provider.

    A dashboard runtime reuses a compatible Python API worker unless a custom
    CLI was explicitly selected. A failed resident attempt can fall back only
    before any audio was delivered. Both attempts share one synthesis deadline.
    """
    stream = _stream_piper_attempts(
        text,
        piper_binary=piper_binary,
        piper_model=piper_model,
        piper_model_config=piper_model_config,
        length_scale=length_scale,
        runtime=runtime,
        request_id=request_id,
    )
    try:
        async with asyncio.timeout(_PIPER_STREAM_TIMEOUT_SECONDS):
            async with contextlib.aclosing(stream):
                async for frame in stream:
                    yield frame
    except TimeoutError as exc:
        raise VoiceSynthesisError("voice_timeout", "Piper synthesis timed out.") from exc


async def _stream_piper_attempts(
    text: str,
    *,
    piper_binary: str,
    piper_model: str,
    piper_model_config: str,
    length_scale: float,
    runtime: PiperRuntime | None,
    request_id: str,
):
    """Try the resident API, then its existing CLI compatibility path if safe."""
    text = redact(text)
    phrases = _piper_phrases(text)
    if not phrases:
        return
    if runtime is not None and not piper_binary:
        if await asyncio.to_thread(piper_runtime.python_piper_available):
            model, config, sample_rate = await asyncio.to_thread(
                _piper_model_settings, piper_model, piper_model_config
            )
            stream = runtime.stream(
                phrases,
                model=model,
                config=config,
                sample_rate=sample_rate,
                length_scale=validate_length_scale(length_scale),
                request_id=request_id,
            )
            emitted = False
            try:
                async with contextlib.aclosing(stream):
                    async for frame in stream:
                        emitted = True
                        yield frame
            except (VoiceSynthesisError, OSError) as exc:
                # A successful gateway import does not establish that the
                # isolated -E/-P child can import or run the same package. Its
                # generator has retired/reaped the failed worker before this
                # branch can start another process. Never replay partial audio,
                # retry policy refusal, or turn a cancellation into new work.
                if emitted or isinstance(exc, PermissionError):
                    raise
                if isinstance(exc, VoiceSynthesisError) and exc.code not in (
                    "voice_invalid_audio",
                    "voice_synthesis_failed",
                ):
                    raise
                fallback_binary = await asyncio.to_thread(_resolve_piper_binary, "")
                if not fallback_binary:
                    raise
                logger.warning("Resident Piper failed before audio; retrying with the CLI")
                piper_binary = fallback_binary
            else:
                return
    binary, model, config, sample_rate = await asyncio.to_thread(
        _piper_stream_settings, piper_binary, piper_model, piper_model_config
    )
    cmd = [binary, "-m", model, "-c", config, "--output-raw"]
    scale = validate_length_scale(length_scale)
    if scale != DEFAULT_LENGTH_SCALE:
        cmd += ["--length-scale", str(scale)]
    cleanup: str | None = None
    proc: asyncio.subprocess.Process | None = None
    pipe_tasks: list[asyncio.Task] = []
    try:
        cmd, cleanup = await wrap_argv_async(cmd, mode="standard", _prepare=wrap_argv)
        cmd = await asyncio.to_thread(cgroup_scope_argv, cmd)
        proc = await create_subprocess_limited(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_env({**os.environ, "PYTHONIOENCODING": "utf-8"}),
        )
        assert proc is not None
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        async def feed() -> None:
            assert proc is not None and proc.stdin is not None
            try:
                for phrase in phrases:
                    proc.stdin.write((phrase + "\n").encode("utf-8"))
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # The exit status and stderr reader own the provider failure.
                pass
            finally:
                proc.stdin.close()

        async def drain_errors() -> bytes:
            assert proc is not None and proc.stderr is not None
            tail = b""
            while data := await proc.stderr.read(_PIPER_MAX_STDERR_BYTES):
                tail = (tail + data)[-_PIPER_MAX_STDERR_BYTES:]
            return tail

        writer = asyncio.create_task(feed())
        errors = asyncio.create_task(drain_errors())
        pipe_tasks.extend((writer, errors))
        chunk_bytes = int(sample_rate * _PIPER_CHUNK_SECONDS) * 2
        pending = b""
        total = index = 0
        while data := await proc.stdout.read(chunk_bytes):
            total += len(data)
            if total > _PIPER_MAX_AUDIO_BYTES:
                raise VoiceSynthesisError(
                    "voice_audio_limit", "Synthesized audio exceeds the limit."
                )
            pending += data
            # Pipes may split an Int16 sample at any byte. Preserve that
            # byte for the next read instead of corrupting every sample.
            aligned = len(pending) - len(pending) % 2
            if aligned:
                yield index, sample_rate, pending[:aligned]
                index += 1
                pending = pending[aligned:]
        await writer
        await proc.wait()
        stderr = await errors
        if proc.returncode != 0:
            diagnostic = redact_log_via_context(stderr.decode("utf-8", errors="replace"))
            logger.error("Piper stream failed (rc=%s): %s", proc.returncode, diagnostic)
            raise VoiceSynthesisError(
                "voice_synthesis_failed", "Piper could not synthesize speech."
            )
        if pending or not total:
            raise VoiceSynthesisError(
                "voice_invalid_audio", "Piper returned incomplete or empty audio."
            )
    except SandboxUnavailableError as exc:
        logger.error("voice_reply: Piper TTS refused by the sandbox (%s): %s", exc.kind, exc)
        raise VoiceSynthesisError("voice_sandbox_unavailable", str(exc)) from exc
    finally:

        async def reap() -> None:
            try:
                if proc is not None:
                    if proc.returncode is None:
                        with contextlib.suppress(OSError):
                            proc.kill()
                    for task in pipe_tasks:
                        task.cancel()
                    await asyncio.gather(*pipe_tasks, return_exceptions=True)
                    # Cancel the stderr reader before communicate takes over.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            proc.communicate(), timeout=_PIPER_REAP_TIMEOUT_SECONDS
                        )
            finally:
                if cleanup:
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(os.unlink, cleanup)

        # Keep cleanup owned by the request until it settles. A repeated stop
        # or shutdown cancellation must not abandon its pipes or sandbox file.
        reaping = asyncio.create_task(reap())
        cancelled = False
        while not reaping.done():
            try:
                await asyncio.shield(reaping)
            except asyncio.CancelledError:
                cancelled = True
        reaping.result()
        if cancelled:
            raise asyncio.CancelledError
