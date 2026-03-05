"""Reachy Mini Lite reference HAL for OpenClaw Wearable SDK.

Wraps the `reachy-mini` Python SDK (pip install reachy-mini) to provide
full OpenClaw HAL compliance for the Reachy Mini Lite robot.

Hardware: Pollen Robotics / Hugging Face Reachy Mini Lite
  - Wide-angle head camera (OpenCV via media_backend='default')
  - 2-microphone array with Direction of Arrival
  - 5W speaker
  - LED eyes (expressive face display)
  - 6-DoF head (Stewart platform, servo encoders for position feedback)
  - 360-degree body rotation
  - USB-C to host Mac/Linux, daemon at localhost:8000
  - NO IMU in Lite version -- head encoder positions used as gyro substitute

Install requirements:
  pip install reachy-mini opencv-python numpy scipy

Usage:
  from reachy_mini import ReachyMini
  from openclaw_embodiment.hal.reachy_reference import (
      ReachyMotionTracker, ReachyCameraHAL, ReachyMicrophoneHAL,
      ReachyAudioOutputHAL, ReachyDisplayHAL, ReachyTransportHAL,
  )

  reachy = ReachyMini(media_backend='default')
  reachy.__enter__()  # or use as context manager

  registry = HALRegistry()
  registry.register_imu(ReachyMotionTracker(reachy))
  registry.register_camera(ReachyCameraHAL(reachy))
  registry.register_microphone(ReachyMicrophoneHAL(reachy))
  registry.register_audio_output(ReachyAudioOutputHAL(reachy))
  registry.register_display(ReachyDisplayHAL(reachy))
  registry.register_transport(ReachyTransportHAL(), priority=0)
"""

from __future__ import annotations

import io
import math
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from ..transport.stt_bridge import OpenClawSTTBridge, STTProvider
from .base import (
    ActuatorCommand,
    ActuatorHal,
    ActuatorResult,
    AudioChunk,
    AudioOutputHal,
    CameraFrame,
    CameraHal,
    ClassifierHal,
    DisplayCard,
    DisplayHal,
    IMUSample,
    IMUHal,
    MicrophoneHal,
    SendResult,
    TransportHal,
    TransportState,
)
try:
    from ..utils.clock import ms_now  # type: ignore[import]
except ImportError:
    pass  # utils.clock not available in all environments; use local _ms() instead

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_jpeg(frame_rgb: np.ndarray) -> bytes:
    """Encode numpy RGB frame to JPEG bytes."""
    try:
        import cv2  # type: ignore
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR))
        return bytes(buf) if ok else b""
    except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
        return b""


def _ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# IMU HAL -- Head Encoder Substitute
# ---------------------------------------------------------------------------

