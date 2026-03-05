"""Trigger pipeline state handling."""

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..hal.base import IMUSample

logger = logging.getLogger(__name__)


@dataclass
class HeartbeatWakeResult:
    """Holds the outcome and timestamp of a heartbeat wake attempt.

    Attributes:
        success: True if the wake call was dispatched, False if cooldown blocked it.
        timestamp: Epoch timestamp of this wake attempt.
        cooldown_remaining: Seconds until next wake call is allowed (0 if success).
        reason: Why the wake was requested.
    """

    success: bool
    timestamp: float
    cooldown_remaining: float
    reason: str = "device_trigger"


@dataclass
class TriggerEvent:
    """Event emitted by trigger detector."""

    event_id: str
    timestamp_ms: int
    timestamp_epoch: int
    trigger_confidence: float
    head_pitch: float
    head_yaw: float
    head_roll: float


@dataclass
class TriggerConfig:
    """Threshold and timing settings for trigger state machine."""

    polling_hz: int = 25
    saccade_threshold_dps: float = 180.0
    saccade_duration_ms: int = 200
    fixation_threshold_dps: float = 20.0
    fixation_duration_ms: int = 400
    motion_reject_threshold_dps: float = 280.0
    motion_reject_duration_ms: int = 150
    refractory_period_ms: int = 700


#: Alias for TriggerConfig. Use named profiles for device-specific tuning.
TriggerProfile = TriggerConfig


class TriggerDetector:
    """State machine implementing IDLE->SACCADE->FIXATION->CAPTURE."""

    #: Minimum seconds between heartbeat wake calls (anti-spam).
    HEARTBEAT_COOLDOWN_S: float = 5.0

    def __init__(self, config: TriggerConfig) -> None:
        self.config = config
        self.state = "IDLE"
        self._saccade_start = 0
        self._fixation_start = 0
        self._last_capture = 0
        self._last_heartbeat_time: float = 0.0

    def update(self, sample: IMUSample) -> Optional[TriggerEvent]:
        """Consume IMU sample and emit trigger event when capture condition is met.

        On CAPTURE state transition, automatically calls request_heartbeat_now()
        to wake the OpenClaw agent immediately.

        Args:
            sample: Single IMU measurement from the device.

        Returns:
            TriggerEvent if CAPTURE state was reached, None otherwise.
        """
        now = sample.timestamp_ms
        speed = max(abs(sample.gyro_x), abs(sample.gyro_y), abs(sample.gyro_z))
        if now - self._last_capture < self.config.refractory_period_ms:
            return None
        if speed > self.config.motion_reject_threshold_dps:
            self.state = "IDLE"
            self._saccade_start = 0
            self._fixation_start = 0
            return None
        if self.state == "IDLE":
            if speed >= self.config.saccade_threshold_dps:
                self._saccade_start = now if self._saccade_start == 0 else self._saccade_start
                if now - self._saccade_start >= self.config.saccade_duration_ms:
                    self.state = "SACCADE"
            else:
                self._saccade_start = 0
            return None
        if self.state == "SACCADE":
            if speed <= self.config.fixation_threshold_dps:
                self._fixation_start = now if self._fixation_start == 0 else self._fixation_start
                if now - self._fixation_start >= self.config.fixation_duration_ms:
                    self.state = "CAPTURE"
                    self._last_capture = now
                    self.state = "IDLE"
                    # Auto-wake agent on CAPTURE state transition.
                    self.request_heartbeat_now(reason="capture_trigger")
                    return TriggerEvent(
                        event_id="evt-%d" % now,
                        timestamp_ms=now,
                        timestamp_epoch=int(time.time()),
                        trigger_confidence=0.9,
                        head_pitch=sample.gyro_x,
                        head_yaw=sample.gyro_y,
                        head_roll=sample.gyro_z,
                    )
            else:
                self._fixation_start = 0
            return None
        return None

    def request_heartbeat_now(self, reason: str = "device_trigger") -> bool:
        """Wake the OpenClaw agent via gateway REST API.
        
        Enforces 5-second cooldown between calls.
        Returns True if wake dispatched, False if on cooldown or unavailable.
        """
        now = time.monotonic()
        elapsed = now - self._last_heartbeat_time
        if elapsed < self.HEARTBEAT_COOLDOWN_S:
            logger.debug("[Trigger] Heartbeat cooldown active (%.1fs).", self.HEARTBEAT_COOLDOWN_S - elapsed)
            return False
        self._last_heartbeat_time = now
        logger.info("[Trigger] Requesting heartbeat wake (reason=%r).", reason)
        try:
            import urllib.request as _ur, json as _j
            _payload = _j.dumps({"reason": reason, "source": "embodiment_sdk"}).encode()
            _req = _ur.Request("http://localhost:18799/heartbeat/trigger",
                data=_payload, headers={"Content-Type": "application/json"}, method="POST")
            with _ur.urlopen(_req, timeout=2) as _resp:
                dispatched = _resp.status == 200
                logger.debug("[Trigger] Gateway heartbeat: status=%d", _resp.status)
                return dispatched
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- trigger wake call -- hardware unavailability is expected
            logger.debug("[Trigger] Gateway unavailable (%s) -- heartbeat skipped.", e)
            return False