"""UnifiedTransport -- wraps any TransportHal with latency metadata, auto-fallback, and rolling stats.

Does NOT replace existing transports. All existing transport implementations are unchanged.
This module wraps them to add:
  - Latency metadata on every message (sent_at, received_at, transport_type)
  - Automatic fallback: if primary fails or exceeds latency threshold, retry via fallback
  - Rolling 100-message stats: success rate, p50/p95/p99 latency, failure counts
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Optional

from ..hal.base import SendResult, TransportHal, TransportState


@dataclass
class TimedSendResult:
    """SendResult extended with timing metadata.

    Attributes:
        success: Whether the send succeeded.
        bytes_sent: Number of bytes sent.
        elapsed_ms: Time for the send call to return (ms).
        error_code: Optional error code string.
        retries: Number of retries attempted.
        sent_at: Monotonic timestamp (ms) when send was initiated.
        received_at: Monotonic timestamp (ms) when send returned.
        transport_type: Name of the transport class that completed the send.
        used_fallback: True if the fallback transport was used.
    """

    success: bool
    bytes_sent: int
    elapsed_ms: int
    error_code: Optional[str]
    retries: int
    sent_at: int
    received_at: int
    transport_type: str
    used_fallback: bool = False

    @classmethod
    def from_send_result(
        cls,
        result: SendResult,
        sent_at: int,
        received_at: int,
        transport_type: str,
        used_fallback: bool = False,
    ) -> "TimedSendResult":
        """Construct from a raw SendResult and timing info."""
        return cls(
            success=result.success,
            bytes_sent=result.bytes_sent,
            elapsed_ms=result.elapsed_ms,
            error_code=result.error_code,
            retries=result.retries,
            sent_at=sent_at,
            received_at=received_at,
            transport_type=transport_type,
            used_fallback=used_fallback,
        )


@dataclass
class TransportStats:
    """Rolling statistics over the last 100 messages.

    Attributes:
        total_sends: Total send attempts recorded.
        success_count: Number of successful sends.
        failure_count: Number of failed sends.
        success_rate: Fraction of successful sends (0.0-1.0).
        p50_ms: Median latency in milliseconds.
        p95_ms: 95th percentile latency in milliseconds.
        p99_ms: 99th percentile latency in milliseconds.
        failure_by_transport: Count of failures per transport class name.
        fallback_count: Number of times the fallback transport was used.
    """

    total_sends: int
    success_count: int
    failure_count: int
    success_rate: float
    p50_ms: Optional[float]
    p95_ms: Optional[float]
    p99_ms: Optional[float]
    failure_by_transport: dict
    fallback_count: int


@dataclass
class UnifiedTransportConfig:
    """Configuration for UnifiedTransport.

    Attributes:
        primary: Primary transport to use for all sends.
        fallback: Optional fallback transport used when primary fails or exceeds threshold.
        fallback_on_error: If True (default), automatically retry via fallback on error.
        fallback_latency_threshold_ms: If set, retry via fallback when primary send
            exceeds this latency even if it technically succeeded.
        window_size: Number of recent messages to keep in the rolling stats window.
    """

    primary: TransportHal
    fallback: Optional[TransportHal] = None
    fallback_on_error: bool = True
    fallback_latency_threshold_ms: Optional[int] = None
    window_size: int = 100


class UnifiedTransport(TransportHal):
    """Wraps one or two TransportHal instances, adding latency metadata and auto-fallback.

    Usage::

        config = UnifiedTransportConfig(primary=ble, fallback=http)
        unified = UnifiedTransport(config)
        unified.initialize({})
        unified.connect()
        result = unified.send(payload)  # TimedSendResult
        stats = unified.get_transport_stats()

    ``UnifiedTransport`` itself implements ``TransportHal`` so it can be registered
    in ``HALRegistry`` in place of a raw transport.
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, config: UnifiedTransportConfig) -> None:
        self._config = config
        self._window: list[TimedSendResult] = []
        self._failure_by_transport: dict[str, int] = {}

    # ------------------------------------------------------------------
    # TransportHal contract
    # ------------------------------------------------------------------

    def initialize(self, config: dict) -> None:
        """Initialize the primary and optional fallback transports."""
        self._config.primary.initialize(config)
        if self._config.fallback is not None:
            self._config.fallback.initialize(config)

    def connect(self) -> None:
        """Connect the primary and optional fallback transports."""
        self._config.primary.connect()
        if self._config.fallback is not None:
            try:
                self._config.fallback.connect()
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- fallback connect failure is non-fatal; transport errors are device-specific
                # Fallback connect failure is non-fatal
                import logging
                logging.getLogger(__name__).warning(
                    "Fallback transport connect failed: %s", exc
                )

    def send(self, payload: bytes) -> TimedSendResult:  # type: ignore[override]
        """Send payload via primary transport, falling back if configured.

        Returns:
            TimedSendResult with latency metadata and transport_type set.
        """
        sent_at = _ms()
        primary_name = type(self._config.primary).__name__
        result: Optional[SendResult] = None
        use_fallback = False

        try:
            result = self._config.primary.send(payload)
            received_at = _ms()
            elapsed_ms = received_at - sent_at

            # Check latency threshold
            if (
                result.success
                and self._config.fallback is not None
                and self._config.fallback_latency_threshold_ms is not None
                and elapsed_ms > self._config.fallback_latency_threshold_ms
            ):
                use_fallback = True

            # Fallback on error
            if not result.success and self._config.fallback_on_error and self._config.fallback is not None:
                use_fallback = True

        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- primary transport send may raise any device/driver error
            received_at = _ms()
            elapsed_ms = received_at - sent_at
            result = SendResult(success=False, bytes_sent=0, elapsed_ms=elapsed_ms, error_code=str(exc))
            if self._config.fallback is not None and self._config.fallback_on_error:
                use_fallback = True

        if use_fallback and self._config.fallback is not None:
            fallback_name = type(self._config.fallback).__name__
            fallback_sent_at = _ms()
            try:
                fallback_result = self._config.fallback.send(payload)
                fallback_received_at = _ms()
                timed = TimedSendResult.from_send_result(
                    fallback_result,
                    sent_at=fallback_sent_at,
                    received_at=fallback_received_at,
                    transport_type=fallback_name,
                    used_fallback=True,
                )
            except Exception as exc2:  # grain: ignore NAKED_EXCEPT -- fallback transport send may raise any device/driver error
                fallback_received_at = _ms()
                timed = TimedSendResult(
                    success=False,
                    bytes_sent=0,
                    elapsed_ms=fallback_received_at - fallback_sent_at,
                    error_code=str(exc2),
                    retries=0,
                    sent_at=fallback_sent_at,
                    received_at=fallback_received_at,
                    transport_type=fallback_name,
                    used_fallback=True,
                )
        else:
            timed = TimedSendResult.from_send_result(
                result,
                sent_at=sent_at,
                received_at=received_at,
                transport_type=primary_name,
                used_fallback=False,
            )

        self._record(timed)
        return timed

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Receive from the primary transport."""
        return self._config.primary.receive(timeout_ms)

    def get_state(self) -> TransportState:
        """Return state of the primary transport."""
        return self._config.primary.get_state()

    def set_state_callback(self, callback) -> None:
        """Register state callback on primary transport."""
        self._config.primary.set_state_callback(callback)

    def disconnect(self) -> None:
        """Disconnect both transports."""
        self._config.primary.disconnect()
        if self._config.fallback is not None:
            self._config.fallback.disconnect()

    def shutdown(self) -> None:
        """Shutdown both transports."""
        self._config.primary.shutdown()
        if self._config.fallback is not None:
            self._config.fallback.shutdown()

    def get_expected_latency_ms(self) -> int:
        """Return expected latency from the primary transport."""
        return self._config.primary.get_expected_latency_ms()

    def get_measured_latency_ms(self) -> Optional[int]:
        """Return rolling average of recent send latencies from internal window."""
        latencies = [r.elapsed_ms for r in self._window if r.success]
        if not latencies:
            return None
        return int(sum(latencies) / len(latencies))

    def validate(self) -> bool:
        """Validate primary transport."""
        return self._config.primary.validate()

    def get_device_info(self) -> dict:
        """Return metadata for this unified transport."""
        return {
            "name": "unified-transport",
            "primary": type(self._config.primary).__name__,
            "fallback": type(self._config.fallback).__name__ if self._config.fallback else None,
            "window_size": self._config.window_size,
        }

    # ------------------------------------------------------------------
    # Stats API
    # ------------------------------------------------------------------

    def get_transport_stats(self) -> TransportStats:
        """Return rolling statistics over the last ``window_size`` messages.

        Returns:
            TransportStats with success rate, latency percentiles, and failure counts.
        """
        window = list(self._window)
        if not window:
            return TransportStats(
                total_sends=0,
                success_count=0,
                failure_count=0,
                success_rate=0.0,
                p50_ms=None,
                p95_ms=None,
                p99_ms=None,
                failure_by_transport=dict(self._failure_by_transport),
                fallback_count=0,
            )

        total = len(window)
        success_count = sum(1 for r in window if r.success)
        failure_count = total - success_count
        fallback_count = sum(1 for r in window if r.used_fallback)

        latencies = sorted(r.elapsed_ms for r in window if r.success)
        p50 = _percentile(latencies, 50) if latencies else None
        p95 = _percentile(latencies, 95) if latencies else None
        p99 = _percentile(latencies, 99) if latencies else None

        return TransportStats(
            total_sends=total,
            success_count=success_count,
            failure_count=failure_count,
            success_rate=success_count / total,
            p50_ms=p50,
            p95_ms=p95,
            p99_ms=p99,
            failure_by_transport=dict(self._failure_by_transport),
            fallback_count=fallback_count,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record(self, result: TimedSendResult) -> None:
        """Record result in the rolling window, evicting oldest if at capacity."""
        if not result.success:
            self._failure_by_transport[result.transport_type] = (
                self._failure_by_transport.get(result.transport_type, 0) + 1
            )
        self._window.append(result)
        if len(self._window) > self._config.window_size:
            self._window.pop(0)


def _ms() -> int:
    """Return current monotonic time in milliseconds."""
    return time.monotonic_ns() // 1_000_000


def _percentile(sorted_data: list[int], p: float) -> float:
    """Calculate percentile p (0-100) from a sorted list of values."""
    if not sorted_data:
        return 0.0
    idx = (p / 100.0) * (len(sorted_data) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_data) - 1)
    frac = idx - lo
    return sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac


__all__ = [
    "UnifiedTransport",
    "UnifiedTransportConfig",
    "TimedSendResult",
    "TransportStats",
]
