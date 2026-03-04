"""Tests for the Context Engine v0.3 components.

Covers:
- ContextBuilder with mock HALs
- AudioTriggerDetector state machine
- ProximityContext assembly
- SensorContext schema validation
"""

from __future__ import annotations

import math
import struct
import threading
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from openclaw_embodiment.core.context_builder import (
    AudioContext,
    ContextBuilder,
    DeviceCapabilityVector,
    DISTILLER_CM5_CAPABILITIES,
    ProximityContext,
    SensorContext,
    VisualContext,
)
from openclaw_embodiment.triggers.audio_trigger import (
    AudioTriggerConfig,
    AudioTriggerDetector,
    AudioTriggerState,
)
from openclaw_embodiment.hal.base import AudioChunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pcm(rms_target: float, num_samples: int = 48000) -> bytes:
    """Generate synthetic S16_LE PCM at approximately the given RMS level."""
    # Simple sine wave scaled to target RMS
    import math as _m
    amplitude = rms_target * _m.sqrt(2)
    amplitude = min(amplitude, 32767)
    samples = [int(amplitude * _m.sin(2 * _m.pi * 440 * i / 48000)) for i in range(num_samples)]
    return struct.pack(f"<{num_samples}h", *samples)


def make_audio_chunk(rms: float = 1000.0) -> AudioChunk:
    return AudioChunk(
        data=make_pcm(rms),
        sample_rate=48000,
        channels=2,
        format="pcm_16",
        duration_ms=1000,
        timestamp_ms=int(time.time() * 1000),
    )


class MockMicHAL:
    """Mic HAL that returns a synthetic audio chunk."""
    def __init__(self, rms: float = 1000.0):
        self.rms = rms

    def capture(self, duration_ms: int = 2000) -> AudioChunk:
        return make_audio_chunk(self.rms)


class MockCameraHAL:
    """Camera HAL that returns a synthetic grayscale JPEG."""
    def __init__(self, lighting: str = "bright", person_count: Optional[int] = 1,
                 color_reliable: bool = False):
        self.lighting = lighting
        self._person_count = person_count
        self.color_reliable = color_reliable

    def capture_grayscale(self) -> bytes:
        # Minimal valid JPEG stub
        from PIL import Image
        import io
        img = Image.new("L", (64, 64), color=200)
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    def capture(self) -> bytes:
        return self.capture_grayscale()

    def get_lighting_level(self) -> str:
        return self.lighting

    def estimate_person_count(self) -> Optional[int]:
        return self._person_count


class MockBLEScanner:
    """BLE scanner that returns a fixed ProximityContext."""
    def __init__(self, known=None, unknown=0, rssi_map=None):
        self._known = known or []
        self._unknown = unknown
        self._rssi_map = rssi_map or {}

    def scan(self):
        from openclaw_embodiment.hal.ble_scanner import ProximityContext as BLECtx
        return BLECtx(
            known_devices=self._known,
            unknown_count=self._unknown,
            rssi_map=self._rssi_map,
            confidence=0.8,
            timestamp_ms=int(time.time() * 1000),
        )


# ---------------------------------------------------------------------------
# ContextBuilder tests
# ---------------------------------------------------------------------------

