"""HAL abstract contracts and shared datatypes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Iterator, Optional, Tuple

if TYPE_CHECKING:
    from ..core.response import AgentResponse as _AgentResponse


@dataclass(frozen=True)
class IMUSample:
    """Single IMU sample."""

    timestamp_ms: int
    accel_x: float
    accel_y: float
    accel_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float


@dataclass(frozen=True)
class CameraFrame:
    """Single camera frame."""

    timestamp_ms: int
    width: int
    height: int
    format: str
    data: bytes


@dataclass(frozen=True)
class AudioChunk:
    """Audio buffer chunk."""

    timestamp_ms: int
    sample_rate: int
    channels: int
    format: str
    data: bytes
    duration_ms: int = 0
    timestamp: float = 0.0


@dataclass(frozen=True)
class ClassificationResult:
    """Binary scene classification result."""

    label: str
    confidence: float
    inference_time_ms: int


class TransportState(Enum):
    """Transport connection state."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"


@dataclass(frozen=True)
class SendResult:
    """Outcome of send operation."""

    success: bool
    bytes_sent: int
    elapsed_ms: int
    error_code: Optional[str] = None
    retries: int = 0


@dataclass
class DisplayCard:
    """Display content model."""

    mode: str
    title: Optional[str]
    body: str
    font_size: int
    duration_ms: int


class HALBase(ABC):
    """Shared HAL base contract."""

    HAL_VERSION = "1.0.0"

    @abstractmethod
    def validate(self) -> bool:
        """Validate HAL runtime readiness."""

    @abstractmethod
    def get_device_info(self) -> dict:
        """Return HAL device metadata."""


class IMUHal(HALBase, ABC):
    """IMU abstraction."""

    @abstractmethod
    def initialize(self, sample_rate_hz: int = 25) -> None:
        """Initialize IMU."""

    @abstractmethod
    def read_sample(self) -> Optional[IMUSample]:
        """Return the latest accelerometer/gyro sample, or None if unavailable."""

    @abstractmethod
    def set_sample_rate(self, hz: int) -> None:
        """Configure the IMU polling frequency in Hz."""

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown IMU."""


class CameraHal(HALBase, ABC):
    """Camera abstraction."""

    @abstractmethod
    def initialize(self, resolution: Tuple[int, int] = (320, 240)) -> None:
        """Initialize camera."""

    @abstractmethod
    def capture_frame(self) -> CameraFrame:
        """Grab a single frame from the camera sensor."""

    def get_raw_frame(self) -> Optional[bytes]:
        """Return raw unprocessed frame bytes. Override for device-specific access.
        Default implementation calls capture_frame() and returns frame.data."""
        try:
            return self.capture_frame().data
        except Exception:  # grain: ignore NAKED_EXCEPT -- camera frame capture -- SDK/driver errors are unpredictable
            return None

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown camera."""


class MicrophoneHal(HALBase, ABC):
    """Microphone abstraction."""

    @abstractmethod
    def initialize(self, sample_rate: int = 16000, channels: int = 1) -> None:
        """Initialize microphone."""

    @abstractmethod
    def start_recording(self) -> None:
        """Begin capturing audio into the internal ring buffer."""

    @abstractmethod
    def stop_recording(self) -> None:
        """Halt audio capture and flush the ring buffer."""

    @abstractmethod
    def get_buffer(self, duration_ms: int) -> AudioChunk:
        """Fetch buffered audio."""

    def get_doa(self) -> Optional[Tuple[float, Optional[float]]]:
        """Return Direction of Arrival as (azimuth_degrees, elevation_degrees).

        Azimuth is -180 to 180 (0 = front). Elevation is -90 to 90 (optional).
        Returns None if DoA is not supported or no sound detected.
        Default implementation returns None (override for mic arrays with DoA support).
        """
        return None

    @abstractmethod
    def transcribe(self, audio: AudioChunk, language: str = "en") -> str:
        """Transcribe audio via OpenClaw native STT bridge.

        Delegates to: openclaw stt transcribe (api.runtime.stt.transcribeAudioFile)
        Returns: transcribed text string
        """
        ...

    @abstractmethod
    def transcribe_stream(self, stream: Iterator[AudioChunk]) -> Iterator[str]:
        """Streaming transcription -- yields partial transcripts as audio arrives."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown microphone."""


