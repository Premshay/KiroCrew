"""Voice synthesis endpoints — TTS config and streaming synthesis.

TTS synthesis is optional and routed through ``voice_reply`` (which lazily
imports any cloud TTS backend only when invoked). The endpoints below stay
importable on a vanilla machine; synthesis simply errors gracefully if no
backend is configured.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import shutil
import secrets
import tempfile
import time
from dataclasses import dataclass

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.config.loader import config_path
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.slack.handler import _vc
from kiro_crew.voice_reply import (
    DEFAULT_POCKET_VOICE,
    PROVIDER_PIPER,
    PROVIDER_POLLY,
    PROVIDER_POCKET,
    VALID_ENGINES,
    VALID_PROVIDERS,
    resolve_polly_cli,
    stitch_mp3s,
    stream_pocket_speech,
    streaming_voice_reply,
    synthesize_speech,
    validate_length_scale,
)

logger = logging.getLogger(__name__)

_REPLAY_JOB_TTL_SECS = 120
_MAX_REPLAY_JOBS = 32


@dataclass(frozen=True)
class _PocketReplayJob:
    text: str
    voice_id: str
    piper_binary: str
    piper_model: str
    piper_model_config: str
    length_scale: float
    expires_at: float


_pocket_replay_jobs: dict[str, _PocketReplayJob] = {}


def _purge_expired_pocket_replay_jobs(now: float) -> None:
    for candidate, job in list(_pocket_replay_jobs.items()):
        if job.expires_at <= now:
            _pocket_replay_jobs.pop(candidate, None)


def _take_pocket_replay_job(job_id: str) -> _PocketReplayJob | None:
    now = time.monotonic()
    _purge_expired_pocket_replay_jobs(now)
    job = _pocket_replay_jobs.pop(job_id, None)
    return job if job and job.expires_at > now else None


async def api_voice_config(request: web.Request) -> web.Response:
    """GET/PUT /api/voice/config — read or update voice settings."""
    if request.method == "GET":
        return web.json_response(
            {
                "enabled": _vc.global_enabled,
                "provider": _vc.provider,
                "voice": _vc.default_voice,
                "engine": _vc.default_engine,
                "rate": _vc.default_rate,
                "pitch": _vc.default_pitch,
                "autoSpeak": _vc.global_enabled,
                "aws_profile": _vc.aws_profile,
                "region": _vc.region,
                "piper_binary": _vc.piper_binary,
                "piper_model": _vc.piper_model,
                "piper_model_config": _vc.piper_model_config,
                "piper_length_scale": _vc.piper_length_scale,
            }
        )

    # PUT — update and persist
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    # Update in-memory
    # ``in VALID_PROVIDERS`` would raise TypeError on an unhashable JSON value
    # (list/dict), 500ing the PUT — require a str first.
    selected_provider = None
    if "provider" in body and isinstance(body["provider"], str) and body["provider"] in VALID_PROVIDERS:
        selected_provider = body["provider"]
        _vc.provider = selected_provider
        if selected_provider == PROVIDER_POCKET and "voice" not in body:
            _vc.default_voice = DEFAULT_POCKET_VOICE
    if "voice" in body:
        _vc.default_voice = str(body["voice"])
    # Same as provider above: ``in VALID_ENGINES`` raises TypeError on an
    # unhashable JSON value (list/dict), 500ing the PUT — require a str first.
    if "engine" in body and isinstance(body["engine"], str) and body["engine"] in VALID_ENGINES:
        _vc.default_engine = body["engine"]
    if "rate" in body:
        _vc.default_rate = str(body["rate"])
    if "pitch" in body:
        _vc.default_pitch = str(body["pitch"])
    if "enabled" in body:
        _vc.global_enabled = bool(body["enabled"])
    if "autoSpeak" in body:
        _vc.global_enabled = bool(body["autoSpeak"])
    if "aws_profile" in body:
        _vc.aws_profile = str(body["aws_profile"]).strip()
    if "region" in body:
        _vc.region = str(body["region"]).strip()
    if "piper_binary" in body:
        _vc.piper_binary = str(body["piper_binary"]).strip()
    if "piper_model" in body:
        _vc.piper_model = str(body["piper_model"]).strip()
    if "piper_model_config" in body:
        _vc.piper_model_config = str(body["piper_model_config"]).strip()
    if "piper_length_scale" in body:
        # Coerce to finite/positive via the shared validator (rejects non-numeric,
        # inf/NaN, and <=0) so a bad value can't reach synthesis or be persisted
        # as unserializable JSON that breaks the browser's config GET.
        _vc.piper_length_scale = validate_length_scale(body["piper_length_scale"])

    # Persist to config.json. MERGE into the existing voice_reply block rather
    # than rewriting it wholesale — the loader (slack/handler.py) also reads
    # auto_speak / auto_reply_to_voice from here, and a wholesale rewrite would
    # silently drop any key not in this handler's set.
    try:
        cfg_path = config_path()
        with open(cfg_path) as f:
            cfg = json.load(f)
        vr = cfg.get("voice_reply")
        if not isinstance(vr, dict):
            vr = {}
        vr.update(
            {
                "enabled": _vc.global_enabled,
                "provider": _vc.provider,
                "voice_id": _vc.default_voice,
                "engine": _vc.default_engine,
                "rate": _vc.default_rate,
                "pitch": _vc.default_pitch,
                "aws_profile": _vc.aws_profile,
                "region": _vc.region,
                "piper_binary": _vc.piper_binary,
                "piper_model": _vc.piper_model,
                "piper_model_config": _vc.piper_model_config,
                "piper_length_scale": _vc.piper_length_scale,
            }
        )
        cfg["voice_reply"] = vr
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        logger.exception("Failed to persist voice config")

    return web.json_response({"ok": True})


async def api_voice_synthesize(request: web.Request) -> web.Response:
    """POST /api/voice/synthesize — sentence-chunked TTS.

    Synthesizes each sentence in parallel, broadcasts ``voice_chunk``
    WS events with base64 MP3 data for immediate playback, then stitches
    all chunks into a single MP3 and broadcasts ``voice_complete``.
    """

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    text = body.get("text", "").strip()
    slot_key = body.get("slot", "")
    if not text:
        return web.json_response({"error": "text required"}, status=400)

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)

    # Voice config — use defaults from handler config or body overrides
    voice_id = body.get("voice", _vc.default_voice)
    engine = body.get("engine", _vc.default_engine)
    rate = body.get("rate", _vc.default_rate)
    pitch = body.get("pitch", _vc.default_pitch)

    # Piper produces a single local WAV (not sentence-chunked SSML like Polly).
    # streaming_voice_reply is Polly-only, so route the selected provider through
    # the provider-aware synthesize_speech and emit one chunk + complete —
    # otherwise the DEFAULT (Piper) provider would yield no dashboard audio.
    if _vc.provider in {PROVIDER_PIPER, PROVIDER_POCKET}:
        return await _synthesize_nonstreaming(state, text, slot_key)

    chunk_paths: list[str] = []
    final_path: str | None = None
    try:
        async for idx, sentence, mp3_bytes in streaming_voice_reply(
            text,
            voice_id=voice_id,
            engine=engine,
            rate=rate,
            pitch=pitch,
            aws_profile=_vc.aws_profile,
            region=_vc.region,
        ):
            # Save chunk for stitching

            fd, chunk_path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            with open(chunk_path, "wb") as f:
                f.write(mp3_bytes)
            chunk_paths.append(chunk_path)

            # Broadcast to dashboard for immediate playback
            state.broadcast_ws(
                "voice_chunk",
                {
                    "slot": slot_key,
                    "index": idx,
                    "sentence": sentence,
                    "audio": base64.b64encode(mp3_bytes).decode(),
                },
            )

        # Stitch all chunks into single MP3
        if chunk_paths:
            final_path = await stitch_mp3s(chunk_paths)
            if final_path:
                with open(final_path, "rb") as f:
                    final_bytes = f.read()
                state.broadcast_ws(
                    "voice_complete",
                    {
                        "slot": slot_key,
                        "audio": base64.b64encode(final_bytes).decode(),
                        "chunks": len(chunk_paths),
                    },
                )

        return web.json_response({"ok": True, "chunks": len(chunk_paths)})
    except Exception as exc:
        logger.exception("Voice synthesis failed")
        err_msg, _ = redact_exfiltration_urls(str(exc))
        err_msg, _ = redact_credentials(err_msg)
        state.broadcast_ws(
            "voice_error",
            {"slot": slot_key, "error": err_msg},
        )
        return web.json_response(
            {"ok": False, "error": err_msg}, status=500
        )
    finally:
        if final_path:
            with contextlib.suppress(OSError):
                os.unlink(final_path)
        for p in chunk_paths:
            with contextlib.suppress(OSError):
                os.unlink(p)


async def _synthesize_nonstreaming(
    state: DashboardState, text: str, slot_key: str
) -> web.Response:
    """Synthesize one clip via the provider-aware ``synthesize_speech`` and emit
    it as a single ``voice_chunk`` + ``voice_complete``.

    Used for providers (Piper) that produce a single local file rather than the
    sentence-chunked Polly SSML stream. The audio is delivered whole; the
    dashboard player already handles a single-chunk reply.
    """
    audio_path: str | None = None
    try:
        audio_path = await synthesize_speech(
            text,
            provider=_vc.provider,
            piper_binary=_vc.piper_binary,
            piper_model=_vc.piper_model,
            piper_model_config=_vc.piper_model_config,
            length_scale=_vc.piper_length_scale,
        )
        if not audio_path:
            msg = "Piper TTS unavailable — check the piper binary and model path in Voice settings."
            state.broadcast_ws("voice_error", {"slot": slot_key, "error": msg})
            return web.json_response({"ok": False, "error": msg}, status=502)
        with open(audio_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode()
        audio_mime = "audio/wav" if audio_path.lower().endswith(".wav") else "audio/mpeg"
        state.broadcast_ws(
            "voice_chunk",
            {
                "slot": slot_key,
                "index": 0,
                "sentence": text,
                "audio": audio_b64,
                "audioMime": audio_mime,
            },
        )
        state.broadcast_ws(
            "voice_complete",
            {"slot": slot_key, "audio": audio_b64, "chunks": 1, "audioMime": audio_mime},
        )
        return web.json_response({"ok": True, "chunks": 1})
    except Exception as exc:
        logger.exception("Piper voice synthesis failed")
        err_msg, _ = redact_exfiltration_urls(str(exc))
        err_msg, _ = redact_credentials(err_msg)
        state.broadcast_ws("voice_error", {"slot": slot_key, "error": err_msg})
        return web.json_response({"ok": False, "error": err_msg}, status=500)
    finally:
        if audio_path:
            with contextlib.suppress(OSError):
                os.unlink(audio_path)


async def api_voice_replay(request: web.Request) -> web.Response:
    """Create a one-time Pocket replay URL, or select the legacy path."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response(
            {"code": "voice_replay_invalid_json", "error": "invalid JSON"}, status=400
        )
    if not isinstance(body, dict):
        return web.json_response(
            {"code": "voice_replay_invalid_json", "error": "invalid JSON"}, status=400
        )
    text = str(body.get("text") or "").strip()
    if not text:
        return web.json_response(
            {"code": "voice_replay_text_required", "error": "text required"}, status=400
        )
    if _vc.provider != PROVIDER_POCKET:
        return web.json_response({"mode": "legacy"})

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    now = time.monotonic()
    _purge_expired_pocket_replay_jobs(now)
    if len(_pocket_replay_jobs) >= _MAX_REPLAY_JOBS:
        oldest = min(_pocket_replay_jobs, key=lambda key: _pocket_replay_jobs[key].expires_at)
        _pocket_replay_jobs.pop(oldest, None)
    job_id = secrets.token_urlsafe(24)
    _pocket_replay_jobs[job_id] = _PocketReplayJob(
        text=text,
        voice_id=_vc.default_voice,
        piper_binary=_vc.piper_binary,
        piper_model=_vc.piper_model,
        piper_model_config=_vc.piper_model_config,
        length_scale=_vc.piper_length_scale,
        expires_at=now + _REPLAY_JOB_TTL_SECS,
    )
    return web.json_response(
        {"mode": "stream", "mime": "audio/ogg; codecs=opus", "url": f"/api/voice/replay/{job_id}"}
    )