class TestContextBuilder:

    def test_build_with_all_hals(self):
        builder = ContextBuilder(
            device_id="test-device",
            mic_hal=MockMicHAL(rms=1500.0),
            camera_hal=MockCameraHAL(lighting="bright", person_count=1),
            ble_scanner=MockBLEScanner(known=["Pendant"], unknown=2),
        )
        ctx = builder.build(trigger="manual")

        assert isinstance(ctx, SensorContext)
        assert ctx.device_id == "test-device"
        assert ctx.trigger == "manual"
        assert ctx.audio is not None
        assert ctx.visual is not None
        assert ctx.proximity is not None
        assert ctx.motion is None  # No IMU on Distiller
        assert 0.0 <= ctx.awareness_level <= 1.0
        assert isinstance(ctx.conflicts, list)
        assert len(ctx.summary) > 0

    def test_build_no_hals(self):
        builder = ContextBuilder(device_id="bare-device")
        ctx = builder.build(trigger="scheduled")

        assert ctx.audio is None
        assert ctx.visual is None
        assert ctx.proximity is None
        assert ctx.motion is None
        assert ctx.awareness_level == 0.0
        assert "Trigger: scheduled" in ctx.summary

    def test_build_mic_only(self):
        builder = ContextBuilder(
            device_id="mic-only",
            mic_hal=MockMicHAL(rms=2000.0),
        )
        ctx = builder.build(trigger="voice_detected")
        assert ctx.audio is not None
        assert ctx.audio.rms_level > 0
        assert ctx.visual is None
        assert ctx.proximity is None
        assert ctx.awareness_level > 0.0

    def test_conflict_detection_speaker_vs_person(self):
        """Conflicting speaker_count vs person_count should reduce awareness."""
        builder = ContextBuilder(
            device_id="conflict-test",
            mic_hal=MockMicHAL(),
            camera_hal=MockCameraHAL(person_count=1),
        )
        # Manually inject a speaker_count to trigger the conflict
        ctx = builder.build(trigger="manual")
        # Force a conflict scenario by patching
        audio_ctx = AudioContext(
            speaker_count=3, ambient_class="speech", rms_level=2000.0, confidence=0.9
        )
        visual_ctx = VisualContext(
            person_count=1, lighting="bright", confidence=0.7
        )
        conflicts = builder._detect_conflicts(audio_ctx, visual_ctx)
        assert len(conflicts) == 1
        assert "speaker_count:audio(3)" in conflicts[0]
        assert "person_count:visual(1)" in conflicts[0]

    def test_no_conflict_matching_counts(self):
        builder = ContextBuilder(device_id="no-conflict")
        audio_ctx = AudioContext(speaker_count=2, ambient_class="speech", rms_level=1000.0, confidence=0.8)
        visual_ctx = VisualContext(person_count=2, lighting="bright", confidence=0.7)
        conflicts = builder._detect_conflicts(audio_ctx, visual_ctx)
        assert conflicts == []

    def test_awareness_penalized_by_conflict(self):
        builder = ContextBuilder(device_id="penalty-test")
        audio_ctx = AudioContext(ambient_class="speech", rms_level=1000.0, confidence=0.8)
        visual_ctx = VisualContext(lighting="bright", confidence=0.7)
        proximity_ctx = ProximityContext(confidence=0.6)

        no_conflict = builder._compute_awareness(audio_ctx, visual_ctx, proximity_ctx, [])
        with_conflict = builder._compute_awareness(
            audio_ctx, visual_ctx, proximity_ctx, ["conflict1"]
        )
        assert with_conflict < no_conflict
        assert with_conflict == pytest.approx(no_conflict - 0.15, abs=0.01)

    def test_awareness_zero_no_sensors(self):
        builder = ContextBuilder(device_id="empty")
        assert builder._compute_awareness(None, None, None, []) == 0.0

    def test_distiller_capabilities(self):
        caps = DISTILLER_CM5_CAPABILITIES
        assert caps.has_microphone is True
        assert caps.has_camera is True
        assert caps.has_imu is False
        assert caps.has_ble is True
        assert caps.has_display is True
        assert caps.has_speaker is True
        assert caps.is_wearable is False
        assert caps.has_gps is False

    def test_summary_includes_trigger(self):
        builder = ContextBuilder(device_id="summary-test")
        ctx = builder.build(trigger="ble_new_device")
        assert "ble_new_device" in ctx.summary

    def test_ambient_classification(self):
        assert ContextBuilder._classify_ambient(50.0) == "silence"
        assert ContextBuilder._classify_ambient(500.0) == "noise"
        assert ContextBuilder._classify_ambient(2000.0) == "speech"
        assert ContextBuilder._classify_ambient(5000.0) == "loud"

    def test_rms_computation(self):
        # Silence should be near-zero
        silent = struct.pack("<4h", 0, 0, 0, 0)
        assert ContextBuilder._compute_rms(silent) == 0.0
        # Known signal
        known = make_pcm(rms_target=1000.0, num_samples=4800)
        rms = ContextBuilder._compute_rms(known)
        assert 800 < rms < 1200  # Allow some tolerance for sine wave math


# ---------------------------------------------------------------------------
# AudioTriggerDetector state machine tests
# ---------------------------------------------------------------------------

