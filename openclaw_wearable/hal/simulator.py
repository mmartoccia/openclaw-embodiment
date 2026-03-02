"""Simulator HAL implementations for CI and local testing."""

import queue
import time
from typing import Callable, Optional

from .base import AudioChunk, AudioOutputHal, CameraFrame, CameraHal, ClassificationResult, ClassifierHal, DisplayCard, DisplayHal, IMUHal, IMUSample, MicrophoneHal, SendResult, TransportHal, TransportState


def _ms() -> int:
    return time.monotonic_ns() // 1_000_000


class SimulatedIMU(IMUHal):
    """Synthetic IMU with deterministic trigger-friendly waveform."""

    def __init__(self) -> None:
        self.idx = 0
        self.rate = 25

    def initialize(self, sample_rate_hz: int = 25) -> None:
        self.rate = sample_rate_hz

    def read_sample(self) -> Optional[IMUSample]:
        self.idx += 1
        phase = self.idx % 30
        gyro = 220.0 if 4 <= phase <= 8 else (18.0 if 9 <= phase <= 18 else 4.0)
        return IMUSample(_ms(), 0.0, 0.0, 9.8, gyro, gyro / 2, 2.0)

    def set_sample_rate(self, hz: int) -> None:
        self.rate = hz

    def shutdown(self) -> None:
        return

    def validate(self) -> bool:
        return self.read_sample() is not None

    def get_device_info(self) -> dict:
        return {"name": "sim-imu", "axes": 6, "max_rate_hz": 100, "current_rate_hz": self.rate}


class SimulatedCamera(CameraHal):
    """Static JPEG-like byte source."""

    def initialize(self, resolution=(320, 240)) -> None:
        self.resolution = resolution

    def capture_frame(self) -> CameraFrame:
        w, h = self.resolution
        return CameraFrame(_ms(), w, h, "JPEG", b"\xff\xd8" + b"sim" * 1024 + b"\xff\xd9")

    def shutdown(self) -> None:
        return

    def validate(self) -> bool:
        return len(self.capture_frame().data) > 10

    def get_device_info(self) -> dict:
        return {"name": "sim-cam", "sensor": "virtual", "max_width": 1920, "max_height": 1080}


class SimulatedMicrophone(MicrophoneHal):
    """PCM silence generator with rolling buffer semantics."""

    def initialize(self, sample_rate: int = 16000, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.channels = channels

    def start_recording(self) -> None:
        return

    def stop_recording(self) -> None:
        return

    def get_buffer(self, duration_ms: int) -> AudioChunk:
        n = int(self.sample_rate * (duration_ms / 1000.0) * 2)
        return AudioChunk(_ms(), self.sample_rate, self.channels, "PCM_S16LE", b"\x00" * n)

    def shutdown(self) -> None:
        return

    def validate(self) -> bool:
        return len(self.get_buffer(100).data) > 0

    def get_device_info(self) -> dict:
        return {"name": "sim-mic", "max_sample_rate": 48000, "channels": self.channels}


class SimulatedClassifier(ClassifierHal):
    """Confidence from payload length parity for deterministic tests."""

    def initialize(self, model_path: str, config: Optional[dict] = None) -> None:
        self.model_path = model_path

    def classify(self, image: bytes, width: int, height: int, format: str = "RGB") -> ClassificationResult:
        c = 0.9 if len(image) % 2 == 0 else 0.2
        return ClassificationResult("interesting" if c >= 0.5 else "uninteresting", c, 3)

    def get_model_info(self) -> dict:
        return {"model_name": "sim", "runtime": "sim", "input": "any"}

    def shutdown(self) -> None:
        return

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {"name": "sim-classifier"}


class SimulatedTransport(TransportHal):
    """Loopback transport preserving sent payload for receive path."""

    def __init__(self) -> None:
        self.state = TransportState.DISCONNECTED
        self.q = queue.Queue()
        self.cb = None  # type: Optional[Callable[[TransportState], None]]

    def _emit(self, state: TransportState) -> None:
        self.state = state
        if self.cb:
            self.cb(state)

    def initialize(self, config: dict) -> None:
        self.config = config

    def connect(self) -> None:
        self._emit(TransportState.CONNECTED)

    def send(self, payload: bytes) -> SendResult:
        t0 = _ms()
        self.q.put(payload)
        return SendResult(True, len(payload), _ms() - t0)

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        try:
            return self.q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return None

    def get_state(self) -> TransportState:
        return self.state

    def set_state_callback(self, callback: Callable[[TransportState], None]) -> None:
        self.cb = callback

    def disconnect(self) -> None:
        self._emit(TransportState.DISCONNECTED)

    def shutdown(self) -> None:
        self.disconnect()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {"name": "sim-txp", "type": "ble", "max_throughput_bps": 1000000, "mtu": 247}


class SimulatedDisplay(DisplayHal):
    """Console display sink used by tests and demo."""

    def initialize(self, resolution=(80, 24)) -> None:
        self.resolution = resolution
        self.last = None

    def show(self, card: DisplayCard) -> None:
        self.last = card

    def clear(self) -> None:
        self.last = None

    def shutdown(self) -> None:
        return

    def validate(self) -> bool:
        self.show(DisplayCard("glance", None, "ok", 12, 500))
        return True

    def get_device_info(self) -> dict:
        return {"name": "sim-display", "width": self.resolution[0], "height": self.resolution[1]}


class SimulatedAudioOutput(AudioOutputHal):
    """Memory-backed audio sink."""

    def initialize(self, sample_rate: int = 22050, channels: int = 1) -> None:
        self.playing = False

    def play(self, audio_data: bytes, format: str = "PCM_S16LE", sample_rate: int = 22050) -> None:
        self.playing = True

    def stop(self) -> None:
        self.playing = False

    def is_playing(self) -> bool:
        return bool(self.playing)

    def shutdown(self) -> None:
        self.stop()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {"name": "sim-audio"}