class ClassifierHal(HALBase, ABC):
    """Classifier abstraction."""

    @abstractmethod
    def initialize(self, model_path: str, config: Optional[dict] = None) -> None:
        """Initialize classifier."""

    @abstractmethod
    def classify(self, image: bytes, width: int, height: int, format: str = "RGB") -> ClassificationResult:
        """Classify image."""

    @abstractmethod
    def get_model_info(self) -> dict:
        """Return model info."""

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown classifier."""


class TransportHal(HALBase, ABC):
    """Transport abstraction."""

    @abstractmethod
    def initialize(self, config: dict) -> None:
        """Initialize transport."""

    @abstractmethod
    def connect(self) -> None:
        """Connect transport."""

    @abstractmethod
    def send(self, payload: bytes) -> SendResult:
        """Send payload."""

    @abstractmethod
    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Receive payload."""

    @abstractmethod
    def get_state(self) -> TransportState:
        """Get connection state."""

    @abstractmethod
    def set_state_callback(self, callback: Callable[[TransportState], None]) -> None:
        """Register state callback."""

    @abstractmethod
    def disconnect(self) -> None:
        """Disconnect transport."""

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown transport."""

    @abstractmethod
    def get_expected_latency_ms(self) -> int:
        """Return the expected one-way send latency in milliseconds.

        Used by the pipeline to adjust actuation timing.
        BLE: ~50ms, HTTP: ~10ms, LocalMLX: ~5ms.

        Returns:
            Estimated latency in milliseconds.
        """
        ...

    def get_measured_latency_ms(self) -> Optional[int]:
        """Return rolling average of last 10 send latencies in milliseconds.

        Returns None until at least one send has completed.
        Override in implementations that track per-send timing.

        Returns:
            Rolling average elapsed_ms or None if no data yet.
        """
        return None


class DisplayHal(HALBase, ABC):
    """Display abstraction."""

    @abstractmethod
    def initialize(self, resolution: Tuple[int, int] = (640, 400)) -> None:
        """Initialize display."""

    @abstractmethod
    def show(self, card: DisplayCard) -> None:
        """Render card."""

    @abstractmethod
    def clear(self) -> None:
        """Clear display."""

    @abstractmethod
    def render_agent_response(self, response: "_AgentResponse") -> None:
        """Format and display an agent response card on the output surface.

        Args:
            response: AgentResponse from the bidirectional agent pipeline.
        """

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown display."""