class TestAudioTriggerStateMachine:

    def _make_chunk(self) -> AudioChunk:
        return make_audio_chunk(rms=1000.0)

    def test_initial_state(self):
        detector = AudioTriggerDetector(on_trigger=lambda c: None)
        assert detector.state == AudioTriggerState.IDLE

    def test_transition_idle_to_detecting(self):
        """High RMS should move IDLE -> DETECTING."""
        config = AudioTriggerConfig(threshold_rms=500.0, min_duration_ms=300)
        detector = AudioTriggerDetector(on_trigger=lambda c: None, config=config)
        chunk = self._make_chunk()
        # RMS=1000 > threshold=500
        detector._transition(1000.0, chunk)
        assert detector.state == AudioTriggerState.DETECTING

    def test_detecting_drops_below_threshold(self):
        """Low RMS during DETECTING should return to IDLE."""
        config = AudioTriggerConfig(threshold_rms=500.0, min_duration_ms=300)
        detector = AudioTriggerDetector(on_trigger=lambda c: None, config=config)
        chunk = self._make_chunk()
        detector._transition(1000.0, chunk)  # -> DETECTING
        assert detector.state == AudioTriggerState.DETECTING
        detector._transition(100.0, chunk)   # drops below threshold -> IDLE
        assert detector.state == AudioTriggerState.IDLE

    def test_detecting_to_triggered_after_min_duration(self):
        """DETECTING + min_duration met -> TRIGGERED -> fires callback -> COOLDOWN."""
        triggered_chunks = []
        config = AudioTriggerConfig(
            threshold_rms=500.0, min_duration_ms=0, cooldown_ms=2000
        )
        detector = AudioTriggerDetector(
            on_trigger=lambda c: triggered_chunks.append(c),
            config=config,
        )
        chunk = self._make_chunk()
        detector._transition(1000.0, chunk)  # IDLE -> DETECTING
        detector._transition(1000.0, chunk)  # DETECTING: min_duration=0 -> fires immediately

        # Give callback thread time to run
        time.sleep(0.1)
        assert len(triggered_chunks) == 1
        assert detector.state == AudioTriggerState.COOLDOWN

    def test_cooldown_rearming(self):
        """After cooldown_ms elapses, state should return to IDLE."""
        config = AudioTriggerConfig(
            threshold_rms=500.0, min_duration_ms=0, cooldown_ms=0
        )
        detector = AudioTriggerDetector(on_trigger=lambda c: None, config=config)
        chunk = self._make_chunk()
        detector._transition(1000.0, chunk)  # IDLE -> DETECTING
        detector._transition(1000.0, chunk)  # DETECTING -> TRIGGERED -> COOLDOWN
        # cooldown=0 means it should re-arm immediately
        detector._transition(100.0, chunk)   # COOLDOWN elapsed -> IDLE
        assert detector.state == AudioTriggerState.IDLE

    def test_callback_not_called_in_idle(self):
        """Low RMS should never fire callback."""
        fired = []
        config = AudioTriggerConfig(threshold_rms=500.0)
        detector = AudioTriggerDetector(on_trigger=lambda c: fired.append(c), config=config)
        chunk = self._make_chunk()
        for _ in range(10):
            detector._transition(50.0, chunk)  # Always below threshold
        assert len(fired) == 0
        assert detector.state == AudioTriggerState.IDLE

    def test_compute_rms_helper(self):
        """Verify _compute_rms function works correctly."""
        from openclaw_embodiment.triggers.audio_trigger import _compute_rms
        silent = struct.pack("<4h", 0, 0, 0, 0)
        assert _compute_rms(silent) == 0.0
        # DC offset signal: RMS = value
        dc = struct.pack("<4h", 1000, 1000, 1000, 1000)
        assert _compute_rms(dc) == pytest.approx(1000.0, abs=1.0)

    def test_start_stop(self):
        """Detector should start and stop without error."""
        config = AudioTriggerConfig(threshold_rms=999999.0)  # Will never trigger
        detector = AudioTriggerDetector(on_trigger=lambda c: None, config=config)

        with patch(
            "openclaw_embodiment.triggers.audio_trigger.AudioTriggerDetector._capture_burst",
        ) as mock_capture:
            mock_capture.return_value = make_audio_chunk(0.0)
            detector.start()
            time.sleep(0.2)
            detector.stop()
            assert not detector._running


# ---------------------------------------------------------------------------
# ProximityContext assembly tests
# ---------------------------------------------------------------------------

