"""Operator-configured batch speech recognition, without a second resident model."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile

from kiro_crew import stt

logger = logging.getLogger("kiro_crew.transcribe")

CODE_BRIDGE_UNAVAILABLE = "stt_bridge_unavailable"
_BRIDGE_ENV = "KIROCREW_STT_BRIDGE"


def _bridge_command() -> str | None:
    """Return the operator-owned bridge executable when it can be run."""
    command = os.environ.get(_BRIDGE_ENV, "").strip()
    if not command or not os.path.isabs(command):
        return None
    try:
        mode = os.stat(command).st_mode
    except OSError:
        return None
    if not stat.S_ISREG(mode) or not os.access(command, os.X_OK):
        return None
    return command


def _bridge_availability() -> stt.Availability:
    if _bridge_command() is None:
        return stt.Availability(
            False,
            CODE_BRIDGE_UNAVAILABLE,
            f"speech bridge is not configured through {_BRIDGE_ENV}",
        )
    return stt.Availability(True)


def _read_bridge_transcript(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read().strip() or None
    except OSError:
        return None


async def _transcribe_bridge(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Invoke the operator's batch STT bridge and collect its text file."""
    command = await asyncio.to_thread(_bridge_command)
    if command is None:
        logger.error("speech bridge unavailable: %s is not executable", _BRIDGE_ENV)
        return None
    out_dir = await asyncio.to_thread(tempfile.mkdtemp)
    try:
        proc = await asyncio.create_subprocess_exec(
            command,
            audio_path,
            "--output_dir",
            out_dir,
            "--output_format",
            "txt",
            "--language",
            stt_config.language_code or "en-US",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=stt_config.timeout_secs
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.error("speech bridge timed out after %ds", stt_config.timeout_secs)
            return None
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[-500:] if stderr else ""
            logger.error("speech bridge exited %s: %s", proc.returncode, detail or "(no stderr)")
            return None
        base = os.path.splitext(os.path.basename(audio_path))[0]
        return await asyncio.to_thread(
            _read_bridge_transcript, os.path.join(out_dir, f"{base}.txt")
        )
    finally:
        await asyncio.to_thread(shutil.rmtree, out_dir, ignore_errors=True)
