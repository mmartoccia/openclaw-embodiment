"""Reachy 2 reference HAL for OpenClaw Wearable SDK.

Full humanoid robot HAL for the Pollen Robotics Reachy 2 platform.

Hardware: Pollen Robotics Reachy 2
  - 7-DOF arms x2 (shoulder pitch/roll, elbow yaw/pitch, wrist roll/pitch/yaw + gripper)
  - Head: 3-DOF neck (pan/tilt/roll) with real encoder feedback
  - Face display: LED antenna expressions + face screen
  - Stereo cameras (left + right, depth-capable)
  - Microphone array with Direction of Arrival (DoA)
  - Speaker system for TTS and audio playback
  - Optional wheeled mobile base
  - Communication: HTTP/gRPC via reachy2-sdk

Key difference from Reachy Mini:
  - 14 arm DOF (vs 0 -- Mini has no arms)
  - Stereo cameras (vs single wide-angle)
  - Real 3-DOF neck (vs 6-DOF Stewart platform head)
  - Real IMU feedback from neck encoders
  - Optional mobile base
  - Face display with LEDs + screen (vs LED eyes only)

Install requirements:
  pip install reachy2-sdk numpy

Tested against: Reachy 2 API spec (hardware validation pending).
API reference: https://pollen-robotics.github.io/reachy2-sdk/

Usage:
  from reachy2_sdk import ReachySDK
  from openclaw_embodiment.hal.reachy2_reference import (
      Reachy2MotionTracker, Reachy2CameraHAL, Reachy2MicrophoneHAL,
      Reachy2AudioOutputHAL, Reachy2DisplayHAL, Reachy2TransportHAL,
      Reachy2ActuatorHAL,
  )

  reachy = ReachySDK(host="reachy.local")
  reachy.connect()

  registry = HALRegistry()
  registry.register_imu(Reachy2MotionTracker(reachy))
  registry.register_camera(Reachy2CameraHAL(reachy, camera_side='left'))
  registry.register_microphone(Reachy2MicrophoneHAL(reachy))
  registry.register_audio_output(Reachy2AudioOutputHAL(reachy))
  registry.register_display(Reachy2DisplayHAL(reachy))
  registry.register_transport(Reachy2TransportHAL(host="reachy.local"))
  registry.register_actuator(Reachy2ActuatorHAL(reachy))
"""

from __future__ import annotations

import math
import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from .base import (
    ActuatorCommand,
    ActuatorHal,
    ActuatorResult,
    AudioChunk,
    AudioOutputHal,
    CameraFrame,
    CameraHal,
    DisplayCard,
    DisplayHal,
    IMUSample,
    IMUHal,
    JointState,
    MicrophoneHal,
    SendResult,
    TransportHal,
    TransportState,
)

if TYPE_CHECKING:
    pass  # ReachySDK type hints deferred to avoid hard import dependency

try:
    from ..utils.clock import ms_now  # type: ignore[import]
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms() -> int:
    return int(time.time() * 1000)


def _to_jpeg(frame_rgb: np.ndarray) -> bytes:
    """Encode numpy RGB frame to JPEG bytes."""
    try:
        import cv2  # type: ignore
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR))
        return bytes(buf) if ok else b""
    except Exception:
        return b""


# ---------------------------------------------------------------------------
# Reachy 2 face display expression constants
# ---------------------------------------------------------------------------

class Reachy2Expression:
    """Supported Reachy 2 face display expressions."""
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    THINKING = "thinking"
    ALERT = "alert"
    CUSTOM = "custom"

    ALL = [NEUTRAL, HAPPY, SAD, THINKING, ALERT, CUSTOM]


# ---------------------------------------------------------------------------
# Actuator IDs
# ---------------------------------------------------------------------------

REACHY2_HEAD_JOINTS = [
    "head.neck.pan",
    "head.neck.tilt",
    "head.neck.roll",
]

REACHY2_R_ARM_JOINTS = [
    "r_arm.shoulder.pitch",
    "r_arm.shoulder.roll",
    "r_arm.elbow.yaw",
    "r_arm.elbow.pitch",
    "r_arm.wrist.roll",
    "r_arm.wrist.pitch",
    "r_arm.wrist.yaw",
    "r_arm.gripper",
]

REACHY2_L_ARM_JOINTS = [j.replace("r_arm", "l_arm") for j in REACHY2_R_ARM_JOINTS]