class AudioOutputHal(HALBase, ABC):
    """Audio output abstraction."""

    @abstractmethod
    def initialize(self, sample_rate: int = 22050, channels: int = 1) -> None:
        """Initialize audio output."""

    @abstractmethod
    def play(self, audio_data: bytes, format: str = "PCM_S16LE", sample_rate: int = 22050) -> None:
        """Play audio."""

    @abstractmethod
    def stop(self) -> None:
        """Stop playback."""

    @abstractmethod
    def is_playing(self) -> bool:
        """Return playback state."""

    @abstractmethod
    def speak_agent_response(self, response: "_AgentResponse") -> None:
        """Speak an agent response via TTS or audio playback.

        Args:
            response: AgentResponse from the bidirectional agent pipeline.
        """

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown audio output."""


# ---------------------------------------------------------------------------
# Actuator layer -- bidirectional embodiment (physical actuation commands)
# ---------------------------------------------------------------------------

@dataclass
class JointState:
    """State snapshot for a single robot joint."""

    joint_id: str
    position_degrees: float
    velocity_dps: float
    load_percent: float
    temperature_celsius: Optional[float] = None


@dataclass
class ActuatorCommand:
    """Command envelope for actuator dispatch.

    Used for both single-command dispatch and action chunking.
    When used in a chunk (via ActionChunkBuffer), duration_ms and
    timestamp_offset_ms carry step timing within the chunk.
    """

    command_id: str
    action: str
    params: dict
    timestamp_ms: int
    timeout_ms: int = 5000
    duration_ms: int = 100           # how long to execute this step (chunk use)
    timestamp_offset_ms: int = 0     # offset from chunk start (chunk use)


@dataclass
class ActuatorResult:
    """Result of an actuator command execution."""

    command_id: str
    success: bool
    elapsed_ms: int
    error: Optional[str] = None


class ActuatorHal(HALBase, ABC):
    """Actuator abstraction for physical output control."""

    @abstractmethod
    def initialize(self) -> None:
        """Initialize actuator system."""

    @abstractmethod
    def execute(self, command: ActuatorCommand) -> ActuatorResult:
        """Execute an actuator command."""

    def send_raw_command(self, raw: bytes) -> Optional[bytes]:
        """Send a raw command bytes directly to the actuator hardware.
        Override for device-specific raw protocol access.
        Default implementation raises NotImplementedError -- must be overridden to use."""
        raise NotImplementedError("send_raw_command not implemented for this device")

    def execute_chunk(self, commands: list, blend_steps: int = 10) -> None:
        """Execute a sequence of actions as a chunk.

        Default implementation executes commands sequentially via execute().
        Override in subclasses that have native chunk execution support
        (e.g. HALs with ActionChunkBuffer or LeRobot ActionQueue).

        Args:
            commands: List of ActuatorCommand objects to execute in sequence.
            blend_steps: Number of overlap steps for blending at chunk boundaries.
        """
        for cmd in commands:
            self.execute(cmd)

    @property
    def supports_chunking(self) -> bool:
        """Return True if this HAL natively supports chunk execution.

        Override to return True in HALs that have an ActionChunkBuffer
        or LeRobot ActionQueue wired in. When True, callers should use
        execute_chunk() + start_control_loop() for decoupled operation.
        """
        return False

    @abstractmethod
    def stop_all(self) -> None:
        """Emergency stop all actuators."""

    @abstractmethod
    def get_capabilities(self) -> list:
        """Return list of supported action strings."""

    def get_joint_states(self) -> dict:
        """Return current state of all joints as {joint_id: JointState}.
        Override for devices that expose joint telemetry.
        Default returns empty dict."""
        return {}

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown actuator system."""


# ---------------------------------------------------------------------------
# Power management layer
# ---------------------------------------------------------------------------

class ChargingState(Enum):
    """Battery charging state."""

    CHARGING = "charging"
    DISCHARGING = "discharging"
    FULL = "full"
    UNKNOWN = "unknown"


class PowerSource(Enum):
    """Active power source."""

    BATTERY = "battery"
    WALL = "wall"
    USB = "usb"
    UNKNOWN = "unknown"