class TestProximityContext:

    def test_known_device_matched(self):
        """Known MAC in scan results should appear in known_devices."""
        from openclaw_embodiment.hal.ble_scanner import BLEProximityScanner, ProximityContext

        scanner = BLEProximityScanner(
            known_map={"aa:bb:cc:dd:ee:ff": "MikePendant"},
            scan_duration_s=0.1,
        )
        # Mock the internal async scan method directly
        mock_device = MagicMock()
        mock_device.address = "AA:BB:CC:DD:EE:FF"
        mock_device.rssi = -65

        async def _mock_scan(self_inner):
            from openclaw_embodiment.hal.ble_scanner import ProximityContext as PC
            rssi_map = {"aa:bb:cc:dd:ee:ff": -65}
            return PC(
                known_devices=["MikePendant"],
                unknown_count=0,
                rssi_map=rssi_map,
                confidence=0.85,
                timestamp_ms=int(time.time() * 1000),
            )

        with patch.object(scanner, "_async_scan", lambda: _mock_scan(scanner)):
            ctx = scanner.scan()

        assert "MikePendant" in ctx.known_devices
        assert ctx.unknown_count == 0
        assert "aa:bb:cc:dd:ee:ff" in ctx.rssi_map

    def test_unknown_device_counted(self):
        from openclaw_embodiment.hal.ble_scanner import BLEProximityScanner, ProximityContext

        scanner = BLEProximityScanner(known_map={}, scan_duration_s=0.1)

        async def _mock_scan():
            return ProximityContext(
                known_devices=[],
                unknown_count=1,
                rssi_map={"11:22:33:44:55:66": -70},
                confidence=0.7,
                timestamp_ms=int(time.time() * 1000),
            )

        with patch.object(scanner, "_async_scan", _mock_scan):
            ctx = scanner.scan()

        assert ctx.unknown_count == 1
        assert ctx.known_devices == []

    def test_empty_scan(self):
        from openclaw_embodiment.hal.ble_scanner import BLEProximityScanner, ProximityContext

        scanner = BLEProximityScanner(known_map={}, scan_duration_s=0.1)

        async def _mock_scan():
            return ProximityContext(
                known_devices=[], unknown_count=0, rssi_map={},
                confidence=0.5, timestamp_ms=int(time.time() * 1000),
            )

        with patch.object(scanner, "_async_scan", _mock_scan):
            ctx = scanner.scan()

        assert ctx.known_devices == []
        assert ctx.unknown_count == 0
        assert ctx.rssi_map == {}
        assert ctx.confidence >= 0.0

    def test_context_builder_proximity_integration(self):
        """ContextBuilder.build() should call BLE scanner and return ProximityContext."""
        builder = ContextBuilder(
            device_id="prox-test",
            ble_scanner=MockBLEScanner(known=["DeviceA"], unknown=3),
        )
        ctx = builder.build(trigger="ble_new_device")
        assert ctx.proximity is not None
        assert ctx.proximity.known_devices == ["DeviceA"]
        assert ctx.proximity.unknown_count == 3

    def test_proximity_none_when_no_scanner(self):
        builder = ContextBuilder(device_id="no-ble")
        ctx = builder.build()
        assert ctx.proximity is None


# ---------------------------------------------------------------------------
# DistillerCameraHAL extension tests
# ---------------------------------------------------------------------------

class TestDistillerCameraHALExtensions:

    def test_color_reliable_flag(self):
        """DistillerCameraHAL should have color_reliable=False by default."""
        from openclaw_embodiment.hal.distiller_reference import DistillerCameraHAL
        hal = DistillerCameraHAL()
        assert hal.color_reliable is False

    def test_capture_grayscale_returns_jpeg(self):
        """capture_grayscale should return valid JPEG bytes (L mode)."""
        from openclaw_embodiment.hal.distiller_reference import DistillerCameraHAL
        from PIL import Image
        import io

        hal = DistillerCameraHAL()
        hal._width = 64
        hal._height = 64

        # Mock capture() to return a color JPEG
        color_img = Image.new("RGB", (64, 64), color=(100, 200, 50))
        buf = io.BytesIO()
        color_img.save(buf, format="JPEG")
        hal.capture = lambda: buf.getvalue()

        gray_bytes = hal.capture_grayscale()
        assert len(gray_bytes) > 100

        # Verify it's actually grayscale
        result = Image.open(io.BytesIO(gray_bytes))
        assert result.mode == "L"

    def test_get_lighting_level(self):
        """get_lighting_level should return bright/dim/dark based on pixel mean."""
        from openclaw_embodiment.hal.distiller_reference import DistillerCameraHAL
        from PIL import Image
        import io

        hal = DistillerCameraHAL()
        hal._width = 64
        hal._height = 64

        def make_gray_jpeg(value: int) -> bytes:
            img = Image.new("L", (64, 64), color=value)
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            return buf.getvalue()

        hal.capture = lambda: make_gray_jpeg(200)  # bright
        assert hal.get_lighting_level() == "bright"

        hal.capture = lambda: make_gray_jpeg(100)  # dim
        assert hal.get_lighting_level() == "dim"

        hal.capture = lambda: make_gray_jpeg(20)   # dark
        assert hal.get_lighting_level() == "dark"