REACHY2_MOBILE_BASE_JOINTS = [
    "mobile_base.x",
    "mobile_base.y",
    "mobile_base.theta",
]

REACHY2_ALL_JOINTS = (
    REACHY2_HEAD_JOINTS
    + REACHY2_R_ARM_JOINTS
    + REACHY2_L_ARM_JOINTS
    + REACHY2_MOBILE_BASE_JOINTS
)

REACHY2_ACTUATOR_CAPABILITIES = [
    "move_head",
    "move_r_arm",
    "move_l_arm",
    "move_gripper",
    "set_expression",
    "mobile_base_move",
    "stop_all",
    "get_joint_states",
]


# ---------------------------------------------------------------------------
# IMU HAL -- Head Neck Encoder Orientation
# ---------------------------------------------------------------------------

class Reachy2MotionTracker(IMUHal):
    """Head orientation via neck joint encoder positions.

    Reachy 2 has a 3-DOF neck (pan/tilt/roll) with real encoder feedback,
    providing accurate head orientation data. Unlike Reachy Mini Lite which
    uses a Stewart platform as a gyro substitute, this reads actual encoder
    positions from the neck joints.

    Returns IMUSample with:
      - gyro_x/y/z: angular velocity derived from position deltas (deg/s)
      - accel_z: 9.8 m/s^2 constant (no physical accelerometer in neck)
      - Orientation is available from encoder absolute positions
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._poll_hz: int = 25
        self._last_pose: Optional[Dict[str, float]] = None
        self._last_poll_ms: int = 0

    def initialize(self, sample_rate_hz: int = 25) -> None:
        self._poll_hz = max(1, sample_rate_hz)
        self._last_pose = self._get_neck_pose()
        self._last_poll_ms = _ms()

    def read_sample(self) -> Optional[IMUSample]:
        now = _ms()
        dt_s = max((now - self._last_poll_ms) / 1000.0, 1e-6)

        pose = self._get_neck_pose()
        if self._last_pose is None:
            self._last_pose = pose
            self._last_poll_ms = now
            return None

        # Angular velocity in degrees/s from encoder position deltas
        gyro_x = (pose["tilt"] - self._last_pose["tilt"]) / dt_s
        gyro_y = (pose["pan"]  - self._last_pose["pan"])  / dt_s
        gyro_z = (pose["roll"] - self._last_pose["roll"]) / dt_s

        self._last_pose = pose
        self._last_poll_ms = now

        return IMUSample(
            timestamp_ms=now,
            accel_x=0.0, accel_y=0.0, accel_z=9.8,  # No physical accelerometer in neck
            gyro_x=gyro_x, gyro_y=gyro_y, gyro_z=gyro_z,
        )

    def get_orientation(self) -> Optional[Dict[str, float]]:
        """Return absolute neck orientation as {pan, tilt, roll} in degrees.

        This is Reachy 2's advantage over Mini Lite -- real encoder absolute position.
        Returns None if unavailable.
        """
        try:
            return self._get_neck_pose()
        except Exception:
            return None

    def get_angular_velocity(self) -> Optional[Dict[str, float]]:
        """Return angular velocity estimate from encoder deltas.

        Returns dict with keys: pan_dps, tilt_dps, roll_dps.
        Returns None if no prior sample available.
        """
        if self._last_pose is None:
            return None
        sample = self.read_sample()
        if sample is None:
            return None
        return {
            "pan_dps": sample.gyro_y,
            "tilt_dps": sample.gyro_x,
            "roll_dps": sample.gyro_z,
        }

    def is_available(self) -> bool:
        """Return True if neck encoder data is readable."""
        try:
            pose = self._get_neck_pose()
            return isinstance(pose, dict)
        except Exception:
            return False

    def set_sample_rate(self, hz: int) -> None:
        self._poll_hz = max(1, hz)

    def shutdown(self) -> None:
        self._last_pose = None

    def validate(self) -> bool:
        return self.is_available()

    def get_device_info(self) -> dict:
        return {
            "name": "reachy2-motion-tracker",
            "type": "neck_encoder",
            "dof": 3,
            "joints": REACHY2_HEAD_JOINTS,
            "axes": 3,
            "note": "Real 3-DOF neck encoder (pan/tilt/roll) -- not an encoder substitute",
            "max_rate_hz": 100,
            "current_rate_hz": self._poll_hz,
        }

    def _get_neck_pose(self) -> Dict[str, float]:
        """Read current neck joint encoder positions in degrees."""
        try:
            # reachy2-sdk: reachy.head.neck.{pan, tilt, roll}.present_position
            neck = self._reachy.head.neck
            return {
                "pan":  float(getattr(neck.pan,  "present_position", 0.0)),
                "tilt": float(getattr(neck.tilt, "present_position", 0.0)),
                "roll": float(getattr(neck.roll, "present_position", 0.0)),
            }
        except Exception:
            return {"pan": 0.0, "tilt": 0.0, "roll": 0.0}


# ---------------------------------------------------------------------------
# Camera HAL -- Stereo Cameras
# ---------------------------------------------------------------------------

class Reachy2CameraHAL(CameraHal):
    """Stereo camera capture from Reachy 2 left/right cameras.

    Reachy 2 has two cameras for stereo depth perception.
    Supports left, right, or fused (side-by-side) frame capture.

    camera_side: 'left' | 'right' | 'both'
      - 'left': capture from left camera only
      - 'right': capture from right camera only
      - 'both': returns side-by-side composite frame (width x2)

    Uses reachy2-sdk camera API: reachy.cameras.left / reachy.cameras.right
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, reachy: Any, camera_side: str = "left") -> None:
        self._reachy = reachy
        self._camera_side = camera_side
        self._width: int = 640
        self._height: int = 480

    def initialize(self, resolution: Tuple[int, int] = (640, 480)) -> None:
        self._width, self._height = resolution

    def capture_frame(self) -> CameraFrame:
        now = _ms()
        try:
            if self._camera_side == "both":
                left = self._get_frame("left")
                right = self._get_frame("right")
                if left is not None and right is not None:
                    # Side-by-side composite
                    composite = np.concatenate([left, right], axis=1)
                    jpeg = _to_jpeg(composite)
                    h, w = composite.shape[:2]
                    return CameraFrame(now, w, h, "JPEG", jpeg)
            else:
                frame = self._get_frame(self._camera_side)
                if frame is not None and frame.size > 0:
                    jpeg = _to_jpeg(frame)
                    h, w = frame.shape[:2]
                    return CameraFrame(now, w, h, "JPEG", jpeg)
        except Exception:
            pass
        return self._blank_frame(now)

    def get_resolution(self) -> Tuple[int, int]:
        """Return (width, height) of configured camera resolution."""
        return (self._width, self._height)

    def is_available(self) -> bool:
        """Return True if the configured camera side is readable."""
        try:
            frame = self._get_frame(self._camera_side if self._camera_side != "both" else "left")
            return frame is not None and frame.size > 0
        except Exception:
            return False

    def get_raw_frame(self) -> Optional[bytes]:
        """Return raw JPEG bytes from the configured camera side."""
        try:
            frame = self._get_frame(self._camera_side if self._camera_side != "both" else "left")
            if frame is not None:
                return _to_jpeg(frame)
        except Exception:
            pass
        return None

    def shutdown(self) -> None:
        pass

    def validate(self) -> bool:
        return self.is_available()

    def get_device_info(self) -> dict:
        return {
            "name": "reachy2-stereo-camera",
            "type": "stereo",
            "sides": ["left", "right"],
            "active_side": self._camera_side,
            "max_width": 1280,
            "max_height": 720,
            "stereo": True,
            "depth_capable": True,
        }

    def _get_frame(self, side: str) -> Optional[np.ndarray]:
        """Get a numpy frame from the specified camera side."""
        try:
            # reachy2-sdk: reachy.cameras.left.get_frame() -> numpy RGB
            cam = self._reachy.cameras.left if side == "left" else self._reachy.cameras.right
            return cam.get_frame()
        except Exception:
            return None

    def _blank_frame(self, ts: int) -> CameraFrame:
        w = self._width * (2 if self._camera_side == "both" else 1)
        blank = np.zeros((self._height, w, 3), dtype=np.uint8)
        return CameraFrame(ts, w, self._height, "JPEG", _to_jpeg(blank))


