"""Tests for UnifiedTransport wrapper."""

from __future__ import annotations

import pytest

from openclaw_embodiment.hal.base import SendResult, TransportState
from openclaw_embodiment.hal.simulator import SimulatedTransport
from openclaw_embodiment.transport.unified import (
    TimedSendResult,
    TransportStats,
    UnifiedTransport,
    UnifiedTransportConfig,
    _percentile,
)


class AlwaysFailTransport(SimulatedTransport):
    """Transport that always returns failure."""

    def send(self, payload: bytes) -> SendResult:
        return SendResult(success=False, bytes_sent=0, elapsed_ms=1, error_code="ALWAYS_FAIL")


class SlowTransport(SimulatedTransport):
    """Transport that actually sleeps to simulate high latency."""

    def __init__(self, latency_ms: int = 200) -> None:
        super().__init__()
        self._latency_ms = latency_ms

    def send(self, payload: bytes) -> SendResult:
        import time
        time.sleep(self._latency_ms / 1000.0)
        return SendResult(success=True, bytes_sent=len(payload), elapsed_ms=self._latency_ms)


class TestTimedSendResult:
    def test_from_send_result(self) -> None:
        raw = SendResult(success=True, bytes_sent=10, elapsed_ms=5)
        timed = TimedSendResult.from_send_result(raw, sent_at=1000, received_at=1005, transport_type="BLE")
        assert timed.success is True
        assert timed.transport_type == "BLE"
        assert timed.used_fallback is False

    def test_from_send_result_fallback(self) -> None:
        raw = SendResult(success=True, bytes_sent=10, elapsed_ms=5)
        timed = TimedSendResult.from_send_result(raw, sent_at=1000, received_at=1005, transport_type="HTTP", used_fallback=True)
        assert timed.used_fallback is True


class TestUnifiedTransportBasic:
    def _make(self, primary=None, fallback=None, **kwargs) -> UnifiedTransport:
        if primary is None:
            primary = SimulatedTransport()
        config = UnifiedTransportConfig(primary=primary, fallback=fallback, **kwargs)
        t = UnifiedTransport(config)
        t.initialize({})
        t.connect()
        return t

    def test_successful_send(self) -> None:
        t = self._make()
        result = t.send(b"hello")
        assert isinstance(result, TimedSendResult)
        assert result.success is True
        assert result.bytes_sent > 0
        assert result.transport_type == "SimulatedTransport"

    def test_latency_metadata(self) -> None:
        t = self._make()
        result = t.send(b"hello")
        assert result.sent_at > 0
        assert result.received_at >= result.sent_at

    def test_get_state(self) -> None:
        t = self._make()
        assert t.get_state() == TransportState.CONNECTED

    def test_get_expected_latency(self) -> None:
        t = self._make()
        assert isinstance(t.get_expected_latency_ms(), int)

    def test_get_device_info(self) -> None:
        info = self._make().get_device_info()
        assert "name" in info
        assert info["primary"] == "SimulatedTransport"


class TestUnifiedTransportFallback:
    def test_fallback_on_error(self) -> None:
        primary = AlwaysFailTransport()
        fallback = SimulatedTransport()
        config = UnifiedTransportConfig(primary=primary, fallback=fallback, fallback_on_error=True)
        t = UnifiedTransport(config)
        t.initialize({})
        t.connect()
        result = t.send(b"test")
        assert result.success is True
        assert result.used_fallback is True
        assert result.transport_type == "SimulatedTransport"

    def test_no_fallback_on_error_when_disabled(self) -> None:
        primary = AlwaysFailTransport()
        fallback = SimulatedTransport()
        config = UnifiedTransportConfig(primary=primary, fallback=fallback, fallback_on_error=False)
        t = UnifiedTransport(config)
        t.initialize({})
        t.connect()
        result = t.send(b"test")
        assert result.success is False
        assert result.used_fallback is False

    def test_fallback_on_latency_threshold(self) -> None:
        primary = SlowTransport(latency_ms=300)
        fallback = SimulatedTransport()
        config = UnifiedTransportConfig(
            primary=primary,
            fallback=fallback,
            fallback_latency_threshold_ms=100,
        )
        t = UnifiedTransport(config)
        t.initialize({})
        t.connect()
        result = t.send(b"test")
        # Primary succeeded but was too slow -- fallback should be used
        assert result.used_fallback is True

    def test_no_fallback_configured(self) -> None:
        primary = AlwaysFailTransport()
        config = UnifiedTransportConfig(primary=primary)
        t = UnifiedTransport(config)
        t.initialize({})
        t.connect()
        result = t.send(b"test")
        assert result.success is False
        assert result.used_fallback is False


class TestTransportStats:
    def _make_with_sends(self, n: int) -> UnifiedTransport:
        primary = SimulatedTransport()
        config = UnifiedTransportConfig(primary=primary, window_size=100)
        t = UnifiedTransport(config)
        t.initialize({})
        t.connect()
        for _ in range(n):
            t.send(b"x" * 100)
        return t

    def test_empty_stats(self) -> None:
        primary = SimulatedTransport()
        t = UnifiedTransport(UnifiedTransportConfig(primary=primary))
        stats = t.get_transport_stats()
        assert stats.total_sends == 0
        assert stats.success_rate == 0.0
        assert stats.p50_ms is None

    def test_success_rate_all_success(self) -> None:
        t = self._make_with_sends(10)
        stats = t.get_transport_stats()
        assert stats.total_sends == 10
        assert stats.success_rate == 1.0
        assert stats.failure_count == 0

    def test_latency_percentiles_populated(self) -> None:
        t = self._make_with_sends(20)
        stats = t.get_transport_stats()
        assert stats.p50_ms is not None
        assert stats.p95_ms is not None
        assert stats.p99_ms is not None

    def test_window_eviction(self) -> None:
        primary = SimulatedTransport()
        config = UnifiedTransportConfig(primary=primary, window_size=5)
        t = UnifiedTransport(config)
        t.initialize({})
        t.connect()
        for _ in range(10):
            t.send(b"x")
        stats = t.get_transport_stats()
        assert stats.total_sends == 5  # window capped at 5

    def test_failure_counted(self) -> None:
        primary = AlwaysFailTransport()
        config = UnifiedTransportConfig(primary=primary, fallback_on_error=False)
        t = UnifiedTransport(config)
        t.initialize({})
        t.connect()
        t.send(b"x")
        stats = t.get_transport_stats()
        assert stats.failure_count == 1
        assert "AlwaysFailTransport" in stats.failure_by_transport

    def test_measured_latency(self) -> None:
        t = self._make_with_sends(5)
        lat = t.get_measured_latency_ms()
        assert lat is not None
        assert lat >= 0


class TestPercentile:
    def test_median(self) -> None:
        data = [1, 2, 3, 4, 5]
        assert _percentile(data, 50) == pytest.approx(3.0)

    def test_empty(self) -> None:
        assert _percentile([], 50) == 0.0

    def test_single(self) -> None:
        assert _percentile([42], 99) == pytest.approx(42.0)
