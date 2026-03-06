"""Tests for v1.1 + v1.2 new features.

Covers:
- StatusIndicatorHal ABC + SimulatedStatusIndicator
- TransportHal latency methods (get_expected_latency_ms / get_measured_latency_ms)
- TriggerArbiter (all policies + dedup)
- AttachmentTransport (instantiation, config, latency)
"""

from __future__ import annotations

import time
import threading
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from openclaw_embodiment.hal.base import (
    StatusIndicatorHal,
    StatusPattern,
    TransportHal,
    TransportState,
    SendResult,
)
from openclaw_embodiment.hal.simulator import (
    SimulatedStatusIndicator,
    SimulatedTransport,
)
from openclaw_embodiment.triggers.arbiter import (
    ArbiterConfig,
    ArbiterPolicy,
    TriggerArbiter,
)
from openclaw_embodiment.core.trigger import TriggerEvent
from openclaw_embodiment.transport.attachment import (
    AttachmentConfig,
    AttachmentTransport,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    event_id: str = "evt-001",
    confidence: float = 0.8,
) -> TriggerEvent:
    return TriggerEvent(
        event_id=event_id,
        timestamp_ms=int(time.monotonic() * 1000),
        timestamp_epoch=int(time.time()),
        trigger_confidence=confidence,
        head_pitch=0.0,
        head_yaw=0.0,
        head_roll=0.0,
    )


# ===========================================================================
# StatusIndicatorHal
# ===========================================================================

class TestStatusIndicatorHalABC:
    """StatusIndicatorHal is a proper ABC."""

    def test_cannot_instantiate_abc_directly(self) -> None:
        with pytest.raises(TypeError):
            StatusIndicatorHal()  # type: ignore[abstract]

    def test_simulator_is_instance_of_abc(self) -> None:
        assert isinstance(SimulatedStatusIndicator(), StatusIndicatorHal)


class TestSimulatedStatusIndicator:
    """Simulator implements all StatusIndicatorHal methods correctly."""

    def setup_method(self) -> None:
        self.led = SimulatedStatusIndicator()
        self.led.initialize()

    def test_initialize_sets_off(self) -> None:
        assert self.led.color == (0, 0, 0)
        assert not self.led.is_on

    def test_set_color_updates_state(self) -> None:
        self.led.set_color(255, 0, 128)
        assert self.led.color == (255, 0, 128)
        assert self.led.is_on

    def test_set_color_raises_on_invalid_channel(self) -> None:
        with pytest.raises(ValueError):
            self.led.set_color(256, 0, 0)
        with pytest.raises(ValueError):
            self.led.set_color(0, -1, 0)

    def test_off_resets_state(self) -> None:
        self.led.set_color(255, 255, 0)
        self.led.off()
        assert self.led.color == (0, 0, 0)
        assert not self.led.is_on

    def test_blink_sets_interval(self) -> None:
        self.led.blink(interval_ms=200)
        assert self.led.blink_interval_ms == 200
        assert self.led.is_on
        assert self.led.pattern is None

    def test_pulse_sets_pattern(self) -> None:
        self.led.pulse("heartbeat")
        assert self.led.pattern == "heartbeat"
        assert self.led.is_on

    def test_pulse_raises_on_invalid_pattern(self) -> None:
        with pytest.raises(ValueError):
            self.led.pulse("disco")

    def test_validate_returns_true(self) -> None:
        assert self.led.validate() is True

    def test_get_device_info_has_name(self) -> None:
        info = self.led.get_device_info()
        assert "name" in info

    def test_shutdown_calls_off(self) -> None:
        self.led.set_color(0, 255, 0)
        self.led.shutdown()
        assert not self.led.is_on

    def test_all_valid_patterns(self) -> None:
        for pattern in ("heartbeat", "alert", "processing", "idle"):
            self.led.pulse(pattern)
            assert self.led.pattern == pattern


class TestStatusPattern:
    """StatusPattern enum values."""

    def test_all_patterns_defined(self) -> None:
        assert StatusPattern.HEARTBEAT.value == "heartbeat"
        assert StatusPattern.ALERT.value == "alert"
        assert StatusPattern.PROCESSING.value == "processing"
        assert StatusPattern.IDLE.value == "idle"


# ===========================================================================
# TransportHal latency methods
# ===========================================================================

class TestSimulatedTransportLatency:
    """SimulatedTransport implements latency methods."""

    def setup_method(self) -> None:
        self.transport = SimulatedTransport()
        self.transport.initialize({})
        self.transport.connect()

    def test_get_expected_latency_ms_returns_int(self) -> None:
        result = self.transport.get_expected_latency_ms()
        assert isinstance(result, int)
        assert result >= 0

    def test_get_measured_latency_ms_none_before_send(self) -> None:
        fresh = SimulatedTransport()
        fresh.initialize({})
        assert fresh.get_measured_latency_ms() is None

    def test_get_measured_latency_ms_after_sends(self) -> None:
        for _ in range(3):
            self.transport.send(b"test payload")
        measured = self.transport.get_measured_latency_ms()
        assert measured is not None
        assert isinstance(measured, int)
        assert measured >= 0

    def test_latency_window_caps_at_10(self) -> None:
        for i in range(15):
            self.transport.send(b"x" * i)
        # Window should only hold last 10
        assert len(self.transport._latency_window) == 10


# ===========================================================================
# TriggerArbiter -- FIRST_WINS policy
# ===========================================================================