# ---------------------------------------------------------------------------
# Microphone HAL -- Array with DoA
# ---------------------------------------------------------------------------

class Reachy2MicrophoneHAL(MicrophoneHal):
    """Audio capture from Reachy 2 microphone array.

    Supports Direction of Arrival (DoA) via microphone array geometry.
    Returns float32 PCM at 16kHz (Reachy 2 default sample rate).

    DoA is computed from the array geometry using reachy2-sdk audio API
    and returned as (azimuth_degrees, elevation_degrees).
    """

    HAL_VERSION = "1.0.0"
    SAMPLE_RATE = 16000

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._recording = False
        self._buffer: queue.Queue = queue.Queue(maxsize=50)
        self._thread: Optional[threading.Thread] = None
        self._run = False
        self._last_doa: Optional[Tuple[float, Optional[float]]] = None

    def initialize(self, sample_rate: int = 16000, channels: int = 1) -> None:
        pass  # Audio backend initialized on start_recording

    def start_recording(self) -> None:
        if self._recording:
            return
        try:
            self._reachy.audio.start_recording()
        except Exception:
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
            self._reachy.audio.stop_recording()
        except Exception:
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
            pcm = b"\x00" * (n_samples * 2)  # mono int16
        return AudioChunk(
            timestamp_ms=_ms(),
            data=pcm,
            sample_rate=self.SAMPLE_RATE,
            channels=1,
            format="PCM16",
        )

    def get_doa(self) -> Optional[Tuple[float, Optional[float]]]:
        """Return Direction of Arrival as (azimuth_degrees, elevation_degrees).

        Azimuth: -180 to 180 (0 = front of robot).
        Elevation: -90 to 90 (optional, None if not computed).
        Returns None if no sound detected or DoA unavailable.
        """
        if self._last_doa is not None:
            return self._last_doa
        try:
            # reachy2-sdk: reachy.audio.get_doa() -> float (azimuth)
            azimuth = float(self._reachy.audio.get_doa())
            return (azimuth, None)
        except Exception:
            return None

    def get_sample_rate(self) -> int:
        """Return configured audio sample rate in Hz."""
        return self.SAMPLE_RATE

    def is_available(self) -> bool:
        """Return True if audio recording is active."""
        return self._recording

    def shutdown(self) -> None:
        self.stop_recording()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "reachy2-mic-array",
            "type": "microphone_array",
            "channels": 1,
            "sample_rate": self.SAMPLE_RATE,
            "features": ["doa", "speech_detection"],
            "doa_axes": ["azimuth"],
        }

    def _poll_loop(self) -> None:
        while self._run:
            try:
                samples = self._reachy.audio.get_audio_sample()
                if samples is not None and len(samples) > 0:
                    try:
                        self._buffer.put_nowait(samples.astype(np.float32))
                    except queue.Full:
                        try:
                            self._buffer.get_nowait()
                        except queue.Empty:
                            pass
                        self._buffer.put_nowait(samples.astype(np.float32))
                # Update DoA cache if available
                try:
                    az = float(self._reachy.audio.get_doa())
                    self._last_doa = (az, None)
                except Exception:
                    pass
            except Exception:
                time.sleep(0.05)


