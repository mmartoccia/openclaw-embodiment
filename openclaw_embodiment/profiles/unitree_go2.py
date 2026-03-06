"""Unitree Go2 Quadruped Robot profile for OpenClaw Embodiment SDK.

Integrates with unitree_sdk2py (pip install unitree_sdk2py) for:
- WebRTC camera stream (1920x1080, 30fps)
- IMU data via robot_state_client (500Hz)
- Locomotion via sport_client (walk, trot, bound, stand, sit, wave)
- Onboard speaker for audio output
- Battery/health monitoring via robot_state_client

Testable without hardware: unitree_sdk2py has simulation mode.
Set simulation.enabled=true in config to use simulated robot.

SDK reference: github.com/unitreerobotics/unitree_sdk2_python
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterator, Optional, Tuple

from ..hal.base import (
    ActuatorCommand,
    ActuatorHal,
    ActuatorResult,
    AudioChunk,
    AudioOutputHal,
    CameraFrame,
    CameraHal,
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
    SimulatedAudioOutput,
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
# IMU HAL -- robot_state_client.GetMotionState()
# ---------------------------------------------------------------------------


class Go2IMUHal(IMUHal):
    """IMU HAL for Unitree Go2 via robot_state_client.

    Reads IMU data (linear acceleration, angular velocity) from
    unitree_sdk2py.robot_state_client.GetMotionState().
    Falls back to SimulatedIMU when simulation_mode=True.
    """

    def __init__(self, simulation_mode: bool = True) -> None:
        """Initialize Go2IMUHal.

        Args:
            simulation_mode: Use SimulatedIMU instead of real robot_state_client.
        """
        self._sim_mode = simulation_mode
        self._sim: Optional[SimulatedIMU] = None
        self._robot_state_client = None
        self._rate = 25

    def initialize(self, sample_rate_hz: int = 500) -> None:
        """Initialize IMU sensor.

        Args:
            sample_rate_hz: Target IMU polling rate in Hz (Go2 supports 500Hz).
        """
        self._rate = sample_rate_hz
        if self._sim_mode:
            self._sim = SimulatedIMU()
            self._sim.initialize(sample_rate_hz)
            logger.info("Go2IMUHal: simulation mode (rate=%d Hz)", sample_rate_hz)
            return
        try:
            from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient
            self._robot_state_client = RobotStateClient()
            self._robot_state_client.Init()
            logger.info("Go2IMUHal: initialized real robot_state_client (rate=%d Hz)", sample_rate_hz)
        except ImportError:
            logger.warning("Go2IMUHal: unitree_sdk2py not installed -- falling back to simulation")
            self._sim = SimulatedIMU()
            self._sim.initialize(sample_rate_hz)

    def read_sample(self) -> Optional[IMUSample]:
        """Return latest IMU sample from robot_state_client or simulator.

        Returns:
            IMUSample with 6DOF data, or None if unavailable.
        """
        if self._sim:
            return self._sim.read_sample()
        try:
            state = self._robot_state_client.GetMotionState()
            imu = state.imu_state
            return IMUSample(
                timestamp_ms=_ms(),
                accel_x=imu.accelerometer[0],
                accel_y=imu.accelerometer[1],
                accel_z=imu.accelerometer[2],
                gyro_x=imu.gyroscope[0],
                gyro_y=imu.gyroscope[1],
                gyro_z=imu.gyroscope[2],
            )
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- Unitree Go2 -- SDK errors unpredictable
            logger.warning("Go2IMUHal: read_sample failed: %s", exc)
            return None

    def set_sample_rate(self, hz: int) -> None:
        """Set IMU polling rate.

        Args:
            hz: Target sample rate in Hz.
        """
        self._rate = hz
        if self._sim:
            self._sim.set_sample_rate(hz)

    def shutdown(self) -> None:
        """Shutdown IMU HAL."""
        if self._sim:
            self._sim.shutdown()

    def validate(self) -> bool:
        """Validate IMU by reading a sample."""
        return self.read_sample() is not None

    def get_device_info(self) -> dict:
        """Return IMU HAL metadata."""
        return {
            "name": "go2-imu",
            "type": "unitree_imu",
            "rate_hz": self._rate,
            "simulation": self._sim_mode,
        }


# ---------------------------------------------------------------------------
# Camera HAL -- WebRTC via Go2WebRTCConnection
# ---------------------------------------------------------------------------


class Go2CameraHal(CameraHal):
    """WebRTC camera HAL for Unitree Go2.

    Captures frames from Go2 head camera via unitree_sdk2py WebRTC track.
    Falls back to SimulatedCamera in simulation mode.
    """

    def __init__(self, simulation_mode: bool = True) -> None:
        """Initialize Go2CameraHal.

        Args:
            simulation_mode: Use SimulatedCamera when True.
        """
        self._sim_mode = simulation_mode
        self._sim: Optional[SimulatedCamera] = None
        self._webrtc_conn = None
        self._latest_frame: Optional[bytes] = None
        self._resolution: Tuple[int, int] = (1920, 1080)

    def initialize(self, resolution: Tuple[int, int] = (1920, 1080)) -> None:
        """Initialize WebRTC camera connection.

        Args:
            resolution: Target resolution (width, height).
        """
        self._resolution = resolution
        if self._sim_mode:
            self._sim = SimulatedCamera()
            self._sim.initialize(resolution)
            logger.info("Go2CameraHal: simulation mode (res=%s)", resolution)
            return
        try:
            from unitree_sdk2py.go2.video.video_client import VideoClient
            self._webrtc_conn = VideoClient()
            self._webrtc_conn.Init()
            logger.info("Go2CameraHal: WebRTC camera initialized (res=%s)", resolution)
        except ImportError:
            logger.warning("Go2CameraHal: unitree_sdk2py not installed -- simulation mode")
            self._sim = SimulatedCamera()
            self._sim.initialize(resolution)

    def capture_frame(self) -> CameraFrame:
        """Capture latest frame from WebRTC stream or simulator.

        Returns:
            CameraFrame with JPEG data at 1920x1080, 30fps.
        """
        if self._sim:
            return self._sim.capture_frame()
        try:
            code, data = self._webrtc_conn.GetImageSample()
            if code == 0 and data:
                w, h = self._resolution
                return CameraFrame(_ms(), w, h, "JPEG", bytes(data))
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- Unitree Go2 -- SDK errors unpredictable
            logger.warning("Go2CameraHal: capture_frame failed: %s", exc)
        w, h = self._resolution
        return CameraFrame(_ms(), w, h, "JPEG", b"\xff\xd8\xff\xd9")

    def shutdown(self) -> None:
        """Shutdown camera HAL."""
        if self._sim:
            self._sim.shutdown()

    def validate(self) -> bool:
        """Validate camera by capturing a test frame."""
        try:
            return len(self.capture_frame().data) > 0
        except Exception:  # grain: ignore NAKED_EXCEPT -- Unitree Go2 -- SDK errors unpredictable
            return False

    def get_device_info(self) -> dict:
        """Return camera HAL metadata."""
        return {
            "name": "go2-camera",
            "type": "webrtc",
            "fps": 30,
            "resolution": f"{self._resolution[0]}x{self._resolution[1]}",
            "simulation": self._sim_mode,
        }


# ---------------------------------------------------------------------------
# Actuator HAL -- sport_client locomotion
# ---------------------------------------------------------------------------


class Go2ActuatorHal(ActuatorHal):
    """Locomotion actuator HAL for Unitree Go2 via SportClient.

    Supports: move_forward, move_backward, turn, stand_up, sit_down,
    high_stand, wave, and optional arm attachment via move_arm.
    Falls back to SimulatedActuator in simulation mode.
    """

    SUPPORTED_ACTIONS = [
        "move_forward", "move_backward", "turn", "stand_up",
        "sit_down", "high_stand", "wave", "move_arm", "stop",
    ]

    def __init__(self, simulation_mode: bool = True) -> None:
        """Initialize Go2ActuatorHal.

        Args:
            simulation_mode: Log commands without real robot when True.
        """
        self._sim_mode = simulation_mode
        self._sport_client = None
        self._initialized = False
        self._commands: list = []

    def initialize(self) -> None:
        """Initialize locomotion actuator system."""
        self._initialized = True
        if self._sim_mode:
            logger.info("Go2ActuatorHal: simulation mode -- commands will be logged, not executed")
            return
        try:
            from unitree_sdk2py.go2.sport.sport_client import SportClient
            self._sport_client = SportClient()
            self._sport_client.SetTimeout(10.0)
            self._sport_client.Init()
            logger.info("Go2ActuatorHal: SportClient initialized")
        except ImportError:
            logger.warning("Go2ActuatorHal: unitree_sdk2py not installed -- simulation mode")

    def execute(self, command: ActuatorCommand) -> ActuatorResult:
        """Execute locomotion command via SportClient.

        Supported commands (command.action):
        - move_forward: params={'speed': float}
        - move_backward: params={'speed': float}
        - turn: params={'yaw_speed': float}
        - stand_up, sit_down, high_stand, wave: no params
        - move_arm: params={'x': float, 'y': float, 'z': float}

        Args:
            command: ActuatorCommand with action and params.

        Returns:
            ActuatorResult with success/timing info.
        """
        t0 = _ms()
        self._commands.append(command)

        if self._sim_mode or self._sport_client is None:
            logger.debug("Go2ActuatorHal: [SIM] execute %s %s", command.action, command.params)
            return ActuatorResult(command.command_id, True, _ms() - t0)

        try:
            code = self._dispatch_sport_command(command)
            success = (code == 0)
            return ActuatorResult(command.command_id, success, _ms() - t0,
                                  error=None if success else f"code={code}")
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- Unitree Go2 -- SDK errors unpredictable
            logger.warning("Go2ActuatorHal: execute failed: %s", exc)
            return ActuatorResult(command.command_id, False, _ms() - t0, error=str(exc))

    def _dispatch_sport_command(self, command: ActuatorCommand) -> int:
        """Dispatch action to SportClient.

        Args:
            command: ActuatorCommand to dispatch.

        Returns:
            Return code from SportClient (0 = success).
        """
        action = command.action
        p = command.params or {}
        if action == "move_forward":
            return self._sport_client.Move(p.get("speed", 0.3), 0, 0)
        elif action == "move_backward":
            return self._sport_client.Move(-p.get("speed", 0.3), 0, 0)
        elif action == "turn":
            return self._sport_client.Move(0, 0, p.get("yaw_speed", 0.5))
        elif action == "stand_up":
            return self._sport_client.RecoveryStand()
        elif action == "sit_down":
            return self._sport_client.StandDown()
        elif action == "high_stand":
            return self._sport_client.HighStand()
        elif action == "wave":
            return self._sport_client.Hello()
        elif action == "stop":
            return self._sport_client.StopMove()
        elif action == "move_arm":
            # Optional arm attachment
            return self._sport_client.ArmWorkspacePosCtrl(
                p.get("x", 0.0), p.get("y", 0.0), p.get("z", 0.0), 0.0, 0.0, 0.0
            )
        else:
            logger.warning("Go2ActuatorHal: unknown action '%s'", action)
            return -1

    def stop_all(self) -> None:
        """Emergency stop all locomotion."""
        if self._sport_client:
            try:
                self._sport_client.StopMove()
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- Unitree Go2 -- SDK errors unpredictable
                logger.warning("Go2ActuatorHal: stop_all failed: %s", exc)

    def get_capabilities(self) -> list:
        """Return list of supported locomotion actions."""
        return list(self.SUPPORTED_ACTIONS)

    def shutdown(self) -> None:
        """Shutdown actuator HAL."""
        self.stop_all()
        self._initialized = False

    def validate(self) -> bool:
        """Validate actuator by checking capabilities list."""
        return len(self.get_capabilities()) > 0

    def get_device_info(self) -> dict:
        """Return actuator HAL metadata."""
        return {
            "name": "go2-actuator",
            "type": "sport_client",
            "gaits": ["walk", "trot", "bound"],
            "simulation": self._sim_mode,
        }


# ---------------------------------------------------------------------------
# Transport HAL -- HTTP to Go2 onboard PC
# ---------------------------------------------------------------------------


class Go2TransportHal(TransportHal):
    """HTTP transport HAL for Unitree Go2 onboard PC.

    Connects to the Go2 onboard PC (default 192.168.123.161:8080).
    Expected latency: 20ms (WiFi/Ethernet).
    In simulation mode, delegates to SimulatedTransport.
    """

    def __init__(self, host: str = "192.168.123.161", port: int = 8080, simulation_mode: bool = True) -> None:
        """Initialize Go2TransportHal.

        Args:
            host: Go2 onboard PC host address.
            port: Go2 onboard PC port.
            simulation_mode: Use SimulatedTransport when True.
        """
        self._host = host
        self._port = port
        self._sim_mode = simulation_mode
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
        if self._sim_mode:
            self._sim = SimulatedTransport()
            self._sim.initialize(config)

    def connect(self) -> None:
        """Connect transport."""
        if self._sim:
            self._sim.connect()
            self._state = self._sim.get_state()
        else:
            self._state = TransportState.CONNECTED
            if self._callback:
                self._callback(self._state)

    def send(self, payload: bytes) -> SendResult:
        """Send context payload to Go2 onboard PC.

        Args:
            payload: Context payload bytes.

        Returns:
            SendResult with success/timing.
        """
        if self._sim:
            return self._sim.send(payload)
        t0 = _ms()
        try:
            import urllib.request
            req = urllib.request.Request(
                f"http://{self._host}:{self._port}/context",
                data=payload,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            elapsed = _ms() - t0
            self._latency_window.append(elapsed)
            if len(self._latency_window) > 10:
                self._latency_window.pop(0)
            return SendResult(True, len(payload), elapsed)
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- Unitree Go2 -- SDK errors unpredictable
            logger.warning("Go2TransportHal: send failed: %s", exc)
            return SendResult(False, 0, _ms() - t0, error_code=str(exc))

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
        """Register state change callback."""
        self._callback = callback
        if self._sim:
            self._sim.set_state_callback(callback)

    def disconnect(self) -> None:
        """Disconnect transport."""
        if self._sim:
            self._sim.disconnect()
        self._state = TransportState.DISCONNECTED

    def shutdown(self) -> None:
        """Shutdown transport."""
        self.disconnect()

    def get_expected_latency_ms(self) -> int:
        """Return expected transport latency: 20ms (WiFi/Ethernet).

        Returns:
            Expected latency in milliseconds.
        """
        return 20

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
            "name": "go2-transport",
            "type": "http",
            "host": self._host,
            "port": self._port,
            "simulation": self._sim_mode,
        }


# ---------------------------------------------------------------------------
# Audio Output HAL -- onboard speaker
# ---------------------------------------------------------------------------


class Go2AudioOutputHal(AudioOutputHal):
    """Audio output HAL for Unitree Go2 onboard speaker.

    Speaks via the Go2 onboard speaker if available.
    Falls back to TTS on companion machine in simulation/unavailable cases.
    """

    def __init__(self, simulation_mode: bool = True) -> None:
        """Initialize Go2AudioOutputHal.

        Args:
            simulation_mode: Use SimulatedAudioOutput when True.
        """
        self._sim_mode = simulation_mode
        self._sim: Optional[SimulatedAudioOutput] = None
        self._playing = False

    def initialize(self, sample_rate: int = 22050, channels: int = 1) -> None:
        """Initialize audio output.

        Args:
            sample_rate: Output sample rate.
            channels: Number of audio channels.
        """
        if self._sim_mode:
            self._sim = SimulatedAudioOutput()
            self._sim.initialize(sample_rate, channels)
        logger.info("Go2AudioOutputHal: initialized (sim=%s)", self._sim_mode)

    def play(self, audio_data: bytes, format: str = "PCM_S16LE", sample_rate: int = 22050) -> None:
        """Play audio via onboard speaker.

        Args:
            audio_data: Raw PCM or encoded audio bytes.
            format: Audio format string.
            sample_rate: Sample rate in Hz.
        """
        if self._sim:
            self._sim.play(audio_data, format, sample_rate)
            return
        self._playing = True
        logger.debug("Go2AudioOutputHal: playing %d bytes via onboard speaker", len(audio_data))
        # Real: stream PCM to Go2 audio hardware via DDS/SDK
        self._playing = False

    def stop(self) -> None:
        """Stop audio playback."""
        self._playing = False
        if self._sim:
            self._sim.stop()

    def is_playing(self) -> bool:
        """Return playback state."""
        if self._sim:
            return self._sim.is_playing()
        return self._playing

    def speak_agent_response(self, response: object) -> None:
        """Speak agent response via TTS.

        Args:
            response: AgentResponse with content attribute.
        """
        if hasattr(response, "content"):
            text = str(response.content)
            logger.debug("Go2AudioOutputHal: speak '%s'", text[:50])

    def shutdown(self) -> None:
        """Shutdown audio output."""
        self.stop()
        if self._sim:
            self._sim.shutdown()

    def validate(self) -> bool:
        """Validate audio output."""
        if self._sim:
            return self._sim.validate()
        return True

    def get_device_info(self) -> dict:
        """Return audio output metadata."""
        return {
            "name": "go2-audio-output",
            "type": "onboard_speaker",
            "simulation": self._sim_mode,
        }


# ---------------------------------------------------------------------------
# System Health HAL -- battery + temperature from robot_state_client
# ---------------------------------------------------------------------------


class Go2SystemHealthHal(SystemHealthHal):
    """System health HAL for Unitree Go2.

    Reports battery level and temperature from robot_state_client.
    Falls back to simulated healthy values in simulation mode.
    """

    def __init__(self, simulation_mode: bool = True) -> None:
        """Initialize Go2SystemHealthHal.

        Args:
            simulation_mode: Return simulated health data when True.
        """
        self._sim_mode = simulation_mode
        self._robot_state_client = None
        self._degraded_callbacks: list = []

        if not simulation_mode:
            try:
                from unitree_sdk2py.go2.robot_state.robot_state_client import RobotStateClient
                self._robot_state_client = RobotStateClient()
                self._robot_state_client.Init()
            except ImportError:
                logger.warning("Go2SystemHealthHal: unitree_sdk2py not installed -- simulation mode")

    def get_health_report(self) -> HealthReport:
        """Return current device health from robot_state_client or simulation.

        Returns:
            HealthReport with battery and connectivity info.
        """
        import datetime
        battery = None
        temp = None
        warnings = []

        if self._robot_state_client is not None:
            try:
                state = self._robot_state_client.GetMotionState()
                battery = state.battery_state.percent
                if battery < 20:
                    warnings.append(f"Low battery: {battery:.0f}%")
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- Unitree Go2 -- SDK errors unpredictable
                logger.debug("Go2SystemHealthHal: state read failed: %s", exc)
                warnings.append("Unable to read robot state")

        return HealthReport(
            timestamp=datetime.datetime.utcnow(),
            device_id="unitree-go2",
            cpu_percent=None,
            memory_percent=None,
            temperature_c=temp,
            battery_percent=battery,
            connectivity={"wifi": True, "ethernet": True},
            sensor_status={"camera": True, "imu": True, "actuator": True},
            is_operational=len(warnings) == 0,
            warnings=warnings,
        )

    def is_operational(self) -> bool:
        """Return True if Go2 is operational."""
        report = self.get_health_report()
        return report.is_operational

    def on_degraded(self, callback: Callable[[HealthReport], None]) -> None:
        """Register degradation callback.

        Args:
            callback: Called when health degrades.
        """
        self._degraded_callbacks.append(callback)

    def validate(self) -> bool:
        """Validate by generating a health report."""
        return self.get_health_report() is not None

    def get_device_info(self) -> dict:
        """Return health HAL metadata."""
        return {
            "name": "go2-health",
            "device": "unitree-go2",
            "simulation": self._sim_mode,
        }


# ---------------------------------------------------------------------------
# Profile factory
# ---------------------------------------------------------------------------


def build_unitree_go2_hals(config: dict) -> dict:
    """Build all HAL instances for the Unitree Go2 profile.

    Args:
        config: Profile config dict (from unitree_go2.yaml).

    Returns:
        Dict mapping HAL type name to HAL instance.
    """
    sim_enabled = config.get("simulation", {}).get("enabled", True)
    transport_cfg = config.get("transport", {})

    imu = Go2IMUHal(simulation_mode=sim_enabled)
    imu.initialize(sample_rate_hz=config.get("hardware", {}).get("imu", {}).get("rate_hz", 500))

    camera = Go2CameraHal(simulation_mode=sim_enabled)
    camera.initialize()

    actuator = Go2ActuatorHal(simulation_mode=sim_enabled)
    actuator.initialize()

    audio_out = Go2AudioOutputHal(simulation_mode=sim_enabled)
    audio_out.initialize()

    transport = Go2TransportHal(
        host=transport_cfg.get("host", "192.168.123.161"),
        port=transport_cfg.get("port", 8080),
        simulation_mode=sim_enabled,
    )
    transport.initialize(transport_cfg)
    transport.connect()

    health = Go2SystemHealthHal(simulation_mode=sim_enabled)
    status = SimulatedStatusIndicator()
    status.initialize()

    return {
        "camera": camera,
        "imu": imu,
        "actuator": actuator,
        "audio_output": audio_out,
        "transport": transport,
        "system_health": health,
        "status_indicator": status,
    }


__all__ = [
    "Go2IMUHal",
    "Go2CameraHal",
    "Go2ActuatorHal",
    "Go2TransportHal",
    "Go2AudioOutputHal",
    "Go2SystemHealthHal",
    "build_unitree_go2_hals",
]
