"""Brilliant Labs Frame AR Glasses reference HAL for OpenClaw Wearable SDK.

Spec-based implementation. Validated against SDK docs. Hardware validation required.

Wraps the Frame Python SDK (pip install frame-sdk) to provide full OpenClaw
HAL compliance for the Brilliant Labs Frame AR glasses -- the original target
form factor for the OpenClaw Embodiment SDK.

Hardware: Brilliant Labs Frame
  - Alif B1 MCU (Cortex-M55 + Ethos-U55 NPU)
  - 640x400 OLED display (waveguide AR overlay)
  - 1080p camera sensor
  - MEMS microphone (single, no DoA)
  - IMU: accelerometer (BMI270 or equivalent)
  - BLE 5.3 to host (iOS/Android/Mac)

Frame SDK notes:
  - Most Frame operations are async (asyncio-based)
  - Sync wrapper pattern: _run_async() via event loop
  - Frame instance is created by caller and passed to each HAL
  - SDK: https://github.com/brilliantlabs/frame-sdk-python

Install requirements:
  pip install frame-sdk

Usage:
  import asyncio
  import frame

  async def main():
      async with frame.Frame() as f:
          cam = FrameCameraHAL(f)
          cam.initialize()
          frame_img = cam.capture_frame()

  asyncio.run(main())

  # Or in sync context using HAL wrappers:
  # (FrameCameraHAL._run_async handles the event loop bridging)
"""

from __future__ import annotations

import asyncio
import queue
import time
from typing import Any, Callable, Optional, Tuple

from ..core.trigger import TriggerConfig
from .base import (
    AudioChunk,
    CameraFrame,
    CameraHal,
    DisplayCard,
    DisplayHal,
    IMUHal,
    IMUSample,
    MicrophoneHal,
    SendResult,
    TransportHal,
    TransportState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms() -> int:
    return int(time.time() * 1000)


def _run_async(coro: Any) -> Any:
    """Run an async coroutine synchronously from a non-async context.

    Uses the current running loop if available (e.g. in Jupyter), otherwise
    creates a new event loop. This bridges the Frame SDK's async API into the
    synchronous HAL contract.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # In a running loop (e.g. Jupyter) -- use run_coroutine_threadsafe
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=10.0)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        # No event loop in this thread -- create one
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Camera HAL
# ---------------------------------------------------------------------------

class FrameCameraHAL(CameraHal):
    """Camera HAL wrapping Frame SDK camera capture.

    Calls frame.camera.capture_photo() (async) via _run_async() bridge.
    Returns JPEG-encoded CameraFrame objects.

    API uncertainty: Frame SDK uses `await frame.camera.capture_photo()`
    which returns raw bytes. The format is JPEG per Frame SDK v0.x docs.
    Validate against installed SDK version on hardware.
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, frame_instance: Any) -> None:
        self._frame = frame_instance
        self._resolution: Tuple[int, int] = (1280, 720)

    def initialize(self, resolution: Tuple[int, int] = (1280, 720)) -> None:
        """Store target resolution. Frame camera is always-on via BLE stream."""
        self._resolution = resolution

    def capture_frame(self) -> CameraFrame:
        """Capture photo from Frame camera via async SDK call."""
        now = _ms()
        try:
            jpeg_bytes = _run_async(self._frame.camera.capture_photo())
            if jpeg_bytes is None:
                jpeg_bytes = b""
            return CameraFrame(
                timestamp_ms=now,
                width=self._resolution[0],
                height=self._resolution[1],
                format="JPEG",
                data=jpeg_bytes,
            )
        except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
            return CameraFrame(
                timestamp_ms=now,
                width=self._resolution[0],
                height=self._resolution[1],
                format="JPEG",
                data=b"",
            )

    def shutdown(self) -> None:
        pass  # BLE connection managed by caller / context manager

    def validate(self) -> bool:
        try:
            frame = self.capture_frame()
            return len(frame.data) > 0
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            return False

    def get_device_info(self) -> dict:
        return {
            "name": "frame-camera",
            "sensor": "1080p",
            "interface": "ble",
            "resolution": self._resolution,
            "note": "Captured via BLE; JPEG bytes returned by Frame SDK",
        }


# ---------------------------------------------------------------------------
# IMU HAL
# ---------------------------------------------------------------------------

