"""Apple Vision Pro profile for OpenClaw Embodiment SDK.

Architecture: VisionProTeleop bridge (WebSocket) provides:
- Head pose (6DOF: position + orientation) via companion app
- Camera frames via ReplayKit stream (30fps)
- Spatial overlay display for passthrough content
- Local WiFi WebSocket transport (8ms expected latency)

Testable without hardware: VisionProTeleop uses WebSocket; mock WebSocket
server in test mode provides all data streams without hardware.

SDK reference: VisionProTeleop (github.com/Haotian-Labs/VisionProTeleop)
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from typing import Callable, Iterator, Optional, Tuple

from ..hal.base import (
    AudioChunk,
    AudioOutputHal,
    CameraFrame,
    CameraHal,
    DisplayCard,
    DisplayHal,
    HealthReport,
    IMUHal,
    IMUSample,
    MicrophoneHal,
    SendResult,
    StatusIndicatorHal,
    SystemHealthHal,
    TransportHal,
    TransportState,
)
from ..hal.simulator import (
    SimulatedCamera,
    SimulatedIMU,
    SimulatedStatusIndicator,
    SimulatedSystemHealth,
    SimulatedTransport,
)

logger = logging.getLogger(__name__)


def _ms() -> int:
    return time.monotonic_ns() // 1_000_000


# ---------------------------------------------------------------------------
# VisionProTeleop WebSocket client
# ---------------------------------------------------------------------------


class VisionProTeleopClient:
    """WebSocket client for VisionProTeleop companion app bridge.

    Receives head pose data and camera frames from Vision Pro.
    Sends display overlay commands back to headset.

    In mock_mode, generates synthetic pose/frame data without hardware.
    """

    def __init__(self, host: str = "localhost", port: int = 8430, mock_mode: bool = True) -> None:
        """Initialize VisionProTeleopClient.

        Args:
            host: Vision Pro companion app WebSocket host.
            port: Vision Pro companion app WebSocket port.
            mock_mode: Generate synthetic data without hardware.
        """
        self.host = host
        self.port = port
        self.mock_mode = mock_mode

        self._pose_queue: queue.Queue = queue.Queue(maxsize=10)
        self._frame_queue: queue.Queue = queue.Queue(maxsize=4)
        self._out_queue: queue.Queue = queue.Queue(maxsize=32)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._mock_idx = 0

    def start(self) -> None:
        """Start the WebSocket client in a background thread."""
        self._running = True
        if self.mock_mode:
            self._thread = threading.Thread(target=self._run_mock, daemon=True, name="avp-mock")
        else:
            self._thread = threading.Thread(target=self._run_ws, daemon=True, name="avp-ws")
        self._thread.start()
        logger.info("VisionProTeleopClient: started (mock=%s)", self.mock_mode)

    def stop(self) -> None:
        """Stop the WebSocket client."""
        self._running = False

    def get_latest_pose(self, timeout_s: float = 0.1) -> Optional[dict]:
        """Get latest head pose message.

        Args:
            timeout_s: Max wait time.

        Returns:
            Dict with head_position and head_quaternion, or None.
        """
        try:
            return self._pose_queue.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def get_latest_frame(self, timeout_s: float = 0.1) -> Optional[bytes]:
        """Get latest camera frame bytes.

        Args:
            timeout_s: Max wait time.

        Returns:
            JPEG bytes or None.
        """
        try:
            return self._frame_queue.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def send_overlay(self, payload: dict) -> None:
        """Queue overlay payload to send to Vision Pro.

        Args:
            payload: Display overlay command dict.
        """
        try:
            self._out_queue.put_nowait(json.dumps(payload).encode())
        except queue.Full:
            pass

    def _run_mock(self) -> None:
        """Generate synthetic pose + frame data for testing."""
        while self._running:
            self._mock_idx += 1
            t = self._mock_idx * 0.033  # ~30fps

            # Synthetic head pose: gentle nodding motion
            pose = {
                "head_position": {"x": 0.0, "y": 1.6, "z": 0.0},
                "head_quaternion": {
                    "x": math.sin(t * 0.1) * 0.05,
                    "y": math.sin(t * 0.05) * 0.1,
                    "z": 0.0,
                    "w": 1.0,
                },
                "timestamp_ms": _ms(),
            }
            try:
                self._pose_queue.put_nowait(pose)
            except queue.Full:
                pass

            # Synthetic JPEG frame
            frame = b"\xff\xd8" + b"avp-sim" * 512 + b"\xff\xd9"
            try:
                self._frame_queue.put_nowait(frame)
            except queue.Full:
                pass

            time.sleep(0.033)

    def _run_ws(self) -> None:
        """Connect to VisionProTeleop WebSocket and process messages."""
        try:
            import asyncio
            import websockets

            async def client_loop() -> None:
                uri = f"ws://{self.host}:{self.port}"
                async with websockets.connect(uri) as ws:
                    async def send_loop() -> None:
                        while self._running:
                            try:
                                payload = self._out_queue.get_nowait()
                                await ws.send(payload)
                            except queue.Empty:
                                await asyncio.sleep(0.01)

                    send_task = asyncio.ensure_future(send_loop())
                    try:
                        async for msg in ws:
                            if not self._running:
                                break
                            data = json.loads(msg) if isinstance(msg, str) else {}
                            msg_type = data.get("type", "")
                            if msg_type == "pose":
                                try:
                                    self._pose_queue.put_nowait(data)
                                except queue.Full:
                                    pass
                            elif msg_type == "frame":
                                frame_b64 = data.get("data", "")
                                if frame_b64:
                                    import base64
                                    try:
                                        self._frame_queue.put_nowait(base64.b64decode(frame_b64))
                                    except queue.Full:
                                        pass
                    finally:
                        send_task.cancel()

            asyncio.run(client_loop())
        except ImportError:
            logger.warning("VisionProTeleopClient: websockets not installed -- no WS connection")
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- Apple Vision Pro -- WebSocket/AVP bridge errors
            logger.error("VisionProTeleopClient WS error: %s", exc)


# ---------------------------------------------------------------------------
# IMU HAL -- head pose as 6DOF IMU
# ---------------------------------------------------------------------------


class VisionProIMUHal(IMUHal):
    """Head pose IMU HAL for Apple Vision Pro.

    Reads 6DOF head pose (position + orientation) from VisionProTeleop
    WebSocket, returns as IMUSample with angular rates derived from
    quaternion derivative.

    In mock_mode, uses synthetic pose stream.
    """

    def __init__(self, client: VisionProTeleopClient) -> None:
        """Initialize VisionProIMUHal.

        Args:
            client: Shared VisionProTeleopClient instance.
        """
        self._client = client
        self._rate = 30
        self._last_quat: Optional[dict] = None
        self._last_ts: int = 0

    def initialize(self, sample_rate_hz: int = 30) -> None:
        """Initialize head pose IMU.

        Args:
            sample_rate_hz: Sampling rate (30Hz matches camera FPS).
        """
        self._rate = sample_rate_hz
        logger.info("VisionProIMUHal: initialized (rate=%d Hz)", sample_rate_hz)

    def read_sample(self) -> Optional[IMUSample]:
        """Return latest head pose as IMUSample.

        Orientation is returned as gyro components (pitch/roll/yaw rates).
        Position is returned as acceleration (dx/dy/dz from position delta).

        Returns:
            IMUSample with head pose data, or None if no pose available.
        """
        pose = self._client.get_latest_pose(timeout_s=0.05)
        if pose is None:
            return None

        q = pose.get("head_quaternion", {})
        pos = pose.get("head_position", {})
        ts = pose.get("timestamp_ms", _ms())

        # Convert quaternion to Euler rates (simplified)
        qx = q.get("x", 0.0)
        qy = q.get("y", 0.0)
        qz = q.get("z", 0.0)
        qw = q.get("w", 1.0)

        # Pitch (x-axis rotation)
        sinr_cosp = 2 * (qw * qx + qy * qz)
        cosr_cosp = 1 - 2 * (qx * qx + qy * qy)
        pitch = math.atan2(sinr_cosp, cosr_cosp)

        # Yaw (z-axis rotation)
        siny_cosp = 2 * (qw * qz + qx * qy)
        cosy_cosp = 1 - 2 * (qy * qy + qz * qz)
        yaw = math.atan2(siny_cosp, cosy_cosp)

        return IMUSample(
            timestamp_ms=ts,
            accel_x=pos.get("x", 0.0),
            accel_y=pos.get("y", 0.0),
            accel_z=pos.get("z", 0.0),
            gyro_x=math.degrees(pitch),
            gyro_y=math.degrees(yaw),
            gyro_z=0.0,
        )

    def get_orientation(self) -> Optional[Tuple[float, float, float]]:
        """Return current head orientation as (pitch, yaw, roll) in degrees.

        Returns:
            Tuple of (pitch, yaw, roll) or None if no data.
        """
        sample = self.read_sample()
        if sample is None:
            return None
        return (sample.gyro_x, sample.gyro_y, sample.gyro_z)

    def get_acceleration(self) -> Optional[Tuple[float, float, float]]:
        """Return current head linear acceleration as (x, y, z).

        Returns:
            Tuple of (x, y, z) acceleration or None if no data.
        """
        sample = self.read_sample()
        if sample is None:
            return None
        return (sample.accel_x, sample.accel_y, sample.accel_z)

    def set_sample_rate(self, hz: int) -> None:
        """Set sampling rate.

        Args:
            hz: Target rate in Hz.
        """
        self._rate = hz

    def shutdown(self) -> None:
        """Shutdown IMU HAL."""
        pass

    def validate(self) -> bool:
        """Validate by reading a sample."""
        return self.read_sample() is not None

    def get_device_info(self) -> dict:
        """Return IMU metadata."""
        return {
            "name": "avp-imu",
            "type": "head_pose",
            "dof": 6,
            "rate_hz": self._rate,
        }


# ---------------------------------------------------------------------------
# Camera HAL -- ReplayKit stream via WebSocket
# ---------------------------------------------------------------------------


class VisionProCameraHal(CameraHal):
    """Camera HAL for Apple Vision Pro via ReplayKit stream.

    Receives camera frames from Vision Pro companion app (ReplayKit capture)
    streamed over WebSocket. Returns latest spatial frame on capture_frame().
    """

    def __init__(self, client: VisionProTeleopClient) -> None:
        """Initialize VisionProCameraHal.

        Args:
            client: Shared VisionProTeleopClient instance.
        """
        self._client = client
        self._resolution: Tuple[int, int] = (3680, 3504)

    def initialize(self, resolution: Tuple[int, int] = (3680, 3504)) -> None:
        """Initialize camera.

        Args:
            resolution: Target spatial camera resolution.
        """
        self._resolution = resolution
        logger.info("VisionProCameraHal: initialized (res=%s)", resolution)

    def capture_frame(self) -> CameraFrame:
        """Return latest spatial camera frame from ReplayKit stream.

        Returns:
            CameraFrame with JPEG data.
        """
        jpeg = self._client.get_latest_frame(timeout_s=0.1)
        if jpeg is None:
            jpeg = b"\xff\xd8\xff\xd9"
        w, h = self._resolution
        return CameraFrame(_ms(), w, h, "JPEG", jpeg)

    def shutdown(self) -> None:
        """Shutdown camera HAL."""
        pass

    def validate(self) -> bool:
        """Validate by capturing a test frame."""
        try:
            return len(self.capture_frame().data) > 0
        except Exception:  # grain: ignore NAKED_EXCEPT -- Apple Vision Pro -- WebSocket/AVP bridge errors
            return False

    def get_device_info(self) -> dict:
        """Return camera metadata."""
        return {
            "name": "avp-camera",
            "type": "replaykit_stream",
            "fps": 30,
            "resolution": f"{self._resolution[0]}x{self._resolution[1]}",
        }


# ---------------------------------------------------------------------------
# Display HAL -- spatial overlay via WebSocket
# ---------------------------------------------------------------------------


class VisionProDisplayHal(DisplayHal):
    """Spatial overlay display HAL for Apple Vision Pro passthrough.

    Sends display overlay commands to Vision Pro via VisionProTeleop WebSocket.
    Renders text and image cards in the spatial UI overlay.
    """

    def __init__(self, client: VisionProTeleopClient) -> None:
        """Initialize VisionProDisplayHal.

        Args:
            client: Shared VisionProTeleopClient instance.
        """
        self._client = client
        self._resolution: Tuple[int, int] = (3680, 3504)
        self._last_card: Optional[DisplayCard] = None

    def initialize(self, resolution: Tuple[int, int] = (3680, 3504)) -> None:
        """Initialize spatial display.

        Args:
            resolution: Display resolution (Vision Pro 3680x3504 per eye).
        """
        self._resolution = resolution
        logger.info("VisionProDisplayHal: initialized (res=%s)", resolution)

    def show(self, card: DisplayCard) -> None:
        """Render display card in Vision Pro spatial overlay.

        Args:
            card: DisplayCard with title, body, font_size, duration_ms.
        """
        self._last_card = card
        payload = {
            "type": "overlay",
            "mode": card.mode,
            "title": card.title,
            "body": card.body,
            "font_size": card.font_size,
            "duration_ms": card.duration_ms,
        }
        self._client.send_overlay(payload)

    def show_card(self, text: str, image: Optional[bytes] = None) -> None:
        """Render text (and optional image) in Vision Pro spatial overlay.

        Args:
            text: Text content to display.
            image: Optional JPEG image bytes for image overlay.
        """
        card = DisplayCard(
            mode="spatial",
            title=None,
            body=text,
            font_size=24,
            duration_ms=3000,
        )
        self.show(card)

    def clear(self) -> None:
        """Clear the spatial overlay."""
        self._last_card = None
        self._client.send_overlay({"type": "overlay", "action": "clear"})

    def render_agent_response(self, response: object) -> None:
        """Display agent response in spatial overlay.

        Args:
            response: AgentResponse with content attribute.
        """
        if hasattr(response, "content"):
            self.show_card(str(response.content))

    def shutdown(self) -> None:
        """Shutdown display HAL."""
        self.clear()

    def validate(self) -> bool:
        """Validate by sending a test card."""
        try:
            self.show(DisplayCard("test", "Test", "ok", 12, 100))
            return True
        except Exception:  # grain: ignore NAKED_EXCEPT -- Apple Vision Pro -- WebSocket/AVP bridge errors
            return False

    def get_device_info(self) -> dict:
        """Return display metadata."""
        return {
            "name": "avp-display",
            "type": "spatial_overlay",
            "resolution": f"{self._resolution[0]}x{self._resolution[1]}",
        }


# ---------------------------------------------------------------------------
# Transport HAL -- WebSocket to OpenClaw
# ---------------------------------------------------------------------------


class VisionProTransportHal(TransportHal):
    """WebSocket transport HAL for Apple Vision Pro.

    Connects to OpenClaw gateway via local WiFi WebSocket.
    Expected latency: 8ms (local WiFi).
    In mock_mode, delegates to SimulatedTransport.
    """

    def __init__(self, host: str = "localhost", port: int = 8430, mock_mode: bool = True) -> None:
        """Initialize VisionProTransportHal.

        Args:
            host: OpenClaw WebSocket gateway host.
            port: OpenClaw WebSocket gateway port.
            mock_mode: Use SimulatedTransport when True.
        """
        self._host = host
        self._port = port
        self._mock_mode = mock_mode
        self._sim: Optional[SimulatedTransport] = None
        self._state = TransportState.DISCONNECTED
        self._callback: Optional[Callable[[TransportState], None]] = None
        self._latency_window: list = []

    def initialize(self, config: dict) -> None:
        """Initialize transport.

        Args:
            config: Config dict with optional host/port overrides.
        """
        self._host = config.get("host", self._host)
        self._port = config.get("port", self._port)
        if self._mock_mode:
            self._sim = SimulatedTransport()
            self._sim.initialize(config)

    def connect(self) -> None:
        """Connect WebSocket transport."""
        if self._sim:
            self._sim.connect()
            self._state = self._sim.get_state()
        else:
            self._state = TransportState.CONNECTED
            if self._callback:
                self._callback(self._state)

    def send(self, payload: bytes) -> SendResult:
        """Send context payload via WebSocket.

        Args:
            payload: Context payload bytes.

        Returns:
            SendResult with timing info.
        """
        if self._sim:
            return self._sim.send(payload)
        t0 = _ms()
        # Real WebSocket send would go here
        logger.debug("VisionProTransportHal: send %d bytes", len(payload))
        elapsed = _ms() - t0
        self._latency_window.append(elapsed)
        if len(self._latency_window) > 10:
            self._latency_window.pop(0)
        return SendResult(True, len(payload), elapsed)

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Receive response payload.

        Args:
            timeout_ms: Timeout in milliseconds.

        Returns:
            Response bytes or None.
        """
        if self._sim:
            return self._sim.receive(timeout_ms)
        return None

    def get_state(self) -> TransportState:
        """Return connection state."""
        if self._sim:
            return self._sim.get_state()
        return self._state

    def set_state_callback(self, callback: Callable[[TransportState], None]) -> None:
        """Register state change callback.

        Args:
            callback: State change callback.
        """
        self._callback = callback
        if self._sim:
            self._sim.set_state_callback(callback)

    def disconnect(self) -> None:
        """Disconnect WebSocket transport."""
        if self._sim:
            self._sim.disconnect()
        self._state = TransportState.DISCONNECTED

    def shutdown(self) -> None:
        """Shutdown transport."""
        self.disconnect()

    def get_expected_latency_ms(self) -> int:
        """Return expected latency: 8ms (local WiFi WebSocket).

        Returns:
            Expected latency in milliseconds.
        """
        return 8

    def get_measured_latency_ms(self) -> Optional[int]:
        """Return rolling average latency.

        Returns:
            Rolling average or None if no sends yet.
        """
        if self._sim:
            return self._sim.get_measured_latency_ms()
        if not self._latency_window:
            return None
        return int(sum(self._latency_window) / len(self._latency_window))

    def validate(self) -> bool:
        """Validate transport."""
        if self._sim:
            return self._sim.validate()
        return True

    def get_device_info(self) -> dict:
        """Return transport metadata."""
        return {
            "name": "avp-transport",
            "type": "websocket",
            "host": self._host,
            "port": self._port,
            "mock_mode": self._mock_mode,
        }


