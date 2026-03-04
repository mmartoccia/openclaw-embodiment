"""Tests for OpenClawSTTBridge and MicrophoneHal.transcribe() integration."""

from __future__ import annotations

import os
import subprocess
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

from openclaw_embodiment.hal.base import AudioChunk, MicrophoneHal
from openclaw_embodiment.hal.simulator import SimulatedMicrophone
from openclaw_embodiment.transport.stt_bridge import (
    OpenClawSTTBridge,
    STTError,
    STTProvider,
    STTTimeoutError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(
    duration_ms: int = 100,
    sample_rate: int = 16000,
    channels: int = 1,
    fmt: str = "PCM_S16LE",
) -> AudioChunk:
    """Build a minimal AudioChunk for testing."""
    n_bytes = int(sample_rate * channels * 2 * duration_ms / 1000)
    return AudioChunk(
        timestamp_ms=0,
        sample_rate=sample_rate,
        channels=channels,
        format=fmt,
        data=b"\x00" * n_bytes,
        duration_ms=duration_ms,
        timestamp=0.0,
    )


# ---------------------------------------------------------------------------
# 1. AudioChunk dataclass creation and validation
# ---------------------------------------------------------------------------

class TestAudioChunk:
    def test_basic_creation(self):
        chunk = _make_chunk(200)
        assert chunk.sample_rate == 16000
        assert chunk.channels == 1
        assert chunk.format == "PCM_S16LE"
        assert chunk.duration_ms == 200
        assert chunk.timestamp == 0.0

    def test_data_length_matches_duration(self):
        chunk = _make_chunk(100, sample_rate=16000, channels=1)
        expected = int(16000 * 1 * 2 * 0.1)  # 3200 bytes
        assert len(chunk.data) == expected

    def test_frozen_immutability(self):
        chunk = _make_chunk()
        with pytest.raises((TypeError, AttributeError)):
            chunk.sample_rate = 8000  # type: ignore[misc]

    def test_optional_fields_have_defaults(self):
        # Construct without duration_ms / timestamp (backward compat)
        chunk = AudioChunk(
            timestamp_ms=123,
            sample_rate=16000,
            channels=1,
            format="PCM_S16LE",
            data=b"\x00" * 100,
        )
        assert chunk.duration_ms == 0
        assert chunk.timestamp == 0.0


# ---------------------------------------------------------------------------
# 2 & 3. OpenClawSTTBridge -- successful transcription
# ---------------------------------------------------------------------------

class TestOpenClawSTTBridgeSuccess:
    def test_transcribe_returns_string(self):
        bridge = OpenClawSTTBridge(provider=STTProvider.MOCK)
        chunk = _make_chunk(500)
        result = bridge.transcribe(chunk)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mock_provider_includes_language(self):
        bridge = OpenClawSTTBridge(provider=STTProvider.MOCK)
        chunk = _make_chunk(200)
        result = bridge.transcribe(chunk, language="fr")
        assert "lang=fr" in result

    def test_openclaw_provider_subprocess_called(self):
        """Patch subprocess.run and verify CLI is invoked."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "hello world"
        mock_result.stderr = ""

        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW)
        chunk = _make_chunk(100)

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            text = bridge.transcribe(chunk)

        assert text == "hello world"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "openclaw" in call_args[0]
        assert "stt" in call_args
        assert "transcribe" in call_args

    def test_language_flag_passed_for_non_english(self):
        """Verify --language flag is appended for non-English locales."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "bonjour"
        mock_result.stderr = ""

        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW)
        chunk = _make_chunk(100)

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            bridge.transcribe(chunk, language="fr")

        call_args = mock_run.call_args[0][0]
        assert "--language" in call_args
        assert "fr" in call_args

    def test_english_no_language_flag(self):
        """--language flag should NOT be appended for default 'en'."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "hi"
        mock_result.stderr = ""

        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW)
        chunk = _make_chunk(100)

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            bridge.transcribe(chunk, language="en")

        call_args = mock_run.call_args[0][0]
        assert "--language" not in call_args


# ---------------------------------------------------------------------------
# 4. Timeout handling
# ---------------------------------------------------------------------------

class TestOpenClawSTTBridgeTimeout:
    def test_timeout_raises_stt_timeout_error(self):
        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW, timeout_s=0.001)
        chunk = _make_chunk(100)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=0.001)):
            with pytest.raises(STTTimeoutError):
                bridge.transcribe(chunk)

    def test_subprocess_error_raises_stt_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "stt engine crashed"

        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW)
        chunk = _make_chunk(100)

        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(STTError):
                bridge.transcribe(chunk)


# ---------------------------------------------------------------------------
# 5. Temp file cleanup on success and failure
# ---------------------------------------------------------------------------

class TestTempFileCleanup:
    def test_temp_file_removed_on_success(self):
        created_paths: list[str] = []
        original_mkstemp = os.path.abspath  # just to confirm we can patch

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "transcript"
        mock_result.stderr = ""

        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW)
        chunk = _make_chunk(100)

        with patch("subprocess.run", return_value=mock_result):
            with patch("openclaw_embodiment.transport.stt_bridge.os.unlink") as mock_unlink:
                bridge.transcribe(chunk)
                mock_unlink.assert_called_once()
                path_removed = mock_unlink.call_args[0][0]
                assert path_removed.endswith(".wav")

    def test_temp_file_removed_on_failure(self):
        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW)
        chunk = _make_chunk(100)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=30)):
            with patch("openclaw_embodiment.transport.stt_bridge.os.unlink") as mock_unlink:
                with pytest.raises(STTTimeoutError):
                    bridge.transcribe(chunk)
                mock_unlink.assert_called_once()


# ---------------------------------------------------------------------------
# 6. Mock HAL transcribe() integration
# ---------------------------------------------------------------------------

class TestSimulatedMicrophoneTranscribe:
    def test_sim_transcribe_returns_string(self):
        mic = SimulatedMicrophone()
        mic.initialize()
        chunk = mic.get_buffer(200)
        result = mic.transcribe(chunk)
        assert isinstance(result, str)
        assert "SIM_TRANSCRIPT" in result

    def test_sim_transcribe_respects_language(self):
        mic = SimulatedMicrophone()
        mic.initialize()
        chunk = mic.get_buffer(100)
        result = mic.transcribe(chunk, language="es")
        assert "lang=es" in result

    def test_sim_microphone_is_microphone_hal(self):
        assert isinstance(SimulatedMicrophone(), MicrophoneHal)


# ---------------------------------------------------------------------------
# 7. transcribe_stream() yields partial results
# ---------------------------------------------------------------------------

class TestTranscribeStream:
    def test_sim_transcribe_stream_yields_partials(self):
        mic = SimulatedMicrophone()
        mic.initialize()

        def _chunks() -> Iterator[AudioChunk]:
            for _ in range(3):
                yield mic.get_buffer(100)

        results = list(mic.transcribe_stream(_chunks()))
        assert len(results) == 3
        for r in results:
            assert isinstance(r, str)
            assert "SIM_PARTIAL" in r

    def test_openclaw_bridge_transcribe_stream_mock(self):
        """OpenClawSTTBridge.transcribe() in MOCK mode works in a streaming loop."""
        bridge = OpenClawSTTBridge(provider=STTProvider.MOCK)

        chunks = [_make_chunk(100) for _ in range(4)]
        # Simulate stream by calling transcribe per chunk
        results = [bridge.transcribe(c) for c in chunks]
        assert len(results) == 4
        assert all("MOCK_TRANSCRIPT" in r for r in results)