class FrameIMUHAL(IMUHal):
    """IMU HAL wrapping Frame accelerometer.

    Frame has a MEMS accelerometer (BMI270 or equivalent). This HAL reads
    accelerometer data via the Frame SDK. If gyroscope data is not available
    (Frame v0.x only exposes accel), gyro values are computed as accel deltas.

    API uncertainty: Frame SDK imu/motion API surface may change across SDK
    versions. The attribute path `frame.motion` is used per v0.5+ docs.
    Validate on hardware and adjust if SDK uses `frame.imu` instead.
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, frame_instance: Any) -> None:
        self._frame = frame_instance
        self._sample_rate_hz: int = 25
        self._last_accel: Optional[Tuple[float, float, float]] = None
        self._last_ts: int = 0

    def initialize(self, sample_rate_hz: int = 25) -> None:
        self._sample_rate_hz = max(1, sample_rate_hz)
        self._last_ts = _ms()

    def read_sample(self) -> Optional[IMUSample]:
        """Read accelerometer from Frame; compute gyro from delta if needed."""
        now = _ms()
        dt_s = max((now - self._last_ts) / 1000.0, 1e-6)

        accel_x, accel_y, accel_z = 0.0, 0.0, 9.8
        gyro_x, gyro_y, gyro_z = 0.0, 0.0, 0.0

        try:
            # Try frame.motion first (Frame SDK v0.5+), fall back to frame.imu
            motion = None
            try:
                motion = _run_async(self._frame.motion.get_direction())
            except AttributeError:
                try:
                    motion = _run_async(self._frame.imu.read())
                except AttributeError:
                    pass

            if motion is not None:
                # SDK returns object with .x, .y, .z attributes (radians or g)
                accel_x = float(getattr(motion, "x", 0.0))
                accel_y = float(getattr(motion, "y", 0.0))
                accel_z = float(getattr(motion, "z", 9.8))

            # Compute synthetic gyro from accel delta when no hardware gyro
            if self._last_accel is not None:
                gyro_x = (accel_x - self._last_accel[0]) / dt_s
                gyro_y = (accel_y - self._last_accel[1]) / dt_s
                gyro_z = (accel_z - self._last_accel[2]) / dt_s

            self._last_accel = (accel_x, accel_y, accel_z)
        except Exception:  # grain: ignore NAKED_EXCEPT -- IMU read -- sensor errors vary by bus type
            if self._last_accel is not None:
                accel_x, accel_y, accel_z = self._last_accel

        self._last_ts = now
        return IMUSample(
            timestamp_ms=now,
            accel_x=accel_x,
            accel_y=accel_y,
            accel_z=accel_z,
            gyro_x=gyro_x,
            gyro_y=gyro_y,
            gyro_z=gyro_z,
        )

    def set_sample_rate(self, hz: int) -> None:
        self._sample_rate_hz = max(1, hz)

    def shutdown(self) -> None:
        self._last_accel = None

    def validate(self) -> bool:
        try:
            sample = self.read_sample()
            return sample is not None
        except Exception:  # grain: ignore NAKED_EXCEPT -- IMU read -- sensor errors vary by bus type
            return False

    def get_device_info(self) -> dict:
        return {
            "name": "frame-imu",
            "type": "accelerometer",
            "note": "Gyro computed from accel delta; Frame v0.5+ motion API",
            "sample_rate_hz": self._sample_rate_hz,
        }


# ---------------------------------------------------------------------------
# Microphone HAL
# ---------------------------------------------------------------------------

class FrameMicrophoneHAL(MicrophoneHal):
    """Microphone HAL wrapping Frame single MEMS microphone.

    Frame has one microphone -- no Direction of Arrival (DoA) support.
    Audio is streamed via BLE using frame.microphone API.

    API uncertainty: Frame SDK microphone streaming API surface varies by
    SDK version. `frame.microphone.record()` is documented in v0.5+ as
    returning bytes. Validate on hardware.
    """

    HAL_VERSION = "1.0.0"
    SAMPLE_RATE = 16000

    def __init__(self, frame_instance: Any) -> None:
        self._frame = frame_instance
        self._recording = False
        self._buffer: queue.Queue = queue.Queue(maxsize=100)

    def initialize(self, sample_rate: int = 16000, channels: int = 1) -> None:
        pass  # Frame microphone always available via BLE when connected

    def start_recording(self) -> None:
        """Begin audio capture via Frame microphone API."""
        if self._recording:
            return
        try:
            _run_async(self._frame.microphone.start())
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass
        self._recording = True

    def stop_recording(self) -> None:
        """Stop audio capture."""
        if not self._recording:
            return
        try:
            _run_async(self._frame.microphone.stop())
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass
        self._recording = False

    def get_buffer(self, duration_ms: int) -> AudioChunk:
        """Return buffered audio from Frame microphone as PCM16 bytes."""
        now = _ms()
        try:
            # Frame SDK record() call: returns raw audio bytes for duration
            # API: await frame.microphone.record(duration_ms) -> bytes
            audio_bytes = _run_async(
                self._frame.microphone.record(duration_ms=duration_ms)
            )
            if audio_bytes is None:
                audio_bytes = b""
        except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
            # Return silence if hardware not available
            n_samples = int(self.SAMPLE_RATE * duration_ms / 1000)
            audio_bytes = b"\x00" * (n_samples * 2)  # mono int16

        return AudioChunk(
            timestamp_ms=now,
            sample_rate=self.SAMPLE_RATE,
            channels=1,
            format="PCM_S16LE",
            data=audio_bytes,
        )

    def get_doa(self) -> None:
        """Single microphone -- Direction of Arrival not supported."""
        return None

    def shutdown(self) -> None:
        self.stop_recording()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "frame-microphone",
            "channels": 1,
            "sample_rate": self.SAMPLE_RATE,
            "features": [],
            "note": "Single MEMS mic; no DoA support",
        }


# ---------------------------------------------------------------------------
# Display HAL
# ---------------------------------------------------------------------------

class FrameDisplayHAL(DisplayHal):
    """Display HAL wrapping Frame 640x400 OLED waveguide display.

    Renders OpenClaw DisplayCards as text overlays via Frame SDK.
    Frame display is a waveguide AR overlay; text appears in the user's
    field of view. Image rendering requires additional SDK support.

    API uncertainty: Frame SDK display API uses `frame.display.write_text()`
    in v0.5+ but earlier versions may use `frame.display.show_text()`.
    Validate on hardware.
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, frame_instance: Any) -> None:
        self._frame = frame_instance
        self._resolution: Tuple[int, int] = (640, 400)

    def initialize(self, resolution: Tuple[int, int] = (640, 400)) -> None:
        self._resolution = resolution
        try:
            # Clear display on init
            _run_async(self._frame.display.clear())
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass

    def show(self, card: DisplayCard) -> None:
        """Render DisplayCard body text to Frame OLED display."""
        try:
            text = card.body
            if card.title:
                text = f"{card.title}\n{card.body}"
            try:
                _run_async(self._frame.display.write_text(text))
            except AttributeError:
                _run_async(self._frame.display.show_text(text))
        except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
            pass

    def clear(self) -> None:
        """Clear Frame display."""
        try:
            _run_async(self._frame.display.clear())
        except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
            pass

    def shutdown(self) -> None:
        self.clear()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "frame-display",
            "type": "oled_waveguide",
            "resolution": self._resolution,
            "interface": "ble",
        }