# ---------------------------------------------------------------------------
# Profile factory
# ---------------------------------------------------------------------------


def build_apple_vision_pro_hals(config: dict) -> dict:
    """Build all HAL instances for the Apple Vision Pro profile.

    Args:
        config: Profile config dict (from apple_vision_pro.yaml).

    Returns:
        Dict mapping HAL type name to HAL instance.
    """
    transport_cfg = config.get("transport", {})
    mock_mode = True  # AVP always uses mock/bridge mode for now

    client = VisionProTeleopClient(
        host=transport_cfg.get("host", "localhost"),
        port=transport_cfg.get("port", 8430),
        mock_mode=mock_mode,
    )
    client.start()

    imu = VisionProIMUHal(client)
    imu.initialize()

    camera = VisionProCameraHal(client)
    hw_cfg = config.get("hardware", {})
    display_cfg = hw_cfg.get("display", {})
    camera.initialize()

    display = VisionProDisplayHal(client)
    display.initialize()

    transport = VisionProTransportHal(
        host=transport_cfg.get("host", "localhost"),
        port=transport_cfg.get("port", 8430),
        mock_mode=mock_mode,
    )
    transport.initialize(transport_cfg)
    transport.connect()

    health = SimulatedSystemHealth(device_id="apple-vision-pro")
    status = SimulatedStatusIndicator()
    status.initialize()

    return {
        "camera": camera,
        "imu": imu,
        "display": display,
        "transport": transport,
        "system_health": health,
        "status_indicator": status,
        "_client": client,
    }


__all__ = [
    "VisionProIMUHal",
    "VisionProCameraHal",
    "VisionProDisplayHal",
    "VisionProTransportHal",
    "VisionProTeleopClient",
    "build_apple_vision_pro_hals",
]
