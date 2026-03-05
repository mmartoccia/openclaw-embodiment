"""OpenClaw STT bridge -- delegates transcription to `openclaw stt transcribe`.

Uses the OpenClaw native transcription runtime (api.runtime.stt.transcribeAudioFile)
via subprocess so any device with openclaw installed can transcribe audio without
a separate cloud API key or local model.

Usage::

    from openclaw_embodiment.transport.stt_bridge import OpenClawSTTBridge, STTProvider
    from openclaw_embodiment.hal.base import AudioChunk

    bridge = OpenClawSTTBridge()
    text = bridge.transcribe(audio_chunk)
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
import time
import wave
from enum import Enum
from typing import Optional

from ..hal.base import AudioChunk

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 30


class STTProvider(Enum):
    """STT backend selection."""

    OPENCLAW = "openclaw"
    WHISPER_LOCAL = "whisper_local"
    MOCK = "mock"


class STTError(RuntimeError):
    """Raised when transcription fails."""


class STTTimeoutError(STTError):
    """Raised when transcription exceeds the configured timeout."""


class OpenClawSTTBridge:
    """Transcribe audio via the OpenClaw CLI STT runtime.

    Writes audio to a temporary WAV file, invokes `openclaw stt transcribe <path>`,
    captures stdout as the transcript, and removes the temp file unconditionally.

    Args:
        provider:   Which backend to use (default OPENCLAW).
        timeout_s:  Subprocess timeout in seconds (default 30).
        openclaw_bin: Path to the openclaw binary (default "openclaw").
    """

    def __init__(
        self,
        provider: STTProvider = STTProvider.OPENCLAW,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        openclaw_bin: str = "openclaw",
    ) -> None:
        self._provider = provider
        self._timeout_s = timeout_s
        self._openclaw_bin = openclaw_bin

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _audio_to_wav_bytes(self, audio: AudioChunk) -> bytes:
        """Convert an AudioChunk to WAV bytes regardless of original format."""
        import io

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(audio.channels)
            wf.setsampwidth(2)  # 16-bit PCM
            wf.setframerate(audio.sample_rate)
            wf.writeframes(audio.data)
        return buf.getvalue()

    def _write_temp_audio(self, audio: AudioChunk) -> str:
        """Write audio to a temp file and return its path."""
        suffix = ".wav" if audio.format.upper() in ("PCM_S16LE", "PCM16", "PCM", "WAV") else f".{audio.format.lower()}"
        fd, path = tempfile.mkstemp(suffix=suffix, prefix="openclaw_stt_")
        try:
            if suffix == ".wav":
                os.write(fd, self._audio_to_wav_bytes(audio))
            else:
                os.write(fd, audio.data)
        finally:
            os.close(fd)
        return path

    def _run_openclaw_stt(self, path: str, language: str) -> str:
        """Invoke openclaw stt transcribe and return the transcript text."""
        cmd = [self._openclaw_bin, "stt", "transcribe", path]
        if language and language != "en":
            cmd += ["--language", language]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise STTTimeoutError(
                f"openclaw stt transcribe timed out after {self._timeout_s}s"
            ) from exc
        except FileNotFoundError as exc:
            raise STTError(
                f"openclaw binary not found at '{self._openclaw_bin}'. "
                "Ensure openclaw is installed and on PATH."
            ) from exc

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise STTError(
                f"openclaw stt transcribe exited {result.returncode}: {stderr}"
            )

        text = result.stdout.strip()
        if not text:
            logger.warning("openclaw stt transcribe returned empty result for %s", path)
        return text

    def _mock_transcribe(self, audio: AudioChunk, language: str) -> str:
        """Return a deterministic mock transcript for testing."""
        duration_s = len(audio.data) / max(1, audio.sample_rate * audio.channels * 2)
        return f"[MOCK_TRANSCRIPT lang={language} duration={duration_s:.2f}s]"

    # ------------------------------------------------------------------
    # Public sync API
    # ------------------------------------------------------------------

    def transcribe(self, audio: AudioChunk, language: str = "en") -> str:
        """Transcribe audio to text.

        Writes to temp file, calls openclaw CLI, returns transcript string.
        Temp file is always removed (success or failure).

        Args:
            audio:    AudioChunk to transcribe.
            language: BCP-47 language code (default "en").

        Returns:
            Transcribed text string (may be empty if no speech detected).

        Raises:
            STTError:        Subprocess failure or binary not found.
            STTTimeoutError: Transcription exceeded timeout.
        """
        if self._provider == STTProvider.MOCK:
            return self._mock_transcribe(audio, language)

        path = self._write_temp_audio(audio)
        try:
            return self._run_openclaw_stt(path, language)
        finally:
            try:
                os.unlink(path)
            except OSError:
                logger.debug("Failed to remove temp audio file: %s", path)

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def transcribe_async(self, audio: AudioChunk, language: str = "en") -> str:
        """Async variant of transcribe() using asyncio.create_subprocess_exec.

        Args:
            audio:    AudioChunk to transcribe.
            language: BCP-47 language code.

        Returns:
            Transcribed text string.

        Raises:
            STTError:        Subprocess failure.
            STTTimeoutError: Transcription exceeded timeout.
        """
        if self._provider == STTProvider.MOCK:
            return self._mock_transcribe(audio, language)

        path = self._write_temp_audio(audio)
        try:
            cmd = [self._openclaw_bin, "stt", "transcribe", path]
            if language and language != "en":
                cmd += ["--language", language]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout_s
                )
            except asyncio.TimeoutError as exc:
                try:
                    proc.kill()
                except Exception:  # grain: ignore NAKED_EXCEPT -- STT transcription -- service errors must not crash the audio pipeline
                    pass
                raise STTTimeoutError(
                    f"openclaw stt transcribe (async) timed out after {self._timeout_s}s"
                ) from exc

            if proc.returncode != 0:
                stderr = stderr_b.decode(errors="replace").strip()
                raise STTError(
                    f"openclaw stt transcribe exited {proc.returncode}: {stderr}"
                )

            text = stdout_b.decode(errors="replace").strip()
            if not text:
                logger.warning("openclaw stt transcribe returned empty result (async)")
            return text
        finally:
            try:
                os.unlink(path)
            except OSError:
                logger.debug("Failed to remove temp audio file (async): %s", path)