async def api_voice_replay_stream(request: web.Request) -> web.StreamResponse:
    """Stream a one-time Pocket replay URL as browser-playable Ogg Opus."""
    job = _take_pocket_replay_job(request.match_info["job_id"])
    if job is None:
        return web.json_response(
            {"code": "voice_replay_missing", "error": "voice replay is no longer available"},
            status=404,
        )
    stream = stream_pocket_speech(
        job.text,
        voice_id=job.voice_id,
        piper_binary=job.piper_binary,
        piper_model=job.piper_model,
        piper_model_config=job.piper_model_config,
        length_scale=job.length_scale,
    )
    try:
        first_chunk = await anext(stream)
    except StopAsyncIteration:
        with contextlib.suppress(Exception):
            await stream.aclose()
        return web.json_response(
            {"code": "voice_replay_empty", "error": "voice replay produced no audio"}, status=502
        )
    except Exception:
        with contextlib.suppress(Exception):
            await stream.aclose()
        logger.exception("Pocket replay could not start")
        return web.json_response(
            {"code": "voice_replay_unavailable", "error": "voice replay is unavailable"}, status=502
        )

    response = web.StreamResponse(
        status=200,
        headers={"Content-Type": "audio/ogg; codecs=opus", "Cache-Control": "no-store"},
    )
    await response.prepare(request)
    try:
        await response.write(first_chunk)
        async for chunk in stream:
            await response.write(chunk)
        await response.write_eof()
    except ConnectionError:
        logger.debug("Pocket replay client disconnected")
    except Exception:
        logger.exception("Pocket replay stream failed after response started")
    finally:
        with contextlib.suppress(Exception):
            await stream.aclose()
    return response