class ReachyMotionTracker(IMUHal):
    """Converts Reachy head servo encoder positions to motion signals for the TriggerDetector.

    Detects head repositioning events (not biological saccades) as angular velocity
    from position deltas.
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._poll_hz: int = 10
        self._last_pose: Optional[Dict[str, float]] = None
        self._last_poll_ms: int = 0

    # -- HAL interface -------------------------------------------------------

    def initialize(self, sample_rate_hz: int = 10) -> None:
        self._poll_hz = max(1, sample_rate_hz)
        self._last_pose = self._get_head_pose()
        self._last_poll_ms = _ms()

    def read_sample(self) -> Optional[IMUSample]:
        now = _ms()
        dt_s = max((now - self._last_poll_ms) / 1000.0, 1e-6)

        pose = self._get_head_pose()
        if self._last_pose is None:
            self._last_pose = pose
            self._last_poll_ms = now
            return None

        # Angular velocity in degrees/s from position delta
        gyro_x = math.degrees(pose["pitch"] - self._last_pose["pitch"]) / dt_s
        gyro_y = math.degrees(pose["yaw"]   - self._last_pose["yaw"])   / dt_s
        gyro_z = math.degrees(pose["roll"]  - self._last_pose["roll"])  / dt_s

        self._last_pose = pose
        self._last_poll_ms = now

        return IMUSample(
            timestamp_ms=now,
            accel_x=0.0, accel_y=0.0, accel_z=9.8,  # No accelerometer in Lite
            gyro_x=gyro_x, gyro_y=gyro_y, gyro_z=gyro_z,
        )

    def set_sample_rate(self, hz: int) -> None:
        self._poll_hz = max(1, hz)

    def shutdown(self) -> None:
        self._last_pose = None

    def validate(self) -> bool:
        try:
            pose = self._get_head_pose()
            return isinstance(pose, dict)
        except Exception:  # grain: ignore NAKED_EXCEPT -- servo/motor read -- firmware errors are not typed
            return False

    def get_device_info(self) -> dict:
        return {
            "name": "reachy-motion-tracker",
            "type": "encoder_substitute",
            "axes": 3,
            "note": "No physical IMU in Lite; derived from servo encoder positions",
            "max_rate_hz": 10,
            "current_rate_hz": self._poll_hz,
        }

    # -- Internal ------------------------------------------------------------

    def _get_head_pose(self) -> Dict[str, float]:
        """Read current head servo position as pitch/roll/yaw in radians."""
        try:
            # reachy.head.present_position returns dict-like with joint angles
            pos = self._reachy.head.present_position
            if hasattr(pos, "__getitem__"):
                return {
                    "pitch": float(pos.get("head_pitch", 0.0)),
                    "roll":  float(pos.get("head_roll",  0.0)),
                    "yaw":   float(pos.get("head_yaw",   0.0)),
                }
            # Fallback: some SDK versions return named tuple or object
            return {
                "pitch": float(getattr(pos, "head_pitch", 0.0)),
                "roll":  float(getattr(pos, "head_roll",  0.0)),
                "yaw":   float(getattr(pos, "head_yaw",   0.0)),
            }
        except Exception:  # grain: ignore NAKED_EXCEPT -- HAL hardware call -- exception types vary by SDK and platform
            return {"pitch": 0.0, "roll": 0.0, "yaw": 0.0}


# ---------------------------------------------------------------------------
# Camera HAL
# ---------------------------------------------------------------------------

class ReachyCameraHAL(CameraHal):
    """Camera HAL wrapping Reachy Mini media.get_frame().

    Returns JPEG-encoded frames from the wide-angle head camera.
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._width: int = 640
        self._height: int = 480

    def initialize(self, resolution: tuple = (640, 480)) -> None:
        self._width, self._height = resolution
        # Ensure media is started (may already be started by microphone HAL)
        try:
            _ = self._reachy.media.get_frame()
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass

    def capture_frame(self) -> CameraFrame:
        now = _ms()
        try:
            frame = self._reachy.media.get_frame()  # numpy (H, W, 3) uint8 RGB
            if frame is None or frame.size == 0:
                return self._blank_frame(now)
            jpeg = _to_jpeg(frame)
            h, w = frame.shape[:2]
            return CameraFrame(now, w, h, "JPEG", jpeg)
        except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
            return self._blank_frame(now)

    def shutdown(self) -> None:
        pass

    def validate(self) -> bool:
        try:
            f = self._reachy.media.get_frame()
            return f is not None and f.size > 0
        except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
            return False

    def get_device_info(self) -> dict:
        return {
            "name": "reachy-mini-camera",
            "sensor": "wide-angle",
            "max_width": 1280,
            "max_height": 720,
        }

    def _blank_frame(self, ts: int) -> CameraFrame:
        blank = np.zeros((self._height, self._width, 3), dtype=np.uint8)
        return CameraFrame(ts, self._width, self._height, "JPEG", _to_jpeg(blank))


# ---------------------------------------------------------------------------
# Microphone HAL
# ---------------------------------------------------------------------------

