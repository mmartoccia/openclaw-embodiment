"""Raspberry Pi Zero 2W reference HAL for OpenClaw Wearable SDK.

Spec-based implementation. Validated against SDK docs. Hardware validation required.

Thin wrapper over pi3_reference.py providing Zero 2W-specific aliases and
performance-constrained defaults.

Hardware: Raspberry Pi Zero 2W
  - 1GHz quad-core Cortex-A53 (ARMv8), 512MB LPDDR2 RAM
  - PiCamera Module 3 (autofocus, 12MP sensor)
  - Bluetooth 4.2 onboard (BLE)
  - Optional MPU6050 IMU via I2C
  - ALSA microphone via USB adapter or I2S mic (e.g. INMP441)

Performance constraints vs Pi 3/4/5:
  - Camera: use 320x240 @ 10fps (higher resolutions will saturate the GPU/CPU)
  - Audio: 16kHz mono (stereo doubles RAM pressure on the ring buffer)
  - IMU poll: 10Hz max (25Hz causes CPU spike under camera load)
  - Avoid running classifier on-device -- offload to host via transport

Install requirements (same as pi3_reference):
  pip install picamera2 pyaudio smbus2 opencv-python

Usage:
  from openclaw_embodiment.hal.pi_zero2w_reference import (
      PiZero2WCameraHAL,
      PiZero2WMicrophoneHAL,
      PiZero2WTransportHAL,
      PIZERO2W_DEFAULTS,
      PIZERO2W_TRIGGER_PROFILE,
  )

  cam = PiZero2WCameraHAL()
  cam.initialize(resolution=PIZERO2W_DEFAULTS["camera_resolution"])
"""

from __future__ import annotations

import logging
from typing import Dict, Tuple

from ..core.trigger import TriggerConfig
from ..hal.base import AudioChunk
from ..transport.stt_bridge import STTProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-export Pi3 HAL classes under Zero 2W aliases
# The Pi Zero 2W runs the same software stack as Pi 3B+; only defaults differ.
# ---------------------------------------------------------------------------
from .pi3_reference import (
    PiCamera as PiZero2WCameraHAL,
    PiMicrophone as _PiMicrophone,
    PiBLETransport as PiZero2WTransportHAL,
)


class PiZero2WMicrophoneHAL(_PiMicrophone):
    """Performance-constrained microphone HAL for Pi Zero 2W.

    Delegates transcribe() to OpenClawSTTBridge with a warning that STT
    is CPU-intensive on the Zero 2W -- prefer offloading to the host.
    """

    def transcribe(self, audio: AudioChunk, language: str = "en") -> str:
        """Transcribe audio via OpenClaw native STT bridge.

        WARNING: Pi Zero 2W is CPU-constrained (512MB RAM, 1GHz quad-core).
        STT is offloaded to the OpenClaw runtime subprocess. For high-throughput
        use cases, consider streaming audio to the host instead.
        """
        logger.warning(
            "PiZero2WMicrophoneHAL.transcribe() called -- "
            "STT offloaded to OpenClaw runtime (subprocess). "
            "Pi Zero 2W is CPU-constrained; consider host-side STT for low-latency pipelines."
        )
        return super().transcribe(audio, language=language)


__all__ = [
    "PiZero2WCameraHAL",
    "PiZero2WMicrophoneHAL",
    "PiZero2WTransportHAL",
    "PIZERO2W_DEFAULTS",
    "PIZERO2W_TRIGGER_PROFILE",
]

# ---------------------------------------------------------------------------
# Performance-constrained defaults for Zero 2W
# ---------------------------------------------------------------------------

PIZERO2W_DEFAULTS: Dict[str, object] = {
    "camera_resolution": (320, 240),   # type: Tuple[int, int]
    "camera_fps": 10,
    "audio_sample_rate": 16000,
    "audio_channels": 1,
}
"""Pass these to HAL initialize() calls to avoid saturating the Zero 2W's limited resources."""

# ---------------------------------------------------------------------------
# Trigger profile -- GLASSES_TRIGGER_PROFILE with polling_hz reduced to 10
# to match the CPU budget on a 512MB quad-core at camera+IMU concurrency.
# ---------------------------------------------------------------------------

PIZERO2W_TRIGGER_PROFILE = TriggerConfig(
    polling_hz=10,                         # Reduced from 25 -- CPU budget
    saccade_threshold_dps=180.0,           # Same as GLASSES_TRIGGER_PROFILE
    saccade_duration_ms=200,
    fixation_threshold_dps=20.0,
    fixation_duration_ms=400,
    motion_reject_threshold_dps=280.0,
    motion_reject_duration_ms=150,
    refractory_period_ms=700,
)
"""Tuned for Pi Zero 2W with MPU6050 at 10Hz polling. Same thresholds as GLASSES_TRIGGER_PROFILE
but with lower polling rate to conserve CPU cycles shared with PiCamera pipeline."""
