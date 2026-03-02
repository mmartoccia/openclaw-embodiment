"""HAL abstract contracts and shared datatypes."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Tuple


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
        """Read a sample."""

    @abstractmethod
    def set_sample_rate(self, hz: int) -> None:
        """Set sample rate."""

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
        """Capture frame."""

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
        """Start recording."""

    @abstractmethod
    def stop_recording(self) -> None:
        """Stop recording."""

    @abstractmethod
    def get_buffer(self, duration_ms: int) -> AudioChunk:
        """Fetch buffered audio."""

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
    def shutdown(self) -> None:
        """Shutdown audio output."""


# ---------------------------------------------------------------------------
# Actuator layer -- bidirectional embodiment (physical actuation commands)
# ---------------------------------------------------------------------------

@dataclass
class ActuatorCommand:
    """Command envelope for actuator dispatch."""

    command_id: str
    action: str
    params: dict
    timestamp_ms: int
    timeout_ms: int = 5000


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

    @abstractmethod
    def stop_all(self) -> None:
        """Emergency stop all actuators."""

    @abstractmethod
    def get_capabilities(self) -> list:
        """Return list of supported action strings."""

    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown actuator system."""