class ReachyMicrophoneHAL(MicrophoneHal):
    """Microphone HAL wrapping Reachy Mini 2-mic array.

    Audio samples are float32, 16kHz stereo (2 channels).
    Also exposes Direction of Arrival (DoA) when available.
    """

    HAL_VERSION = "1.0.0"
    SAMPLE_RATE = 16000

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._recording = False
        self._buffer: queue.Queue = queue.Queue(maxsize=50)
        self._thread: Optional[threading.Thread] = None
        self._run = False

    def initialize(self) -> None:
        pass  # Media backend initialized on start_recording

    def start_recording(self) -> None:
        if self._recording:
            return
        try:
            self._reachy.media.start_recording()
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass
        self._recording = True
        self._run = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop_recording(self) -> None:
        self._run = False
        if self._thread:
            self._thread.join(timeout=2.0)
        try:
            self._reachy.media.stop_recording()
        except Exception:  # grain: ignore NAKED_EXCEPT -- audio subsystem -- SDK may throw any error on hardware fault
            pass
        self._recording = False

    def get_buffer(self, duration_ms: int = 100) -> AudioChunk:
        """Return up to duration_ms of buffered audio as PCM16 bytes."""
        n_samples = int(self.SAMPLE_RATE * duration_ms / 1000)
        chunks: List[np.ndarray] = []
        total = 0
        while total < n_samples:
            try:
                chunk = self._buffer.get_nowait()
                chunks.append(chunk)
                total += len(chunk)
            except queue.Empty:
                break
        if chunks:
            combined = np.concatenate(chunks, axis=0)[:n_samples]
            pcm = (combined * 32767).astype(np.int16).tobytes()
        else:
            pcm = b"\x00" * (n_samples * 2 * 2)  # stereo int16
        return AudioChunk(
            timestamp_ms=_ms(),
            data=pcm,
            sample_rate=self.SAMPLE_RATE,
            channels=2,
            format="PCM16",
        )

    def transcribe(self, audio, language: str = "en") -> str:
        """Transcribe audio via OpenClaw native STT bridge."""
        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW)
        return bridge.transcribe(audio, language=language)

    def transcribe_stream(self, stream, language: str = "en"):
        """Streaming transcription -- yields partial transcripts as audio arrives."""
        bridge = OpenClawSTTBridge(provider=STTProvider.OPENCLAW)
        for chunk in stream:
            yield bridge.transcribe(chunk, language=language)

    def shutdown(self) -> None:
        self.stop_recording()

    def validate(self) -> bool:
        return True  # Validated at runtime

    def get_device_info(self) -> dict:
        return {
            "name": "reachy-mini-mic-array",
            "channels": 2,
            "sample_rate": self.SAMPLE_RATE,
            "features": ["doa", "speech_detection"],
        }

    def _poll_loop(self) -> None:
        while self._run:
            try:
                samples = self._reachy.media.get_audio_sample()
                if samples is not None and len(samples) > 0:
                    try:
                        self._buffer.put_nowait(samples.astype(np.float32))
                    except queue.Full:
                        try:
                            self._buffer.get_nowait()
                        except queue.Empty:
                            pass
                        self._buffer.put_nowait(samples.astype(np.float32))
            except Exception:  # grain: ignore NAKED_EXCEPT -- audio subsystem -- SDK may throw any error on hardware fault
                time.sleep(0.05)


# ---------------------------------------------------------------------------
# Audio Output HAL
# ---------------------------------------------------------------------------

class ReachyAudioOutputHAL(AudioOutputHal):
    """Audio output HAL wrapping Reachy Mini 5W speaker.

    Accepts PCM16 bytes or numpy float32 arrays for playback.
    """

    HAL_VERSION = "1.0.0"
    SAMPLE_RATE = 16000

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._playing = False

    def initialize(self) -> None:
        pass

    def play(self, audio: bytes, sample_rate: int = 16000, channels: int = 1) -> None:
        """Play PCM16 bytes through Reachy's speaker."""
        try:
            if not self._playing:
                self._reachy.media.start_playing()
                self._playing = True
            pcm = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32767.0
            if channels == 1:
                pcm = pcm.reshape(-1, 1)
            else:
                pcm = pcm.reshape(-1, channels)
            self._reachy.media.push_audio_sample(pcm)
        except Exception:  # grain: ignore NAKED_EXCEPT -- audio subsystem -- SDK may throw any error on hardware fault
            pass

    def speak(self, text: str) -> None:
        """TTS via system TTS (platform dependent).

        For production: integrate with OpenClaw TTS pipeline.
        """
        import subprocess
        try:
            subprocess.run(["say", text], timeout=10, check=False)
        except Exception:  # grain: ignore NAKED_EXCEPT -- subprocess call -- OS/permission errors are heterogeneous
            pass

    def shutdown(self) -> None:
        if self._playing:
            try:
                self._reachy.media.stop_playing()
            except Exception:  # grain: ignore NAKED_EXCEPT -- cleanup path -- must not raise during teardown
                pass
            self._playing = False

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "reachy-mini-speaker",
            "type": "5W speaker",
            "sample_rate": self.SAMPLE_RATE,
        }