# ── Voices list (cached) ──

_voices_cache: list[dict] | None = None
_voices_cache_ts: float = 0.0
_VOICES_CACHE_TTL = 3600  # 1 hour


async def api_voice_voices(request: web.Request) -> web.Response:
    """GET /api/voice/voices — list available Polly voices (cached 1h)."""
    global _voices_cache, _voices_cache_ts

    now = time.time()
    if _voices_cache is not None and (now - _voices_cache_ts) < _VOICES_CACHE_TTL:
        return web.json_response({"voices": _voices_cache})

    # The catalogue lives behind a paid provider, so two gates come before the
    # subprocess. Both used to be absent here: the ONLY thing stopping this
    # endpoint from calling AWS was the frontend declining to fetch it while
    # Piper was selected, so any other client — or a direct request — reached
    # `aws polly describe-voices` against whatever the ambient credential chain
    # resolved to.
    #
    # 1. Not the active provider: a Piper user has no business shipping a
    #    request to Polly at all.
    if _vc.provider != PROVIDER_POLLY:
        return web.json_response({"voices": []})
    # 2. Polly IS selected but unconfirmed. Same empty list: the operator-facing
    #    explanation is the consent card's job (it has its own GET carrying the
    #    reason), so returning a second copy here would be a response field with
    #    no reader. Routed through ``refuse_and_log`` rather than ``authorize``
    #    so the denial reaches the tamper-evident audit log like every other
    #    gated call site -- a denial that only logs is a denial an incident
    #    review cannot see.
    if not await aws_consent.refuse_and_log(
        aws_consent.SERVICE_POLLY, profile=_vc.aws_profile, region=_vc.region
    ):
        return web.json_response({"voices": []})

    aws_bin = await asyncio.to_thread(resolve_polly_cli)
    if aws_bin is None:
        # Polly voice listing needs the AWS CLI, which is optional (the
        # default Piper provider works without it). Resolution goes through
        # the deploy engine's shared well-known-dirs resolver, so a gateway
        # running under launchd with a minimal PATH still finds a Homebrew /
        # official-pkg install (#4770). When the CLI genuinely is not
        # installed, degrade to an empty list instead of a 500 + traceback.
        # Not cached, so the list recovers as soon as `aws` becomes
        # resolvable. The probe runs in a thread so a wedged network mount
        # on PATH cannot stall the event loop.
        logger.info("aws CLI not resolvable — returning empty voices list")
        return web.json_response({"voices": []})

    cmd = [aws_bin, "polly", "describe-voices", "--output", "json"]
    if _vc.aws_profile:
        cmd += ["--profile", _vc.aws_profile]
    if _vc.region:
        cmd += ["--region", _vc.region]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.error("describe-voices failed: %s", err)
            return web.json_response({"error": "Failed to retrieve voices"}, status=502)

        data = json.loads(stdout)
        voices = [
            {
                "id": v["Id"],
                "name": v["Name"],
                "language": v["LanguageName"],
                "languageCode": v["LanguageCode"],
                "gender": v["Gender"],
                "engines": v["SupportedEngines"],
            }
            for v in data.get("Voices", [])
        ]
        voices.sort(key=lambda v: (v["languageCode"], v["name"]))
        _voices_cache = voices
        _voices_cache_ts = now
        return web.json_response({"voices": voices})
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # Reap via communicate(), not wait(): wait_for cancelled the pipe
        # readers before the kill landed, so a child blocked writing to a
        # full stderr PIPE is never drained and wait() can hang the request
        # handler indefinitely (#5975, same class as #5834).
        await proc.communicate()
        return web.json_response({"error": "timeout"}, status=504)
    except FileNotFoundError:
        # Defense-in-depth behind the which() guard above: exec can still
        # fail with ENOENT — the binary was removed between the check and
        # the spawn, or `aws` is a script whose interpreter is missing.
        # Same graceful degrade as the guard, with the exception logged so
        # the non-PATH causes stay diagnosable.
        logger.info(
            "aws CLI could not be executed — returning empty voices list",
            exc_info=True,
        )
        return web.json_response({"voices": []})
    except Exception:
        logger.exception("describe-voices error")
        return web.json_response({"error": "Failed to retrieve voices"}, status=500)