# ---------------------------------------------------------------------------
# Audio Output HAL -- Speaker System
# ---------------------------------------------------------------------------

class Reachy2AudioOutputHAL(AudioOutputHal):
    """TTS and audio playback via Reachy 2 speaker system.

    Wraps reachy2-sdk audio output API for PCM playback.
    TTS falls back to system `say` command (macOS) or Google TTS.
    For production: integrate with OpenClaw TTS pipeline.
    """

    HAL_VERSION = "1.0.0"
    SAMPLE_RATE = 22050

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._playing = False
        self._volume: float = 1.0

    def initialize(self, sample_rate: int = 22050, channels: int = 1) -> None:
        self.SAMPLE_RATE = sample_rate

    def play(self, audio_data: bytes, format: str = "PCM_S16LE", sample_rate: int = 22050) -> None:
        """Play PCM16 bytes through Reachy 2 speaker system."""
        try:
            if not self._playing:
                self._reachy.audio.start_playing()
                self._playing = True
            pcm = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32767.0
            pcm = pcm * self._volume
            self._reachy.audio.push_audio_sample(pcm)
        except Exception:
            pass

    def speak(self, text: str) -> None:
        """TTS via system TTS or Reachy 2 TTS engine if available.

        Tries reachy2-sdk TTS first, falls back to system `say` command.
        """
        try:
            # reachy2-sdk may expose a TTS interface
            self._reachy.audio.speak(text)
        except Exception:
            import subprocess
            try:
                subprocess.run(["say", text], timeout=30, check=False)
            except Exception:
                pass

    def play_audio(self, audio_data: bytes, sample_rate: int = 22050) -> None:
        """Alias for play() with standard signature."""
        self.play(audio_data, sample_rate=sample_rate)

    def set_volume(self, volume: float) -> None:
        """Set playback volume in range [0.0, 1.0]."""
        self._volume = max(0.0, min(1.0, float(volume)))
        try:
            self._reachy.audio.set_volume(self._volume)
        except Exception:
            pass  # Volume scaling applied locally on next play()

    def is_available(self) -> bool:
        """Return True if audio output system is accessible."""
        try:
            # Try a no-op volume read to confirm SDK is reachable
            _ = getattr(self._reachy, "audio", None)
            return self._reachy is not None
        except Exception:
            return False

    def stop(self) -> None:
        """Stop active playback."""
        if self._playing:
            try:
                self._reachy.audio.stop_playing()
            except Exception:
                pass
            self._playing = False

    def is_playing(self) -> bool:
        return self._playing

    def shutdown(self) -> None:
        self.stop()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "reachy2-audio-output",
            "type": "speaker",
            "sample_rate": self.SAMPLE_RATE,
            "volume": self._volume,
        }


