"""Trigger pipeline state handling."""

import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from ..hal.base import IMUSample


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


class TriggerDetector:
    """State machine implementing IDLE->SACCADE->FIXATION->CAPTURE."""

    def __init__(self, config: TriggerConfig) -> None:
        self.config = config
        self.state = "IDLE"
        self._saccade_start = 0
        self._fixation_start = 0
        self._last_capture = 0

    def update(self, sample: IMUSample) -> Optional[TriggerEvent]:
        """Consume IMU sample and emit trigger event when capture condition is met."""
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
