"""AudioTriggerDetector -- mic-based voice activity trigger for Distiller CM5.

State machine: IDLE -> DETECTING -> TRIGGERED -> COOLDOWN -> IDLE

Hardware: Pamir AI SoundCard, hw:0,0, 48kHz stereo S16_LE
"""

from __future__ import annotations

import logging
import math
import os
import struct
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from openclaw_embodiment.hal.base import AudioChunk

logger = logging.getLogger(__name__)


class AudioTriggerState(Enum):
    IDLE = "IDLE"
    DETECTING = "DETECTING"
    TRIGGERED = "TRIGGERED"
    COOLDOWN = "COOLDOWN"


@dataclass
class AudioTriggerConfig:
    """Configuration for the AudioTriggerDetector."""
    # RMS energy threshold to consider audio "active" (range 0-32767 for S16_LE)
    threshold_rms: float = 800.0
    # How long audio must be above threshold to fire (ms)
    min_duration_ms: int = 300
    # Polling burst length (ms) -- shorter = more responsive, more CPU
    poll_duration_ms: int = 250
    # Cooldown after TRIGGERED before re-arming (ms)
    cooldown_ms: int = 2000
    # Audio device
    device: str = "hw:0,0"
    sample_rate: int = 48000
    # Channels
    channels: int = 2
    # Format
    format: str = "S16_LE"


def _compute_rms(raw_pcm: bytes) -> float:
    """Compute RMS energy of raw S16_LE PCM data."""
    if len(raw_pcm) < 2:
        return 0.0
    num_samples = len(raw_pcm) // 2
    samples = struct.unpack(f"<{num_samples}h", raw_pcm[:num_samples * 2])
    mean_sq = sum(s * s for s in samples) / num_samples
    return math.sqrt(mean_sq)


class AudioTriggerDetector:
    """Polls microphone in short bursts and fires callback when voice detected.

    State transitions:
        IDLE        -- listening for energy above threshold
        DETECTING   -- above threshold, measuring duration
        TRIGGERED   -- threshold met for min_duration_ms; callback fired
        COOLDOWN    -- waiting cooldown_ms before re-arming

    Usage::

        def on_trigger(chunk: AudioChunk):
            print("Voice detected!", chunk.duration_ms)

        detector = AudioTriggerDetector(on_trigger=on_trigger)
        detector.start()
        ...
        detector.stop()
    """

    def __init__(
        self,
        on_trigger: Callable[[AudioChunk], None],
        config: Optional[AudioTriggerConfig] = None,
    ) -> None:
        self.on_trigger = on_trigger
        self.config = config or AudioTriggerConfig()
        self._state = AudioTriggerState.IDLE
        self._state_entered_ms: int = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Begin polling the microphone in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="audio-trigger")
        self._thread.start()
        logger.info("[AudioTrigger] Started. threshold_rms=%.0f min_dur=%dms cooldown=%dms",
                    self.config.threshold_rms, self.config.min_duration_ms, self.config.cooldown_ms)

    def stop(self) -> None:
        """Stop polling and wait for thread to exit."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        logger.info("[AudioTrigger] Stopped.")

    @property
    def state(self) -> AudioTriggerState:
        with self._lock:
            return self._state

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Main polling loop -- runs in background thread."""
        while self._running:
            try:
                chunk = self._capture_burst()
                rms = _compute_rms(chunk.data)
                logger.debug("[AudioTrigger] state=%s rms=%.1f", self._state.value, rms)
                self._transition(rms, chunk)
            except Exception as e:  # grain: ignore NAKED_EXCEPT -- trigger wake call -- hardware unavailability is expected
                logger.warning("[AudioTrigger] Capture error: %s", e)
                time.sleep(0.5)

    def _capture_burst(self) -> AudioChunk:
        """Capture a short audio burst for RMS analysis."""
        duration_s = max(1, int(math.ceil(self.config.poll_duration_ms / 1000)))
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            subprocess.run(
                [
                    "arecord",
                    "-D", self.config.device,
                    "-f", self.config.format,
                    "-r", str(self.config.sample_rate),
                    "-c", str(self.config.channels),
                    "-d", str(duration_s),
                    tmp,
                ],
                capture_output=True,
                check=True,
                timeout=duration_s + 3,
            )
            with wave.open(tmp, "rb") as w:
                raw = w.readframes(w.getnframes())
            return AudioChunk(
                data=raw,
                sample_rate=self.config.sample_rate,
                channels=self.config.channels,
                format="pcm_16",
                duration_ms=self.config.poll_duration_ms,
                timestamp_ms=int(time.time() * 1000),
            )
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _set_state(self, new_state: AudioTriggerState) -> None:
        with self._lock:
            self._state = new_state
            self._state_entered_ms = self._now_ms()

    def _elapsed_in_state_ms(self) -> int:
        return self._now_ms() - self._state_entered_ms

    def _transition(self, rms: float, chunk: AudioChunk) -> None:
        """Drive the state machine based on current RMS reading."""
        state = self.state

        if state == AudioTriggerState.IDLE:
            if rms >= self.config.threshold_rms:
                logger.debug("[AudioTrigger] IDLE -> DETECTING (rms=%.1f)", rms)
                self._set_state(AudioTriggerState.DETECTING)

        elif state == AudioTriggerState.DETECTING:
            if rms < self.config.threshold_rms:
                # Dropped below threshold -- back to IDLE
                logger.debug("[AudioTrigger] DETECTING -> IDLE (rms dropped)")
                self._set_state(AudioTriggerState.IDLE)
            elif self._elapsed_in_state_ms() >= self.config.min_duration_ms:
                # Sustained long enough -- TRIGGER
                logger.info("[AudioTrigger] DETECTING -> TRIGGERED (rms=%.1f dur=%dms)",
                            rms, self._elapsed_in_state_ms())
                self._set_state(AudioTriggerState.TRIGGERED)
                self._fire(chunk)
                self._set_state(AudioTriggerState.COOLDOWN)

        elif state == AudioTriggerState.COOLDOWN:
            if self._elapsed_in_state_ms() >= self.config.cooldown_ms:
                logger.debug("[AudioTrigger] COOLDOWN -> IDLE")
                self._set_state(AudioTriggerState.IDLE)

        elif state == AudioTriggerState.TRIGGERED:
            # Should transition to COOLDOWN immediately after fire -- belt & suspenders
            self._set_state(AudioTriggerState.COOLDOWN)

    def _fire(self, chunk: AudioChunk) -> None:
        """Invoke the trigger callback (non-blocking via thread)."""
        try:
            t = threading.Thread(target=self.on_trigger, args=(chunk,), daemon=True)
            t.start()
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- trigger wake call -- hardware unavailability is expected
            logger.error("[AudioTrigger] Callback error: %s", e)