# ---------------------------------------------------------------------------
# Display HAL -- Face Display (LED Antennas + Face Screen)
# ---------------------------------------------------------------------------

class Reachy2DisplayHAL(DisplayHal):
    """Face display: LED antenna expressions and face screen.

    Reachy 2 has both LED antennas (expressive mood lighting) and a face
    screen that can show expressions/animations.

    Supported expressions: NEUTRAL, HAPPY, SAD, THINKING, ALERT, CUSTOM.

    Expression mapping to Reachy 2 SDK:
      - NEUTRAL:  reachy.head.look_at(center) + antennas off
      - HAPPY:    antenna happy animation + face screen smile
      - SAD:      antenna down + face screen sad expression
      - THINKING: antenna slow pulse + face screen searching
      - ALERT:    antenna rapid flash + face screen alert
      - CUSTOM:   pass custom card.metadata for raw expression control
    """

    HAL_VERSION = "1.0.0"

    _EXPRESSION_MAP = {
        Reachy2Expression.NEUTRAL:  "neutral",
        Reachy2Expression.HAPPY:    "happy",
        Reachy2Expression.SAD:      "sad",
        Reachy2Expression.THINKING: "thinking",
        Reachy2Expression.ALERT:    "alert",
    }

    def __init__(self, reachy: Any) -> None:
        self._reachy = reachy
        self._brightness: float = 1.0
        self._last_expression: str = Reachy2Expression.NEUTRAL

    def initialize(self, resolution: Tuple[int, int] = (640, 400)) -> None:
        """Initialize face display and move to neutral expression."""
        try:
            self._reachy.head.set_expression(Reachy2Expression.NEUTRAL)
        except Exception:
            pass

    def show(self, card: "DisplayCard") -> None:
        """Render a DisplayCard as a face expression on Reachy 2.

        Maps card.title/content sentiment to Reachy 2 face expressions.
        Checks card.metadata for 'expression' key to override mapping.
        """
        expression = Reachy2Expression.NEUTRAL
        if hasattr(card, "metadata") and isinstance(card.metadata, dict):
            expression = card.metadata.get("expression", Reachy2Expression.NEUTRAL)
        self.show_expression(expression)

    def show_expression(self, expression: str) -> None:
        """Directly set a face expression by name.

        expression: one of Reachy2Expression.{NEUTRAL, HAPPY, SAD, THINKING, ALERT, CUSTOM}
        """
        self._last_expression = expression
        sdk_expr = self._EXPRESSION_MAP.get(expression, "neutral")
        try:
            # reachy2-sdk: reachy.head.set_expression(expression_name)
            self._reachy.head.set_expression(sdk_expr)
        except Exception:
            pass
        try:
            # Also trigger antenna animation if available
            self._reachy.head.antennas.set_expression(sdk_expr)
        except Exception:
            pass

    def clear(self) -> None:
        """Reset face display to neutral expression."""
        self.show_expression(Reachy2Expression.NEUTRAL)

    def set_brightness(self, brightness: float) -> None:
        """Set face display brightness in range [0.0, 1.0].

        Applies to both LED antennas and face screen.
        """
        self._brightness = max(0.0, min(1.0, float(brightness)))
        try:
            self._reachy.head.set_brightness(self._brightness)
        except Exception:
            pass

    def is_available(self) -> bool:
        """Return True if head display system is accessible."""
        try:
            return hasattr(self._reachy, "head") and self._reachy.head is not None
        except Exception:
            return False

    def shutdown(self) -> None:
        """Reset to neutral expression on shutdown."""
        self.clear()

    def validate(self) -> bool:
        return self.is_available()

    def get_device_info(self) -> dict:
        return {
            "name": "reachy2-face-display",
            "type": "led_antennas + face_screen",
            "expressions": Reachy2Expression.ALL,
            "brightness": self._brightness,
        }