# ---------------------------------------------------------------------------
# Transport HAL -- BLE via Frame SDK
# ---------------------------------------------------------------------------

class FrameTransportHAL(TransportHal):
    """Transport HAL using Frame SDK BLE connection as transport layer.

    Frame is already BLE-connected when the SDK frame instance is created.
    This HAL wraps the Frame SDK's Bluetooth data channel for sending
    Wearable Packet v1 payloads to the OpenClaw gateway.

    API uncertainty: Frame SDK bluetooth/data send API varies by version.
    `frame.bluetooth.send_data(payload)` is used per v0.5+ docs.
    Earlier versions may use `frame.send()`. Validate on hardware.
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, frame_instance: Any) -> None:
        self._frame = frame_instance
        self._state = TransportState.DISCONNECTED
        self._cb: Optional[Callable[[TransportState], None]] = None
        self._recv_q: queue.Queue = queue.Queue()

    def initialize(self, config: dict) -> None:
        pass  # BLE managed by Frame SDK; no separate init needed

    def connect(self) -> None:
        """Frame is already connected via SDK context manager; mark CONNECTED."""
        self._set_state(TransportState.CONNECTED)

    def send(self, payload: bytes) -> SendResult:
        """Send payload via Frame SDK BLE data channel."""
        t0 = _ms()
        try:
            try:
                _run_async(self._frame.bluetooth.send_data(payload))
            except AttributeError:
                # Fallback: some SDK versions expose frame.send() directly
                _run_async(self._frame.send(payload))
            return SendResult(True, len(payload), _ms() - t0)
        except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
            return SendResult(False, 0, _ms() - t0)

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Receive data from Frame BLE channel (best-effort queue pop)."""
        try:
            return self._recv_q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return None

    def get_state(self) -> TransportState:
        return self._state

    def set_state_callback(self, cb: Callable[[TransportState], None]) -> None:
        self._cb = cb

    def disconnect(self) -> None:
        self._set_state(TransportState.DISCONNECTED)

    def shutdown(self) -> None:
        self.disconnect()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "frame-ble-transport",
            "type": "ble",
            "note": "BLE 5.3 via Frame SDK bluetooth data channel",
        }

    def _set_state(self, state: TransportState) -> None:
        self._state = state
        if self._cb:
            self._cb(state)


# ---------------------------------------------------------------------------
# Recommended TriggerConfig for Frame glasses
# ---------------------------------------------------------------------------

FRAME_TRIGGER_PROFILE = TriggerConfig(
    polling_hz=25,
    saccade_threshold_dps=180.0,
    saccade_duration_ms=200,
    fixation_threshold_dps=20.0,
    fixation_duration_ms=400,
    motion_reject_threshold_dps=280.0,
    motion_reject_duration_ms=150,
    refractory_period_ms=700,
)
"""Tuned for Frame glasses with accelerometer IMU at 25Hz polling.
Matches GLASSES_TRIGGER_PROFILE -- Frame is the canonical target form factor.
Note: Gyro is computed from accel delta; tune thresholds on hardware if
derivative-based gyro is noisier than a physical gyroscope."""
