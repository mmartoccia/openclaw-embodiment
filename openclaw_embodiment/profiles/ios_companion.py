"""iOS Companion Profile -- iPhone as OpenClaw embodiment sensor node.

Architecture:
  iPhone (companion app) → WiFi/BLE → Python SDK receiver → Agent context

The iPhone companion app (not in this SDK) sends sensor data over local network.
This module defines: the protocol, the Python receiver, and the profile spec.

Wire protocol:
  - Transport: HTTP/1.1 over local WiFi (port 18800)
  - Auth: HMAC-SHA256 signed envelope (X-OpenClaw-Signature header)
  - Compression: gzip for camera frames; raw JSON for IMU/audio/location
  - Max rates: 30fps camera, 50Hz IMU, continuous audio streaming

Companion app responsibilities (out of scope here):
  - Capture sensor data via AVFoundation / CoreMotion / CoreLocation
  - Package as iOSSensorPayload JSON and POST to receiver endpoints
  - Sign each request with shared HMAC secret
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import json
import logging
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from ..hal.base import (
    AudioChunk,
    CameraFrame,
    CameraHal,
    HALBase,
    IMUHal,
    IMUSample,
    MicrophoneHal,
    SendResult,
    TransportHal,
    TransportState,
)

logger = logging.getLogger(__name__)

# ── Wire protocol constants ──────────────────────────────────────────────────

COMPANION_PORT: int = 18800
FORMAT_VERSION: str = "1.0"
SIGNATURE_HEADER: str = "X-OpenClaw-Signature"
MAX_CAMERA_FPS: int = 30
MAX_IMU_HZ: int = 50

VALID_SENSOR_TYPES = frozenset({"imu", "camera", "audio", "location", "battery"})


# ── Wire format dataclass ────────────────────────────────────────────────────


@dataclass
class iOSSensorPayload:
    """Wire format the iPhone companion app sends to the Python receiver.

    All POST requests body must deserialise to this schema.  The ``data``
    field is sensor-type-specific (see CompanionProtocolSpec for per-sensor
    schemas).

    Attributes:
        device_id: iPhone device UUID (UIDevice.current.identifierForVendor).
        sensor_type: One of "imu", "camera", "audio", "location", "battery".
        timestamp: Unix epoch timestamp (seconds, float) from the device clock.
        data: Sensor-specific payload dict.
        format_version: Protocol version string, currently "1.0".
    """

    device_id: str
    sensor_type: str
    timestamp: float
    data: Dict
    format_version: str = FORMAT_VERSION

    def validate(self) -> None:
        """Raise ValueError if the payload is structurally invalid."""
        if not self.device_id:
            raise ValueError("device_id must be non-empty")
        if self.sensor_type not in VALID_SENSOR_TYPES:
            raise ValueError(
                f"Unknown sensor_type '{self.sensor_type}'. "
                f"Valid types: {sorted(VALID_SENSOR_TYPES)}"
            )
        if self.timestamp <= 0:
            raise ValueError("timestamp must be a positive Unix epoch float")
        if not isinstance(self.data, dict):
            raise ValueError("data must be a dict")
        if self.format_version != FORMAT_VERSION:
            raise ValueError(
                f"Unsupported format_version '{self.format_version}'. "
                f"Expected '{FORMAT_VERSION}'"
            )

    @classmethod
    def from_dict(cls, d: Dict) -> "iOSSensorPayload":
        """Deserialise from a raw JSON-decoded dict."""
        return cls(
            device_id=d.get("device_id", ""),
            sensor_type=d.get("sensor_type", ""),
            timestamp=float(d.get("timestamp", 0)),
            data=d.get("data", {}),
            format_version=d.get("format_version", FORMAT_VERSION),
        )


# ── Protocol spec documentation class ───────────────────────────────────────


class CompanionProtocolSpec:
    """Documents the iOS companion app wire protocol for SDK integrators.

    This class is documentation-as-code.  It provides human-readable
    descriptions, example payloads, and schema definitions for each sensor
    type.  It has no runtime behaviour.

    Authentication
    --------------
    Every POST request must include an HMAC-SHA256 signature in the
    ``X-OpenClaw-Signature`` header.  The signature is computed over the
    raw request body bytes using the shared secret (see setup instructions).

    Example (Swift)::

        let body = try! JSONEncoder().encode(payload)
        let sig = HMAC<SHA256>.authenticationCode(for: body, using: secret)
        request.setValue(sig.hex, forHTTPHeaderField: "X-OpenClaw-Signature")

    Compression
    -----------
    - Camera frames: gzip compress the raw JPEG/PNG bytes, base64-encode,
      set ``data.encoding = "gzip+b64"``.
    - IMU / audio / location: uncompressed JSON (no special encoding).

    Rate limits
    -----------
    - Camera: max 30 fps.
    - IMU: max 50 Hz.
    - Audio: continuous chunked streaming (suggest 100 ms chunks).
    - Location: as-needed (CLLocationManager significant-change mode suggested).
    """

    ENDPOINT_IMU = "POST /sensor/imu"
    ENDPOINT_CAMERA = "POST /sensor/camera"
    ENDPOINT_AUDIO = "POST /sensor/audio"
    ENDPOINT_LOCATION = "POST /sensor/location"
    ENDPOINT_BATTERY = "POST /sensor/battery"

    EXAMPLE_IMU_PAYLOAD = {
        "device_id": "A1B2-C3D4-E5F6-G7H8",
        "sensor_type": "imu",
        "timestamp": 1709500000.123,
        "format_version": "1.0",
        "data": {
            "accel_x": 0.012,
            "accel_y": -9.806,
            "accel_z": 0.054,
            "gyro_x": 0.001,
            "gyro_y": -0.003,
            "gyro_z": 0.000,
            "sample_rate_hz": 50,
        },
    }

    EXAMPLE_CAMERA_PAYLOAD = {
        "device_id": "A1B2-C3D4-E5F6-G7H8",
        "sensor_type": "camera",
        "timestamp": 1709500000.033,
        "format_version": "1.0",
        "data": {
            "width": 1920,
            "height": 1080,
            "format": "JPEG",
            "encoding": "gzip+b64",
            "frame_data": "<gzip-compressed-then-base64-encoded-JPEG-bytes>",
        },
    }

    EXAMPLE_AUDIO_PAYLOAD = {
        "device_id": "A1B2-C3D4-E5F6-G7H8",
        "sensor_type": "audio",
        "timestamp": 1709500000.100,
        "format_version": "1.0",
        "data": {
            "sample_rate": 16000,
            "channels": 1,
            "format": "PCM_S16LE",
            "encoding": "b64",
            "audio_data": "<base64-encoded-PCM-bytes>",
            "duration_ms": 100,
        },
    }

    EXAMPLE_LOCATION_PAYLOAD = {
        "device_id": "A1B2-C3D4-E5F6-G7H8",
        "sensor_type": "location",
        "timestamp": 1709500000.000,
        "format_version": "1.0",
        "data": {
            "latitude": 37.7749,
            "longitude": -122.4194,
            "altitude": 12.5,
            "accuracy_m": 5.0,
            "speed_ms": 0.0,
            "heading_deg": 270.0,
        },
    }

    RESPONSE_OK = {"status": "ok", "received_at": "<unix_timestamp>"}
    RESPONSE_BAD_REQUEST = {"status": "error", "message": "<description>"}
    RESPONSE_UNAUTHORIZED = {"status": "error", "message": "invalid signature"}

    @classmethod
    def schema_for(cls, sensor_type: str) -> Dict:
        """Return example payload for the given sensor type."""
        mapping = {
            "imu": cls.EXAMPLE_IMU_PAYLOAD,
            "camera": cls.EXAMPLE_CAMERA_PAYLOAD,
            "audio": cls.EXAMPLE_AUDIO_PAYLOAD,
            "location": cls.EXAMPLE_LOCATION_PAYLOAD,
        }
        if sensor_type not in mapping:
            raise ValueError(f"No schema for sensor_type '{sensor_type}'")
        return mapping[sensor_type]


# ── HMAC helpers ─────────────────────────────────────────────────────────────


def _compute_hmac(secret: bytes, body: bytes) -> str:
    """Return hex HMAC-SHA256 of ``body`` using ``secret``."""
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_hmac(secret: bytes, body: bytes, signature: str) -> bool:
    """Constant-time HMAC verification.

    Args:
        secret: Shared HMAC secret bytes.
        body: Raw request body bytes.
        signature: Hex digest from the ``X-OpenClaw-Signature`` header.

    Returns:
        True if the signature matches, False otherwise.
    """
    expected = _compute_hmac(secret, body)
    return hmac.compare_digest(expected, signature.lower())


# ── Payload converters ───────────────────────────────────────────────────────


def _imu_payload_to_sample(payload: iOSSensorPayload) -> IMUSample:
    """Convert an IMU iOSSensorPayload to an IMUSample."""
    d = payload.data
    ts_ms = int(payload.timestamp * 1000)
    return IMUSample(
        timestamp_ms=ts_ms,
        accel_x=float(d.get("accel_x", 0.0)),
        accel_y=float(d.get("accel_y", 0.0)),
        accel_z=float(d.get("accel_z", 0.0)),
        gyro_x=float(d.get("gyro_x", 0.0)),
        gyro_y=float(d.get("gyro_y", 0.0)),
        gyro_z=float(d.get("gyro_z", 0.0)),
    )


def _camera_payload_to_frame(payload: iOSSensorPayload) -> CameraFrame:
    """Convert a camera iOSSensorPayload to a CameraFrame.

    Handles gzip+b64 and plain b64 encodings.
    """
    d = payload.data
    ts_ms = int(payload.timestamp * 1000)
    encoding = d.get("encoding", "b64")
    raw_b64: str = d.get("frame_data", "")
    frame_bytes = base64.b64decode(raw_b64)
    if encoding == "gzip+b64":
        frame_bytes = gzip.decompress(frame_bytes)
    return CameraFrame(
        timestamp_ms=ts_ms,
        width=int(d.get("width", 0)),
        height=int(d.get("height", 0)),
        format=d.get("format", "JPEG"),
        data=frame_bytes,
    )


def _audio_payload_to_chunk(payload: iOSSensorPayload) -> AudioChunk:
    """Convert an audio iOSSensorPayload to an AudioChunk."""
    d = payload.data
    ts_ms = int(payload.timestamp * 1000)
    audio_bytes = base64.b64decode(d.get("audio_data", ""))
    return AudioChunk(
        timestamp_ms=ts_ms,
        sample_rate=int(d.get("sample_rate", 16000)),
        channels=int(d.get("channels", 1)),
        format=d.get("format", "PCM_S16LE"),
        data=audio_bytes,
    )


# ── HAL implementations ──────────────────────────────────────────────────────


class iOSIMUHal(IMUHal):
    """IMU HAL that buffers samples received from the iOS companion app.

    Samples are pushed into this HAL by ``iOSCompanionReceiver`` when POST
    /sensor/imu requests arrive.  Call ``read_sample()`` from your pipeline
    loop to consume them.
    """

    def __init__(self) -> None:
        self._buffer: List[IMUSample] = []
        self._sample_rate_hz: int = MAX_IMU_HZ
        self._initialized: bool = False
        self._device_id: Optional[str] = None

    # ── HALBase ──────────────────────────────────────────────────────────────

    def validate(self) -> bool:
        return self._initialized

    def get_device_info(self) -> Dict:
        return {
            "hal": "iOSIMUHal",
            "version": "1.0",
            "device_id": self._device_id,
            "sample_rate_hz": self._sample_rate_hz,
            "source": "CoreMotion via companion app",
        }

    # ── IMUHal ───────────────────────────────────────────────────────────────

    def initialize(self, sample_rate_hz: int = MAX_IMU_HZ) -> None:
        """Prepare HAL for incoming IMU samples.

        Args:
            sample_rate_hz: Expected sample rate from the companion app
                (informational; actual rate is controlled by the iOS app).
        """
        self._sample_rate_hz = min(sample_rate_hz, MAX_IMU_HZ)
        self._initialized = True
        logger.info("iOSIMUHal initialised (max %d Hz)", self._sample_rate_hz)

    def read_sample(self) -> Optional[IMUSample]:
        """Pop and return the oldest buffered sample, or None if empty."""
        return self._buffer.pop(0) if self._buffer else None

    def set_sample_rate(self, hz: int) -> None:
        """Update the target sample rate (informational only)."""
        self._sample_rate_hz = min(hz, MAX_IMU_HZ)

    def shutdown(self) -> None:
        self._buffer.clear()
        self._initialized = False

    # ── Push interface (called by receiver) ──────────────────────────────────

    def push_sample(self, sample: IMUSample, device_id: str) -> None:
        """Push a new IMUSample into the buffer."""
        self._device_id = device_id
        self._buffer.append(sample)


class iOSCameraHal(CameraHal):
    """Camera HAL that receives frames pushed from the iOS companion app.

    Frames arrive via POST /sensor/camera and are decoded by the receiver
    before being placed into an internal ring buffer (newest frame only,
    to avoid unbounded growth at 30fps).
    """

    def __init__(self) -> None:
        self._latest_frame: Optional[CameraFrame] = None
        self._resolution: Tuple[int, int] = (1920, 1080)
        self._initialized: bool = False
        self._device_id: Optional[str] = None

    # ── HALBase ──────────────────────────────────────────────────────────────

    def validate(self) -> bool:
        return self._initialized

    def get_device_info(self) -> Dict:
        return {
            "hal": "iOSCameraHal",
            "version": "1.0",
            "device_id": self._device_id,
            "resolution": self._resolution,
            "max_fps": MAX_CAMERA_FPS,
            "source": "AVFoundation via companion app",
        }

    # ── CameraHal ────────────────────────────────────────────────────────────

    def initialize(self, resolution: Tuple[int, int] = (1920, 1080)) -> None:
        """Prepare HAL for incoming camera frames."""
        self._resolution = resolution
        self._initialized = True
        logger.info("iOSCameraHal initialised (%dx%d)", *resolution)

    def capture_frame(self) -> CameraFrame:
        """Return the most recently received frame.

        Raises:
            RuntimeError: If no frame has been received yet.
        """
        if self._latest_frame is None:
            raise RuntimeError("No camera frame received from companion app yet")
        return self._latest_frame

    def get_raw_frame(self) -> Optional[bytes]:
        """Return raw frame bytes, or None if no frame available."""
        return self._latest_frame.data if self._latest_frame else None

    def shutdown(self) -> None:
        self._latest_frame = None
        self._initialized = False

    # ── Push interface ────────────────────────────────────────────────────────

    def push_frame(self, frame: CameraFrame, device_id: str) -> None:
        """Replace the current frame with a newly received one."""
        self._device_id = device_id
        self._latest_frame = frame


class iOSMicrophoneHal(MicrophoneHal):
    """Microphone HAL that receives audio chunks from the iOS companion app.

    Chunks arrive via POST /sensor/audio.  ``get_buffer()`` returns the
    oldest enqueued chunk whose duration matches the request.
    """

    def __init__(self) -> None:
        self._buffer: List[AudioChunk] = []
        self._sample_rate: int = 16000
        self._channels: int = 1
        self._initialized: bool = False
        self._recording: bool = False
        self._device_id: Optional[str] = None

    # ── HALBase ──────────────────────────────────────────────────────────────

    def validate(self) -> bool:
        return self._initialized

    def get_device_info(self) -> Dict:
        return {
            "hal": "iOSMicrophoneHal",
            "version": "1.0",
            "device_id": self._device_id,
            "sample_rate": self._sample_rate,
            "channels": self._channels,
            "source": "AVAudioEngine via companion app",
        }

    # ── MicrophoneHal ─────────────────────────────────────────────────────────

    def initialize(self, sample_rate: int = 16000, channels: int = 1) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._initialized = True
        logger.info("iOSMicrophoneHal initialised (%d Hz, %dch)", sample_rate, channels)

    def start_recording(self) -> None:
        self._recording = True

    def stop_recording(self) -> None:
        self._recording = False

    def get_buffer(self, duration_ms: int) -> AudioChunk:
        """Return the oldest buffered chunk, or empty chunk if none available."""
        if self._buffer:
            return self._buffer.pop(0)
        # Return silent chunk as fallback
        return AudioChunk(
            timestamp_ms=int(time.time() * 1000),
            sample_rate=self._sample_rate,
            channels=self._channels,
            format="PCM_S16LE",
            data=b"\x00" * (self._sample_rate * self._channels * 2 * duration_ms // 1000),
        )

    def get_doa(self) -> Optional[Tuple[float, Optional[float]]]:
        """Direction of arrival -- not available from iPhone (single mic)."""
        return None

    def transcribe(self, audio: AudioChunk, language: str = "en") -> str:
        """Stub transcription -- delegates to OpenClaw STT bridge.

        In production, this would forward ``audio`` to the OpenClaw native STT
        bridge (``openclaw stt transcribe``).  For the companion profile,
        transcription is handled server-side after the audio chunk arrives.

        Returns:
            Empty string (stub).  Wire to openclaw stt for real transcription.
        """
        return ""

    def transcribe_stream(self, stream: Iterator[AudioChunk]) -> Iterator[str]:
        """Stub streaming transcription -- yields partial transcripts.

        In production, pipe chunks to the OpenClaw STT bridge.

        Yields:
            Empty strings (stub).
        """
        for _ in stream:
            yield ""

    def shutdown(self) -> None:
        self._buffer.clear()
        self._initialized = False
        self._recording = False

    # ── Push interface ────────────────────────────────────────────────────────

    def push_chunk(self, chunk: AudioChunk, device_id: str) -> None:
        """Enqueue a received audio chunk."""
        self._device_id = device_id
        self._buffer.append(chunk)


# ── HTTP receiver ─────────────────────────────────────────────────────────────


class _CompanionRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the companion sensor receiver."""

    # server attribute is set by HTTPServer; type annotation for IDE support
    server: "iOSCompanionReceiver"  # type: ignore[override]

    # suppress default access log output
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("iOSCompanionReceiver: " + format, *args)

    def do_POST(self) -> None:  # noqa: N802
        """Route POST requests to the appropriate sensor handler."""
        path = self.path.rstrip("/")
        handlers = {
            "/sensor/imu": self.server._handle_imu,
            "/sensor/camera": self.server._handle_camera,
            "/sensor/audio": self.server._handle_audio,
            "/sensor/location": self.server._handle_location,
            "/sensor/battery": self.server._handle_battery,
        }

        if path not in handlers:
            self._respond(404, {"status": "error", "message": f"Unknown endpoint: {path}"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # HMAC verification
        if self.server.hmac_secret:
            sig = self.headers.get(SIGNATURE_HEADER, "")
            if not verify_hmac(self.server.hmac_secret, body, sig):
                self._respond(401, {"status": "error", "message": "invalid signature"})
                return

        try:
            raw = json.loads(body)
            payload = iOSSensorPayload.from_dict(raw)
            payload.validate()
        except (json.JSONDecodeError, ValueError) as exc:
            self._respond(400, {"status": "error", "message": str(exc)})
            return

        try:
            result = handlers[path](payload)
            self._respond(200, result or {"status": "ok", "received_at": time.time()})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Handler error for %s", path)
            self._respond(500, {"status": "error", "message": str(exc)})

    def _respond(self, code: int, body: Dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class iOSCompanionReceiver(HTTPServer):
    """Local HTTP server that receives sensor payloads from the iPhone companion app.

    Listens on port 18800 (configurable) and dispatches incoming JSON payloads
    to the appropriate HAL push interfaces.

    Usage::

        receiver = iOSCompanionReceiver(hmac_secret=b"my-secret")
        receiver.start()           # non-blocking, runs in daemon thread
        # ... pipeline loop ...
        receiver.stop()

    Args:
        host: Bind address (default "0.0.0.0" -- all interfaces).
        port: Bind port (default 18800).
        hmac_secret: Shared HMAC-SHA256 secret bytes.  If None, auth is
            disabled (development only).
        imu_hal: iOSIMUHal instance to receive IMU samples.
        camera_hal: iOSCameraHal instance to receive camera frames.
        mic_hal: iOSMicrophoneHal instance to receive audio chunks.
        location_callback: Optional callable(dict) invoked for each location update.
        battery_callback: Optional callable(dict) invoked for each battery update.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = COMPANION_PORT,
        hmac_secret: Optional[bytes] = None,
        imu_hal: Optional[iOSIMUHal] = None,
        camera_hal: Optional[iOSCameraHal] = None,
        mic_hal: Optional[iOSMicrophoneHal] = None,
        location_callback: Optional[Callable[[Dict], None]] = None,
        battery_callback: Optional[Callable[[Dict], None]] = None,
    ) -> None:
        super().__init__((host, port), _CompanionRequestHandler)
        self.hmac_secret = hmac_secret
        self.imu_hal = imu_hal or iOSIMUHal()
        self.camera_hal = camera_hal or iOSCameraHal()
        self.mic_hal = mic_hal or iOSMicrophoneHal()
        self.location_callback = location_callback
        self.battery_callback = battery_callback
        self._thread: Optional[Thread] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the receiver in a background daemon thread."""
        self._thread = Thread(target=self.serve_forever, daemon=True, name="ios-companion-receiver")
        self._thread.start()
        host, port = self.server_address
        logger.info("iOSCompanionReceiver listening on %s:%d", host, port)

    def stop(self) -> None:
        """Shutdown the receiver and wait for the thread to exit."""
        self.shutdown()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("iOSCompanionReceiver stopped")

    # ── Sensor handlers ───────────────────────────────────────────────────────

    def _handle_imu(self, payload: iOSSensorPayload) -> Dict:
        sample = _imu_payload_to_sample(payload)
        self.imu_hal.push_sample(sample, payload.device_id)
        return {"status": "ok", "received_at": time.time()}

    def _handle_camera(self, payload: iOSSensorPayload) -> Dict:
        frame = _camera_payload_to_frame(payload)
        self.camera_hal.push_frame(frame, payload.device_id)
        return {"status": "ok", "received_at": time.time()}

    def _handle_audio(self, payload: iOSSensorPayload) -> Dict:
        chunk = _audio_payload_to_chunk(payload)
        self.mic_hal.push_chunk(chunk, payload.device_id)
        return {"status": "ok", "received_at": time.time()}

    def _handle_location(self, payload: iOSSensorPayload) -> Dict:
        if self.location_callback:
            self.location_callback(payload.data)
        return {"status": "ok", "received_at": time.time()}

    def _handle_battery(self, payload: iOSSensorPayload) -> Dict:
        if self.battery_callback:
            self.battery_callback(payload.data)
        return {"status": "ok", "received_at": time.time()}


# ── DeviceProfile integration ─────────────────────────────────────────────────


@dataclass
class iOSCompanionProfile:
    """DeviceProfile for iPhone as an OpenClaw embodiment sensor node.

    This profile wires together the three iPhone HALs and the HTTP receiver
    into a single object that the SDK pipeline can consume.

    Attributes:
        name: Profile identifier ("ios-companion").
        description: Human-readable description.
        capabilities: List of sensor capabilities provided by this profile.
        receiver: The HTTP receiver instance.
        imu: IMU HAL.
        camera: Camera HAL.
        microphone: Microphone HAL.
    """

    name: str = "ios-companion"
    description: str = "iPhone as OpenClaw embodiment sensor node via companion app"
    capabilities: List[str] = field(
        default_factory=lambda: ["camera", "microphone", "imu", "location", "battery"]
    )
    receiver: iOSCompanionReceiver = field(default_factory=iOSCompanionReceiver)
    imu: iOSIMUHal = field(default_factory=iOSIMUHal)
    camera: iOSCameraHal = field(default_factory=iOSCameraHal)
    microphone: iOSMicrophoneHal = field(default_factory=iOSMicrophoneHal)

    def __post_init__(self) -> None:
        # Wire HALs into the receiver so they share the same instances
        self.receiver.imu_hal = self.imu
        self.receiver.camera_hal = self.camera
        self.receiver.mic_hal = self.microphone

    def initialize(self) -> None:
        """Initialize all HALs and start the receiver."""
        self.imu.initialize(sample_rate_hz=MAX_IMU_HZ)
        self.camera.initialize(resolution=(1920, 1080))
        self.microphone.initialize(sample_rate=16000, channels=1)
        self.receiver.start()

    def shutdown(self) -> None:
        """Stop the receiver and shut down all HALs."""
        self.receiver.stop()
        self.imu.shutdown()
        self.camera.shutdown()
        self.microphone.shutdown()

    def as_dict(self) -> Dict:
        """Return a config dict compatible with load_profile() consumers."""
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "receiver_port": COMPANION_PORT,
            "max_camera_fps": MAX_CAMERA_FPS,
            "max_imu_hz": MAX_IMU_HZ,
            "protocol_version": FORMAT_VERSION,
        }


# ── Module-level exported profile ────────────────────────────────────────────

PROFILE = iOSCompanionProfile()
"""Module-level DeviceProfile instance for the iOS companion sensor node."""