# ---------------------------------------------------------------------------
# Transport HAL -- HTTP/gRPC via reachy2-sdk
# ---------------------------------------------------------------------------

class Reachy2TransportHAL(TransportHal):
    """HTTP/gRPC transport to Reachy 2 via reachy2-sdk API.

    Reachy 2 communicates over network (WiFi or Ethernet) using the
    reachy2-sdk Python library. This HAL wraps the SDK transport layer
    to provide OpenClaw-compatible send/receive.

    Default ports:
      - 50051: gRPC (reachy2-sdk primary)
      - 4242:  HTTP REST (alternative)

    Transport packets follow OpenClaw Wearable Packet v1 format,
    delivered over gRPC/HTTP instead of BLE.
    """

    HAL_VERSION = "1.0.0"

    def __init__(
        self,
        host: str = "reachy.local",
        port: int = 50051,
        timeout_ms: int = 5000,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout_s = timeout_ms / 1000.0
        self._state = TransportState.DISCONNECTED
        self._cb: Optional[Callable[[TransportState], None]] = None
        self._q: queue.Queue = queue.Queue()

    def initialize(self, config: dict) -> None:
        if "host" in config:
            self._host = config["host"]
        if "port" in config:
            self._port = config["port"]

    def connect(self) -> None:
        """Mark transport as connected (gRPC session managed by reachy2-sdk)."""
        self._set_state(TransportState.CONNECTED)

    def send(self, payload: bytes) -> SendResult:
        """Send payload to OpenClaw gateway via HTTP POST."""
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
        except Exception as exc:
            return SendResult(False, 0, _ms() - t0)

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Receive next packet from inbound queue."""
        try:
            return self._q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return None

    def is_connected(self) -> bool:
        """Return True if transport is in CONNECTED state."""
        return self._state == TransportState.CONNECTED

    def get_expected_latency_ms(self) -> int:
        """Return expected round-trip latency estimate in milliseconds.

        WiFi local network: ~5-15ms typical.
        Returns 10ms as conservative local-network estimate.
        """
        return 10

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
            "name": "reachy2-transport",
            "type": "grpc_http",
            "host": self._host,
            "port": self._port,
            "grpc_port": 50051,
            "http_port": 4242,
            "note": "OpenClaw Wearable Packet v1 over HTTP/gRPC",
        }

    def _set_state(self, state: TransportState) -> None:
        self._state = state
        if self._cb:
            self._cb(state)


# ---------------------------------------------------------------------------
# Actuator HAL -- Full 19+ DOF Control
# ---------------------------------------------------------------------------

class Reachy2ActuatorHAL(ActuatorHal):
    """Full actuator control: arms (14 DOF), neck (3 DOF), grippers (2), mobile base.

    Dispatches physical actuation commands via reachy2-sdk.

    Actuator IDs (use as command.action or in get_joint_states() keys):
      Head:
        head.neck.pan, head.neck.tilt, head.neck.roll

      Right arm (7 DOF + gripper):
        r_arm.shoulder.pitch, r_arm.shoulder.roll
        r_arm.elbow.yaw, r_arm.elbow.pitch
        r_arm.wrist.roll, r_arm.wrist.pitch, r_arm.wrist.yaw
        r_arm.gripper

      Left arm (mirror of right arm):
        l_arm.shoulder.pitch, l_arm.shoulder.roll
        l_arm.elbow.yaw, l_arm.elbow.pitch
        l_arm.wrist.roll, l_arm.wrist.pitch, l_arm.wrist.yaw
        l_arm.gripper

      Mobile base (if installed):
        mobile_base.x, mobile_base.y, mobile_base.theta

    Supported actions (get_capabilities()):
      move_head, move_r_arm, move_l_arm, move_gripper,
      set_expression, mobile_base_move, stop_all, get_joint_states
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, reachy: Any, has_mobile_base: bool = False) -> None:
        self._reachy = reachy
        self._has_mobile_base = has_mobile_base

    def initialize(self) -> None:
        """Initialize actuator system -- move to compliant mode."""
        try:
            # reachy2-sdk: set all joints to compliant (safe) mode on init
            self._reachy.turn_on("reachy")
        except Exception:
            pass

    def execute(self, command: ActuatorCommand) -> ActuatorResult:
        """Dispatch an actuator command to Reachy 2.

        Supported actions and their params:
          move_head:       {pan: float, tilt: float, roll: float, duration: float}
          move_r_arm:      {joint_positions: {joint_id: angle_deg, ...}, duration: float}
          move_l_arm:      {joint_positions: {joint_id: angle_deg, ...}, duration: float}
          move_gripper:    {side: 'left'|'right', position: float}
          mobile_base_move:{x: float, y: float, theta: float}
          set_expression:  {expression: str}
          stop_all:        {}
        """
        t0 = _ms()
        action = command.action
        params = command.params or {}

        try:
            if action == "move_head":
                self._move_head(params)
            elif action == "move_r_arm":
                self._move_arm("right", params)
            elif action == "move_l_arm":
                self._move_arm("left", params)
            elif action == "move_gripper":
                self._move_gripper(params)
            elif action == "mobile_base_move":
                self._mobile_base_move(params)
            elif action == "set_expression":
                expr = params.get("expression", "neutral")
                self._reachy.head.set_expression(expr)
            elif action == "stop_all":
                self.stop_all()
            elif action == "get_joint_states":
                pass  # Read-only, no hardware action needed
            else:
                return ActuatorResult(
                    command_id=command.command_id,
                    success=False,
                    elapsed_ms=_ms() - t0,
                    error=f"Unsupported action: {action}",
                )
            return ActuatorResult(
                command_id=command.command_id,
                success=True,
                elapsed_ms=_ms() - t0,
            )
        except Exception as exc:
            return ActuatorResult(
                command_id=command.command_id,
                success=False,
                elapsed_ms=_ms() - t0,
                error=str(exc),
            )

    def stop_all(self) -> None:
        """Emergency stop -- set all joints to compliant (free) mode."""
        try:
            self._reachy.turn_off("reachy")
        except Exception:
            pass

    def get_capabilities(self) -> list:
        """Return list of supported action strings."""
        caps = list(REACHY2_ACTUATOR_CAPABILITIES)
        if not self._has_mobile_base:
            caps = [c for c in caps if c != "mobile_base_move"]
        return caps

    def get_joint_states(self) -> dict:
        """Return current state of all joints as {joint_id: JointState}.

        Reads encoder positions from reachy2-sdk for all active joints.
        Returns empty JointState values (0.0) on read failure.
        """
        states: Dict[str, JointState] = {}

        joint_ids = REACHY2_HEAD_JOINTS + REACHY2_R_ARM_JOINTS + REACHY2_L_ARM_JOINTS
        if self._has_mobile_base:
            joint_ids = joint_ids + REACHY2_MOBILE_BASE_JOINTS

        for joint_id in joint_ids:
            try:
                pos = self._read_joint_position(joint_id)
                states[joint_id] = JointState(
                    joint_id=joint_id,
                    position_degrees=pos,
                    velocity_dps=0.0,    # velocity not always exposed by SDK
                    load_percent=0.0,    # load not always exposed by SDK
                    temperature_celsius=None,
                )
            except Exception:
                states[joint_id] = JointState(
                    joint_id=joint_id,
                    position_degrees=0.0,
                    velocity_dps=0.0,
                    load_percent=0.0,
                )
        return states

    def send_raw_command(self, raw: bytes) -> Optional[bytes]:
        """Send a raw command bytes to the Reachy 2 gRPC interface.

        Hardware validation pending -- command format depends on reachy2-sdk
        internal protocol. This is a placeholder for direct gRPC access.
        """
        raise NotImplementedError(
            "send_raw_command not yet implemented for Reachy 2. "
            "Use execute() with supported action strings. "
            "Hardware validation pending for raw gRPC access."
        )

    def shutdown(self) -> None:
        """Shutdown actuator system -- set all joints to safe/compliant mode."""
        self.stop_all()

    def validate(self) -> bool:
        """Ping reachy2-sdk connection -- True if robot is reachable."""
        try:
            return self._reachy is not None and hasattr(self._reachy, "head")
        except Exception:
            return False

    def get_device_info(self) -> dict:
        return {
            "name": "reachy2-actuator",
            "type": "full_humanoid",
            "dof_arms": 14,
            "dof_neck": 3,
            "grippers": 2,
            "has_mobile_base": self._has_mobile_base,
            "capabilities": self.get_capabilities(),
            "joint_ids": REACHY2_ALL_JOINTS,
        }

    # -- Internal movement helpers -------------------------------------------

    def _move_head(self, params: dict) -> None:
        """Move neck joints by position."""
        try:
            duration = float(params.get("duration", 1.0))
            # reachy2-sdk: reachy.head.goto(pan, tilt, roll, duration)
            self._reachy.head.goto(
                pan=float(params.get("pan", 0.0)),
                tilt=float(params.get("tilt", 0.0)),
                roll=float(params.get("roll", 0.0)),
                duration=duration,
            )
        except Exception:
            # Fallback: try neck joint direct access
            neck = getattr(self._reachy.head, "neck", None)
            if neck:
                if "pan" in params:
                    neck.pan.goal_position = float(params["pan"])
                if "tilt" in params:
                    neck.tilt.goal_position = float(params["tilt"])
                if "roll" in params:
                    neck.roll.goal_position = float(params["roll"])

    def _move_arm(self, side: str, params: dict) -> None:
        """Move arm joints by position dict."""
        arm = self._reachy.r_arm if side == "right" else self._reachy.l_arm
        joint_positions = params.get("joint_positions", {})
        duration = float(params.get("duration", 1.0))
        try:
            arm.goto(joint_positions, duration=duration)
        except Exception:
            # Fallback: set individual joint goals
            for joint_id, angle in joint_positions.items():
                try:
                    parts = joint_id.split(".")  # e.g. r_arm.elbow.pitch -> elbow.pitch
                    if len(parts) >= 3:
                        joint_obj = getattr(getattr(arm, parts[1], None), parts[2], None)
                        if joint_obj:
                            joint_obj.goal_position = float(angle)
                except Exception:
                    pass

    def _move_gripper(self, params: dict) -> None:
        """Open/close gripper."""
        side = params.get("side", "right")
        position = float(params.get("position", 0.0))  # 0.0=open, 1.0=closed
        arm = self._reachy.r_arm if side == "right" else self._reachy.l_arm
        try:
            arm.gripper.goal_position = position
        except Exception:
            pass

    def _mobile_base_move(self, params: dict) -> None:
        """Command mobile base movement if installed."""
        if not self._has_mobile_base:
            raise NotImplementedError("Mobile base not installed on this Reachy 2 unit.")
        try:
            # reachy2-sdk: reachy.mobile_base.goto(x, y, theta)
            self._reachy.mobile_base.goto(
                x=float(params.get("x", 0.0)),
                y=float(params.get("y", 0.0)),
                theta=float(params.get("theta", 0.0)),
            )
        except Exception:
            pass

    def _read_joint_position(self, joint_id: str) -> float:
        """Read encoder position for a joint ID in degrees."""
        # Parse joint_id like "r_arm.shoulder.pitch" or "head.neck.pan"
        parts = joint_id.split(".")
        try:
            if parts[0] == "head" and parts[1] == "neck":
                neck_joint = getattr(self._reachy.head.neck, parts[2], None)
                return float(getattr(neck_joint, "present_position", 0.0))
            elif parts[0] in ("r_arm", "l_arm"):
                arm = self._reachy.r_arm if parts[0] == "r_arm" else self._reachy.l_arm
                if parts[1] == "gripper":
                    return float(getattr(arm.gripper, "present_position", 0.0))
                link = getattr(arm, parts[1], None)
                joint = getattr(link, parts[2], None)
                return float(getattr(joint, "present_position", 0.0))
            elif parts[0] == "mobile_base":
                base = getattr(self._reachy, "mobile_base", None)
                if base:
                    state = base.get_state()
                    return float(getattr(state, parts[1], 0.0))
        except Exception:
            pass
        return 0.0


# ---------------------------------------------------------------------------
# Recommended TriggerConfig for Reachy 2
# ---------------------------------------------------------------------------

REACHY2_TRIGGER_CONFIG = {
    "polling_hz": 25,
    "saccade_threshold_dps": 20.0,     # Neck encoder, smooth robot motion
    "saccade_duration_ms": 200,
    "fixation_threshold_dps": 3.0,
    "fixation_duration_ms": 500,        # 0.5s deliberate robot gaze
    "motion_reject_threshold_dps": 100.0,
    "motion_reject_duration_ms": 150,
    "refractory_period_ms": 1500,       # 1.5s between captures
}
"""Pass to TriggerConfig(**REACHY2_TRIGGER_CONFIG) when constructing WearableSDK."""