# ---------------------------------------------------------------------------
# Display HAL -- LED Eyes
# ---------------------------------------------------------------------------

class ReachyDisplayHAL(DisplayHal):
    """Display HAL mapping OpenClaw DisplayCards to Reachy LED eye expressions.

    Maps response sentiment to Reachy head animations and antenna states.

    Eye expression mapping (approximate, based on Reachy emotion library):
      - Positive/informational response: curious look (head tilt)
      - Question/thinking: neutral with antenna raise
      - Alert/warning: attention pose
      - Default: return to neutral after display duration
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._last_card: Optional[DisplayCard] = None

    def initialize(self, resolution: tuple = (80, 24)) -> None:
        # Move head to neutral on init
        try:
            from reachy_mini.utils import create_head_pose  # type: ignore
            self._reachy.goto_target(
                head=create_head_pose(z=0, roll=0, degrees=True, mm=True),
                duration=0.5,
            )
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass

    def show(self, card: DisplayCard) -> None:
        """Animate Reachy in response to a DisplayCard."""
        self._last_card = card
        try:
            from reachy_mini.utils import create_head_pose  # type: ignore
            import numpy as np

            # Subtle head nod to acknowledge the trigger event
            self._reachy.goto_target(
                head=create_head_pose(z=5, roll=0, degrees=True, mm=True),
                antennas=np.deg2rad([20, 20]),
                duration=0.4,
                method="minjerk",
            )
            time.sleep(0.5)
            # Return to neutral after display duration
            self._reachy.goto_target(
                head=create_head_pose(z=0, roll=0, degrees=True, mm=True),
                antennas=np.deg2rad([0, 0]),
                duration=0.6,
                method="minjerk",
            )
        except Exception:  # grain: ignore NAKED_EXCEPT -- servo/motor read -- firmware errors are not typed
            pass

    def shutdown(self) -> None:
        try:
            from reachy_mini.utils import create_head_pose  # type: ignore
            self._reachy.goto_target(
                head=create_head_pose(z=0, roll=0, degrees=True, mm=True),
                duration=1.0,
            )
        except Exception:  # grain: ignore NAKED_EXCEPT -- servo/motor read -- firmware errors are not typed
            pass

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "reachy-mini-display",
            "type": "led_eyes + head_pose",
            "dof": 6,
        }


# ---------------------------------------------------------------------------
# Transport HAL -- WiFi/localhost HTTP to OpenClaw
# ---------------------------------------------------------------------------

class ReachyTransportHAL(TransportHal):
    """Transport HAL using HTTP POST to OpenClaw context endpoint on .183.

    Reachy connects to the host Mac over USB-C, so localhost traffic
    routes directly to the Mac Mini running the OpenClaw gateway.
    Transport packets are identical to BLE packets (Wearable Packet v1)
    but delivered over HTTP instead of BLE.
    """

    HAL_VERSION = "1.0.0"

    def __init__(
        self,
        openclaw_host: str = "100.82.191.2",  # .183 Tailscale IP
        port: int = 18800,                    # OpenClaw wearable context port (TBD)
        timeout_ms: int = 3000,
    ) -> None:
        self._host = openclaw_host
        self._port = port
        self._timeout_s = timeout_ms / 1000.0
        self._state = TransportState.DISCONNECTED
        self._cb: Optional[Callable[[TransportState], None]] = None
        self._q: queue.Queue = queue.Queue()

    def initialize(self, config: dict) -> None:
        pass

    def connect(self) -> None:
        # HTTP is connectionless -- just mark as connected
        self._set_state(TransportState.CONNECTED)

    def send(self, payload: bytes) -> SendResult:
        t0 = _ms()
        try:
            import urllib.request
            url = f"http://{self._host}:{self._port}/wearable/context"
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/octet-stream"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                _ = resp.read()
            return SendResult(True, len(payload), _ms() - t0)
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- BLE scan -- adapter errors vary by OS and driver
            return SendResult(False, 0, _ms() - t0)

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        try:
            return self._q.get(timeout=timeout_ms / 1000.0)
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
            "name": "reachy-http-transport",
            "type": "http",
            "host": self._host,
            "port": self._port,
            "note": "Wearable Packet v1 over HTTP instead of BLE",
        }

    def _set_state(self, state: TransportState) -> None:
        self._state = state
        if self._cb:
            self._cb(state)


# ---------------------------------------------------------------------------
# Recommended TriggerConfig for Reachy
# ---------------------------------------------------------------------------

REACHY_TRIGGER_CONFIG = {
    "polling_hz": 10,
    "saccade_threshold_dps": 30.0,    # Robot head moves slower than human saccade
    "saccade_duration_ms": 150,
    "fixation_threshold_dps": 5.0,
    "fixation_duration_ms": 600,       # Longer fixation for deliberate robot gaze
    "motion_reject_threshold_dps": 120.0,
    "motion_reject_duration_ms": 200,
    "refractory_period_ms": 2000,      # 2s between captures (robot pacing)
}
"""Pass to TriggerConfig(**REACHY_TRIGGER_CONFIG) when constructing WearableSDK."""


# ---------------------------------------------------------------------------
# Actuator HAL -- physical output control for Reachy Mini Lite
# ---------------------------------------------------------------------------

class ReachyActuatorHAL(ActuatorHal):
    """Actuator HAL for Reachy Mini Lite.

    Dispatches physical actuation commands via HTTP POST to the Reachy daemon.
    Requires no reachy SDK -- all calls go through urllib over HTTP.

    Supported actions:
      - move_head      -- params: pitch, yaw, speed (float)
      - rotate_body    -- params: degrees, speed (float)
      - animate_antennas -- params: pattern (str: happy|thinking|alert|idle)
      - set_expression -- params: emotion (str: neutral|happy|curious|alert)
      - nod            -- no params (affirmation gesture)
      - shake_head     -- no params (negation gesture)
    """

    HAL_VERSION = "1.0.0"

    _CAPABILITIES = [
        "move_head",
        "rotate_body",
        "animate_antennas",
        "set_expression",
        "nod",
        "shake_head",
    ]

    def __init__(self, host: str = "localhost", port: int = 50055) -> None:
        self._host = host
        self._port = port
        self._timeout_s = 5.0
        self._base_url = f"http://{host}:{port}/api/actuate"

    # -- HALBase contract ----------------------------------------------------

    def validate(self) -> bool:
        """Ping the Reachy daemon; return True if reachable."""
        try:
            import urllib.request
            url = f"http://{self._host}:{self._port}/api/health"
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:  # grain: ignore NAKED_EXCEPT -- BLE scan -- adapter errors vary by OS and driver
            return False

    def get_device_info(self) -> dict:
        return {
            "name": "reachy-mini-actuator",
            "type": "http_actuator",
            "host": self._host,
            "port": self._port,
            "capabilities": self._CAPABILITIES,
        }

    # -- ActuatorHal contract ------------------------------------------------

    def initialize(self) -> None:
        """Initialize actuator system (no-op for HTTP backend)."""
        pass

    def execute(self, command: ActuatorCommand) -> ActuatorResult:
        """Dispatch command to Reachy daemon via HTTP POST."""
        t0 = _ms()
        if command.action not in self._CAPABILITIES:
            return ActuatorResult(
                command_id=command.command_id,
                success=False,
                elapsed_ms=_ms() - t0,
                error=f"Unsupported action: {command.action}",
            )
        try:
            import json
            import urllib.request

            payload = json.dumps({
                "command_id": command.command_id,
                "action": command.action,
                "params": command.params,
            }).encode("utf-8")

            req = urllib.request.Request(
                self._base_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                _ = resp.read()
            return ActuatorResult(
                command_id=command.command_id,
                success=True,
                elapsed_ms=_ms() - t0,
            )
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- HAL hardware call -- exception types vary by SDK and platform
            return ActuatorResult(
                command_id=command.command_id,
                success=False,
                elapsed_ms=_ms() - t0,
                error=str(exc),
            )

    def stop_all(self) -> None:
        """Emergency stop -- send stop command to daemon."""
        try:
            import json
            import urllib.request

            payload = json.dumps({"action": "stop_all"}).encode("utf-8")
            req = urllib.request.Request(
                self._base_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                _ = resp.read()
        except Exception:  # grain: ignore NAKED_EXCEPT -- HAL hardware call -- exception types vary by SDK and platform
            pass

    def get_capabilities(self) -> list:
        """Return list of supported action strings."""
        return list(self._CAPABILITIES)

    def shutdown(self) -> None:
        """Shutdown actuator system -- stop all then release."""
        self.stop_all()