class PowerHal(HALBase, ABC):
    """Power management abstraction."""

    @abstractmethod
    def get_battery_percent(self) -> float:
        """Return battery level 0.0-100.0. Returns -1.0 if no battery present."""

    @abstractmethod
    def get_charging_state(self) -> ChargingState:
        """Return current charging state."""

    @abstractmethod
    def get_power_source(self) -> PowerSource:
        """Return active power source."""

    @abstractmethod
    def register_low_battery_callback(self, threshold: float, callback: Callable[[], None]) -> None:
        """Register callback fired when battery drops below threshold percent."""

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown power monitoring."""


# ---------------------------------------------------------------------------
# Status indicator layer -- LED / visual feedback
# ---------------------------------------------------------------------------

class StatusPattern(Enum):
    """Named LED animation patterns."""

    HEARTBEAT = "heartbeat"
    ALERT = "alert"
    PROCESSING = "processing"
    IDLE = "idle"


class StatusIndicatorHal(HALBase, ABC):
    """Hardware abstraction for LED / status indicator hardware.

    Provides a uniform interface for visual device status feedback across
    all supported platforms: Reachy Mini front LED, Even G2 indicator,
    Pi GPIO LED strip, and simulator.

    Contract:
    - All methods complete within 50ms.
    - Thread-safe. Lifecycle idempotent.
    - ``off()`` is always safe to call regardless of current state.
    """

    HAL_VERSION = "1.0.0"

    @abstractmethod
    def initialize(self) -> None:
        """Initialize LED hardware and set to idle state.

        Raises:
            HardwareError: If LED hardware cannot be initialised.
        """
        ...

    @abstractmethod
    def set_color(self, r: int, g: int, b: int) -> None:
        """Set LED to a solid RGB colour.

        Args:
            r: Red channel 0-255.
            g: Green channel 0-255.
            b: Blue channel 0-255.

        Raises:
            ValueError: If any channel is outside 0-255.
        """
        ...

    @abstractmethod
    def blink(self, interval_ms: int = 500) -> None:
        """Start blinking at the given interval (on+off cycle).

        Args:
            interval_ms: Full on-off cycle duration in milliseconds.
                         Minimum 50ms.
        """
        ...

    @abstractmethod
    def pulse(self, pattern: str = "heartbeat") -> None:
        """Start a named animation pattern.

        Args:
            pattern: One of ``"heartbeat"``, ``"alert"``, ``"processing"``, ``"idle"``.

        Raises:
            ValueError: If pattern is not a recognised StatusPattern name.
        """
        ...

    @abstractmethod
    def off(self) -> None:
        """Turn off all LEDs immediately. Always safe to call."""
        ...

    @abstractmethod
    def shutdown(self) -> None:
        """Release LED hardware resources. Idempotent."""
        ...


# ---------------------------------------------------------------------------
# System health layer -- 10th HAL ABC
# ---------------------------------------------------------------------------

from datetime import datetime


@dataclass
class HealthReport:
    """Snapshot of device system health.

    Attributes:
        timestamp: UTC datetime of the report.
        device_id: Identifier for the reporting device.
        cpu_percent: CPU utilization 0-100, or None if unavailable.
        memory_percent: RAM utilization 0-100, or None if unavailable.
        temperature_c: CPU/SoC temperature in Celsius, or None if unavailable.
        battery_percent: Battery level 0-100, or None if no battery.
        connectivity: Map of interface name to connected boolean (e.g. {"wifi": True}).
        sensor_status: Map of sensor name to operational boolean (e.g. {"camera": True}).
        is_operational: True if device is fully operational (no critical warnings).
        warnings: List of human-readable warning strings.
    """

    timestamp: datetime
    device_id: str
    cpu_percent: Optional[float]
    memory_percent: Optional[float]
    temperature_c: Optional[float]
    battery_percent: Optional[float]
    connectivity: dict  # {"wifi": True, "ble": True, ...}
    sensor_status: dict  # {"camera": True, "imu": False, ...}
    is_operational: bool
    warnings: list


class SystemHealthHal(HALBase, ABC):
    """System health abstraction -- 10th HAL ABC.

    Provides a uniform interface for querying device health metrics
    (CPU, memory, temperature, battery, connectivity, sensor status).
    Required for production deployments.
    """

    @abstractmethod
    def get_health_report(self) -> HealthReport:
        """Return a current snapshot of device health.

        Returns:
            HealthReport populated with all available metrics.
        """
        ...

    @abstractmethod
    def is_operational(self) -> bool:
        """Return True if the device is fully operational with no critical warnings.

        Returns:
            bool indicating operational status.
        """
        ...

    @abstractmethod
    def on_degraded(self, callback: Callable[[HealthReport], None]) -> None:
        """Register a callback invoked when device health degrades.

        The callback receives the HealthReport at the moment degradation
        is detected. May be called from a background thread.

        Args:
            callback: Callable accepting a HealthReport.
        """
        ...
