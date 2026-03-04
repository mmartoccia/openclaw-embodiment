"""Tests for requestHeartbeatNow() and CAPTURE-state heartbeat integration."""

import time
from unittest.mock import MagicMock, call, patch

import pytest

from openclaw_embodiment.core.trigger import (
    HeartbeatWakeResult,
    TriggerConfig,
    TriggerDetector,
    TriggerEvent,
)
from openclaw_embodiment.hal.base import IMUSample


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_imu(ts_ms: int, gyro: float = 0.0) -> IMUSample:
    """Build a minimal IMUSample for testing."""
    return IMUSample(
        timestamp_ms=ts_ms,
        accel_x=0.0,
        accel_y=0.0,
        accel_z=9.8,
        gyro_x=gyro,
        gyro_y=0.0,
        gyro_z=0.0,
    )


def _make_detector() -> TriggerDetector:
    """Build a TriggerDetector with fast thresholds for testing."""
    config = TriggerConfig(
        saccade_threshold_dps=50.0,
        saccade_duration_ms=50,
        fixation_threshold_dps=10.0,
        fixation_duration_ms=80,
        motion_reject_threshold_dps=300.0,
        refractory_period_ms=200,
        polling_hz=25,
    )
    return TriggerDetector(config)


# ---------------------------------------------------------------------------
# HeartbeatWakeResult dataclass
# ---------------------------------------------------------------------------

class TestHeartbeatWakeResult:
    def test_fields_success(self):
        """HeartbeatWakeResult populates all fields correctly."""
        ts = time.time()
        result = HeartbeatWakeResult(
            success=True,
            timestamp=ts,
            cooldown_remaining=0.0,
            reason="motion_detected",
        )
        assert result.success is True
        assert result.timestamp == ts
        assert result.cooldown_remaining == 0.0
        assert result.reason == "motion_detected"

    def test_fields_cooldown(self):
        """HeartbeatWakeResult represents a blocked (cooldown) result."""
        result = HeartbeatWakeResult(
            success=False,
            timestamp=time.time(),
            cooldown_remaining=3.5,
        )
        assert result.success is False
        assert result.cooldown_remaining == 3.5
        assert result.reason == "device_trigger"  # default


# ---------------------------------------------------------------------------
# request_heartbeat_now() cooldown tests
# ---------------------------------------------------------------------------

class TestRequestHeartbeatNow:
    def test_first_call_returns_true(self):
        """First request_heartbeat_now() call always returns True."""
        detector = _make_detector()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            result = detector.request_heartbeat_now("test_reason")
        assert result is True

    def test_cooldown_blocks_rapid_calls(self):
        """Rapid calls within 5s cooldown window return False."""
        detector = _make_detector()
        # Force last heartbeat to now
        detector._last_heartbeat_time = time.monotonic()

        with patch("subprocess.run") as mock_run:
            result = detector.request_heartbeat_now("rapid_trigger")

        assert result is False
        # subprocess should NOT be called when cooldown blocks
        mock_run.assert_not_called()

    def test_cooldown_allows_call_after_expiry(self):
        """Calls are allowed after cooldown window expires."""
        detector = _make_detector()
        # Simulate last call was 6 seconds ago (past 5s cooldown)
        detector._last_heartbeat_time = time.monotonic() - 6.0

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            result = detector.request_heartbeat_now("resumed")

        assert result is True

    def test_default_reason(self):
        """request_heartbeat_now() uses 'device_trigger' as default reason."""
        detector = _make_detector()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            detector.request_heartbeat_now()  # No reason arg

        # Verify reason appears in the subprocess call
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert any("device_trigger" in arg for arg in cmd)

    def test_cli_not_found_does_not_raise(self):
        """request_heartbeat_now() doesn't raise when openclaw CLI is missing."""
        detector = _make_detector()
        with patch("subprocess.run", side_effect=FileNotFoundError("openclaw not found")):
            result = detector.request_heartbeat_now("test")
        # Should still return True (attempt was made, CLI just not installed)
        assert result is True

    def test_subprocess_timeout_does_not_raise(self):
        """request_heartbeat_now() handles subprocess timeout gracefully."""
        import subprocess as _subprocess
        detector = _make_detector()
        with patch("subprocess.run", side_effect=_subprocess.TimeoutExpired("openclaw", 2.0)):
            result = detector.request_heartbeat_now("timeout_test")
        assert result is True  # Still returns True -- attempt was made


# ---------------------------------------------------------------------------
# CAPTURE state -> heartbeat wake integration
# ---------------------------------------------------------------------------

class TestCaptureHeartbeatIntegration:
    def test_capture_triggers_heartbeat_wake(self):
        """CAPTURE state transition automatically calls request_heartbeat_now()."""
        detector = _make_detector()

        with patch.object(detector, "request_heartbeat_now", return_value=True) as mock_hb:
            # Drive state machine through IDLE -> SACCADE -> CAPTURE.
            # Start at ts=1000 to avoid _saccade_start=0 sentinel edge case.
            base = 1000

            # Phase 1: sustained saccade (fast gyro for saccade_duration_ms=50ms)
            # Need at least 50ms of fast gyro: updates at 1000, 1010, 1020, ... 1060
            for i in range(8):
                detector.update(_make_imu(ts_ms=base + i * 10, gyro=100.0))

            # State should now be SACCADE
            assert detector.state == "SACCADE", (
                f"Expected SACCADE, got {detector.state}. "
                "_saccade_start must have been set -- check sentinel logic."
            )

            # Phase 2: fixation (slow gyro for fixation_duration_ms=80ms)
            event = None
            saccade_end = base + 80
            for i in range(12):
                ts = saccade_end + i * 10
                ev = detector.update(_make_imu(ts_ms=ts, gyro=2.0))  # below fixation threshold
                if ev is not None:
                    event = ev
                    break

        # Heartbeat should have been called on CAPTURE
        mock_hb.assert_called()
        # The reason should indicate capture -- may be positional or keyword arg
        call_args = mock_hb.call_args
        reason_str = (
            call_args[0][0] if call_args[0]
            else call_args.kwargs.get("reason", "")
        )
        assert "capture" in reason_str or "trigger" in reason_str

    def test_no_heartbeat_before_capture(self):
        """request_heartbeat_now() is NOT called during IDLE state updates."""
        detector = _make_detector()

        with patch.object(detector, "request_heartbeat_now") as mock_hb:
            # Only IDLE-state updates (no trigger conditions)
            for i in range(10):
                detector.update(_make_imu(ts_ms=i * 10, gyro=0.5))  # below all thresholds

        mock_hb.assert_not_called()

    def test_heartbeat_wake_result_dataclass_used(self):
        """HeartbeatWakeResult can be constructed from request_heartbeat_now() data."""
        detector = _make_detector()

        # Force last_heartbeat to allow this call
        detector._last_heartbeat_time = 0.0

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr=b"")
            success = detector.request_heartbeat_now("integration_test")

        result = HeartbeatWakeResult(
            success=success,
            timestamp=time.time(),
            cooldown_remaining=0.0,
            reason="integration_test",
        )
        assert result.success is True
        assert result.reason == "integration_test"