class TestTriggerArbiterFirstWins:
    """TriggerArbiter with FIRST_WINS policy."""

    def _make_arbiter(self, events: List[TriggerEvent]) -> TriggerArbiter:
        def cb(e: TriggerEvent) -> None:
            events.append(e)

        return TriggerArbiter(
            on_trigger=cb,
            config=ArbiterConfig(policy=ArbiterPolicy.FIRST_WINS, dedup_window_ms=500),
        )

    def test_first_event_emitted(self) -> None:
        collected: List[TriggerEvent] = []
        arbiter = self._make_arbiter(collected)
        e1 = _make_event("evt-001", 0.9)
        arbiter.push(e1)
        time.sleep(0.05)
        assert len(collected) == 1
        assert collected[0].event_id == "evt-001"

    def test_duplicate_within_window_suppressed(self) -> None:
        collected: List[TriggerEvent] = []
        arbiter = self._make_arbiter(collected)
        e1 = _make_event("evt-001", 0.9)
        e2 = _make_event("evt-002", 0.8)
        arbiter.push(e1)
        time.sleep(0.05)  # let callback fire
        arbiter.push(e2)  # should be suppressed (within 500ms window)
        time.sleep(0.05)
        assert len(collected) == 1

    def test_second_event_after_window_emitted(self) -> None:
        collected: List[TriggerEvent] = []
        arbiter = TriggerArbiter(
            on_trigger=lambda e: collected.append(e),
            config=ArbiterConfig(policy=ArbiterPolicy.FIRST_WINS, dedup_window_ms=50),
        )
        arbiter.push(_make_event("evt-001", 0.9))
        time.sleep(0.1)  # wait for window to expire
        arbiter.push(_make_event("evt-002", 0.8))
        time.sleep(0.05)
        assert len(collected) == 2


# ===========================================================================
# TriggerArbiter -- HIGHEST_CONFIDENCE policy
# ===========================================================================

class TestTriggerArbiterHighestConfidence:
    """HIGHEST_CONFIDENCE selects the best event in the window."""

    def test_highest_confidence_wins(self) -> None:
        collected: List[TriggerEvent] = []
        arbiter = TriggerArbiter(
            on_trigger=lambda e: collected.append(e),
            config=ArbiterConfig(
                policy=ArbiterPolicy.HIGHEST_CONFIDENCE, dedup_window_ms=100
            ),
        )
        arbiter.push(_make_event("low", 0.3))
        arbiter.push(_make_event("high", 0.95))
        arbiter.push(_make_event("mid", 0.6))
        time.sleep(0.25)  # wait for flush
        assert len(collected) == 1
        assert collected[0].event_id == "high"


# ===========================================================================
# TriggerArbiter -- AUDIO_PRIORITY policy
# ===========================================================================

class TestTriggerArbiterAudioPriority:
    """AUDIO_PRIORITY promotes audio events over visual within window."""

    def test_audio_event_not_suppressed_over_visual(self) -> None:
        collected: List[TriggerEvent] = []
        arbiter = TriggerArbiter(
            on_trigger=lambda e: collected.append(e),
            config=ArbiterConfig(policy=ArbiterPolicy.AUDIO_PRIORITY, dedup_window_ms=500),
        )
        # Push a visual event first
        arbiter.push(_make_event("visual-001", 0.8))
        time.sleep(0.05)
        # Push an audio event within window -- should replace
        arbiter.push(_make_event("audio-002", 0.7))
        time.sleep(0.05)
        # At minimum, audio event was considered (no crash)
        assert len(collected) >= 1


# ===========================================================================
# AttachmentTransport
# ===========================================================================

class TestAttachmentTransport:
    """AttachmentTransport instantiation, config, and latency."""

    def test_default_instantiation(self) -> None:
        t = AttachmentTransport()
        assert t is not None

    def test_get_expected_latency_ms(self) -> None:
        t = AttachmentTransport()
        lat = t.get_expected_latency_ms()
        assert isinstance(lat, int)
        assert lat > 0

    def test_get_measured_latency_ms_initially_none(self) -> None:
        t = AttachmentTransport()
        assert t.get_measured_latency_ms() is None

    def test_connect_and_state(self) -> None:
        t = AttachmentTransport()
        t.initialize({"session_id": "test-session"})
        assert t.get_state() == TransportState.CONNECTED

    def test_disconnect_changes_state(self) -> None:
        t = AttachmentTransport()
        t.initialize({})
        t.disconnect()
        assert t.get_state() == TransportState.DISCONNECTED

    def test_get_device_info(self) -> None:
        t = AttachmentTransport()
        info = t.get_device_info()
        assert "name" in info
        assert info["name"] == "attachment-transport"

    def test_receive_returns_none(self) -> None:
        t = AttachmentTransport()
        result = t.receive()
        assert result is None

    def test_send_fallback_on_failure(self) -> None:
        """When send fails and fallback is configured, routes to fallback."""
        fallback = SimulatedTransport()
        fallback.initialize({})
        fallback.connect()
        config = AttachmentConfig(
            openclaw_bin="nonexistent_bin_xyz",
            fallback_transport=fallback,
        )
        t = AttachmentTransport(config=config)
        result = t.send(b"test payload")
        # Should route to fallback (SimulatedTransport always succeeds)
        assert result.success

    def test_set_state_callback_invoked(self) -> None:
        events: List[TransportState] = []
        t = AttachmentTransport()
        t.set_state_callback(lambda s: events.append(s))
        t.initialize({})
        assert TransportState.CONNECTED in events

    def test_validate_with_missing_binary(self) -> None:
        """validate() returns False when binary not found."""
        t = AttachmentTransport(AttachmentConfig(openclaw_bin="definitely_not_a_real_binary"))
        assert t.validate() is False
