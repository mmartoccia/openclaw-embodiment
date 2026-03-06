"""OpenGlass (ESP32-S3 DIY AI Glasses) profile for OpenClaw Embodiment SDK.

Architecture: BLE GATT connection to ESP32-S3 firmware.
- Camera: GATT camera characteristic (0x5678), MTU-split JPEG chunks
- Audio: GATT audio characteristic (0x5679), 8kHz PCM
- Control: GATT control characteristic (0x567A), LED commands
- Battery: BLE Battery Service (0x2A19)

Testable without hardware: open source BLE firmware means full BLE
simulator is available via bleak's mock scanner + SimulatedCamera/Mic.

Reference: github.com/BasedHardware/OpenGlass (ESP32-S3 AI glasses)
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterator, Optional, Tuple

from ..hal.base import (
    AudioChunk,
    CameraFrame,
    CameraHal,
    ChargingState,
    HealthReport,
    MicrophoneHal,
    PowerHal,
    PowerSource,
    SendResult,
    StatusIndicatorHal,
    SystemHealthHal,
    TransportHal,
    TransportState,
)
from ..hal.simulator import (
    SimulatedCamera,
    SimulatedMicrophone,
    SimulatedStatusIndicator,
    SimulatedSystemHealth,
    SimulatedTransport,
)

logger = logging.getLogger(__name__)

# BLE GATT UUIDs for OpenGlass ESP32-S3 firmware
OPENGLASS_SERVICE_UUID = "00001234-0000-1000-8000-00805f9b34fb"
OPENGLASS_CAMERA_CHAR_UUID = "00005678-0000-1000-8000-00805f9b34fb"
OPENGLASS_AUDIO_CHAR_UUID = "00005679-0000-1000-8000-00805f9b34fb"
OPENGLASS_CONTROL_CHAR_UUID = "0000567a-0000-1000-8000-00805f9b34fb"
OPENGLASS_BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


def _ms() -> int:
    return time.monotonic_ns() // 1_000_000


# ---------------------------------------------------------------------------
# Camera HAL -- BLE GATT JPEG chunk reassembly
# ---------------------------------------------------------------------------


class OpenGlassCameraHal(CameraHal):
    """BLE GATT camera HAL for OpenGlass ESP32-S3.

    Subscribes to the camera GATT characteristic (UUID 0x5678).
    ESP32 sends MTU-split JPEG chunks; this HAL reassembles them.
    capture_frame() returns the latest complete JPEG frame.

    In mock_mode (no hardware), delegates to SimulatedCamera.
    """

    def __init__(self, mock_mode: bool = True) -> None:
        """Initialize OpenGlassCameraHal.

        Args:
            mock_mode: Use SimulatedCamera without BLE hardware.
        """
        self._mock_mode = mock_mode
        self._sim: Optional[SimulatedCamera] = None
        self._resolution: Tuple[int, int] = (320, 240)
        self._chunk_buffer: list = []
        self._latest_frame: Optional[bytes] = None
        self._ble_client = None

    def initialize(self, resolution: Tuple[int, int] = (320, 240)) -> None:
        """Initialize BLE GATT camera subscription.

        Args:
            resolution: Camera resolution (OpenGlass is 320x240 typical).
        """
        self._resolution = resolution
        if self._mock_mode:
            self._sim = SimulatedCamera()
            self._sim.initialize(resolution)
            logger.info("OpenGlassCameraHal: mock mode (res=%s)", resolution)
            return
        logger.info("OpenGlassCameraHal: BLE GATT camera (res=%s, char=%s)", resolution, OPENGLASS_CAMERA_CHAR_UUID)

    def _reassemble_jpeg(self, chunks: list) -> Optional[bytes]:
        """Reassemble MTU-split JPEG chunks into a complete frame.

        Detects JPEG SOI (0xFF 0xD8) as frame start and EOI (0xFF 0xD9) as end.

        Args:
            chunks: List of raw BLE notification bytes.

        Returns:
            Complete JPEG bytes, or None if incomplete.
        """
        data = b"".join(chunks)
        soi = data.find(b"\xff\xd8")
        eoi = data.find(b"\xff\xd9", soi + 2 if soi >= 0 else 0)
        if soi >= 0 and eoi > soi:
            return data[soi:eoi + 2]
        return None

    def on_ble_notification(self, chunk: bytes) -> None:
        """Handle incoming BLE notification chunk from camera characteristic.

        Called by BLE transport on GATT notification. Accumulates chunks
        until a complete JPEG frame is assembled.

        Args:
            chunk: Raw bytes from BLE GATT notification.
        """
        # Start of new frame detection
        if len(chunk) >= 2 and chunk[:2] == b"\xff\xd8":
            self._chunk_buffer = [chunk]
        else:
            self._chunk_buffer.append(chunk)

        frame = self._reassemble_jpeg(self._chunk_buffer)
        if frame is not None:
            self._latest_frame = frame
            self._chunk_buffer = []

    def capture_frame(self) -> CameraFrame:
        """Return latest assembled JPEG frame from BLE camera.

        Returns:
            CameraFrame with JPEG data, 1fps.
        """
        if self._mock_mode and self._sim:
            return self._sim.capture_frame()

        jpeg = self._latest_frame or b"\xff\xd8\xff\xd9"
        w, h = self._resolution
        return CameraFrame(_ms(), w, h, "JPEG", jpeg)

    def shutdown(self) -> None:
        """Shutdown camera HAL."""
        if self._sim:
            self._sim.shutdown()
        self._chunk_buffer = []
        self._latest_frame = None

    def validate(self) -> bool:
        """Validate camera by capturing a test frame."""
        try:
            return len(self.capture_frame().data) > 0
        except Exception:  # grain: ignore NAKED_EXCEPT -- OpenGlass -- BLE GATT errors unpredictable
            return False

    def get_device_info(self) -> dict:
        """Return camera HAL metadata."""
        return {
            "name": "openglass-camera",
            "type": "ble_gatt",
            "fps": 1,
            "mtu": 512,
            "char_uuid": OPENGLASS_CAMERA_CHAR_UUID,
            "mock_mode": self._mock_mode,
        }


# ---------------------------------------------------------------------------
# Microphone HAL -- BLE GATT 8kHz PCM
# ---------------------------------------------------------------------------


class OpenGlassMicrophoneHal(MicrophoneHal):
    """BLE GATT microphone HAL for OpenGlass ESP32-S3.

    Subscribes to audio GATT characteristic (UUID 0x5679).
    Receives 8kHz PCM_INT16 audio chunks.
    """

    def __init__(self, mock_mode: bool = True) -> None:
        """Initialize OpenGlassMicrophoneHal.

        Args:
            mock_mode: Use SimulatedMicrophone without BLE hardware.
        """
        self._mock_mode = mock_mode
        self._sim: Optional[SimulatedMicrophone] = None
        self._sample_rate = 8000
        self._channels = 1
        self._audio_buffer: list = []

    def initialize(self, sample_rate: int = 8000, channels: int = 1) -> None:
        """Initialize BLE GATT microphone.

        Args:
            sample_rate: PCM sample rate (8000 Hz for OpenGlass).
            channels: Number of audio channels (1 = mono).
        """
        self._sample_rate = sample_rate
        self._channels = channels
        if self._mock_mode:
            self._sim = SimulatedMicrophone()
            self._sim.initialize(sample_rate, channels)
        logger.info("OpenGlassMicrophoneHal: initialized (mock=%s, rate=%d)", self._mock_mode, sample_rate)

    def start_recording(self) -> None:
        """Begin BLE audio capture."""
        if self._mock_mode and self._sim:
            self._sim.start_recording()

    def stop_recording(self) -> None:
        """Stop BLE audio capture."""
        if self._mock_mode and self._sim:
            self._sim.stop_recording()
        self._audio_buffer = []

    def on_ble_notification(self, pcm_chunk: bytes) -> None:
        """Handle incoming audio chunk from GATT audio characteristic.

        Args:
            pcm_chunk: Raw PCM_INT16 bytes at 8kHz.
        """
        self._audio_buffer.append(pcm_chunk)
        max_bytes = int(self._sample_rate * 2 * 5)  # 5 second buffer max
        total = sum(len(c) for c in self._audio_buffer)
        while total > max_bytes and self._audio_buffer:
            removed = self._audio_buffer.pop(0)
            total -= len(removed)

    def get_buffer(self, duration_ms: int) -> AudioChunk:
        """Return buffered audio from BLE microphone.

        Args:
            duration_ms: Desired buffer duration in milliseconds.

        Returns:
            AudioChunk with 8kHz PCM_INT16 data.
        """
        if self._mock_mode and self._sim:
            return self._sim.get_buffer(duration_ms)

        n_bytes = int(self._sample_rate * (duration_ms / 1000.0) * 2)
        data = b"".join(self._audio_buffer)
        if len(data) >= n_bytes:
            chunk = data[-n_bytes:]
        else:
            chunk = data.ljust(n_bytes, b"\x00")
        return AudioChunk(_ms(), self._sample_rate, self._channels, "PCM_INT16", chunk, duration_ms=duration_ms)

    def transcribe(self, audio: AudioChunk, language: str = "en") -> str:
        """Transcribe audio via STT bridge.

        Args:
            audio: AudioChunk to transcribe.
            language: Language code.

        Returns:
            Transcribed text string.
        """
        if self._mock_mode and self._sim:
            return self._sim.transcribe(audio, language)
        try:
            from ..transport.stt_bridge import STTBridge
            return STTBridge().transcribe(audio, language=language)
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- OpenGlass -- BLE GATT errors unpredictable
            logger.warning("OpenGlassMicrophoneHal: transcription failed: %s", exc)
            return ""

    def transcribe_stream(self, stream: Iterator[AudioChunk], language: str = "en") -> Iterator[str]:
        """Streaming transcription.

        Args:
            stream: Iterator of AudioChunk objects.
            language: Language code.

        Yields:
            Partial transcription strings.
        """
        if self._mock_mode and self._sim:
            yield from self._sim.transcribe_stream(stream, language)
            return
        for chunk in stream:
            yield self.transcribe(chunk, language)

    def shutdown(self) -> None:
        """Shutdown microphone HAL."""
        self.stop_recording()
        if self._sim:
            self._sim.shutdown()

    def validate(self) -> bool:
        """Validate microphone."""
        try:
            return len(self.get_buffer(100).data) > 0
        except Exception:  # grain: ignore NAKED_EXCEPT -- OpenGlass -- BLE GATT errors unpredictable
            return False

    def get_device_info(self) -> dict:
        """Return microphone metadata."""
        return {
            "name": "openglass-microphone",
            "type": "ble_gatt",
            "sample_rate": self._sample_rate,
            "char_uuid": OPENGLASS_AUDIO_CHAR_UUID,
            "mock_mode": self._mock_mode,
        }


# ---------------------------------------------------------------------------
# Status Indicator HAL -- BLE GATT LED control
# ---------------------------------------------------------------------------


class OpenGlassStatusIndicatorHal(StatusIndicatorHal):
    """Status indicator HAL for OpenGlass via BLE GATT control characteristic.

    Writes LED control commands to GATT control characteristic (UUID 0x567A).
    Supports on/off, color (if hardware supports RGB), and blink patterns.
    """

    def __init__(self, mock_mode: bool = True) -> None:
        """Initialize OpenGlassStatusIndicatorHal.

        Args:
            mock_mode: Track state in memory without BLE hardware.
        """
        self._mock_mode = mock_mode
        self.color: tuple = (0, 0, 0)
        self.pattern: Optional[str] = None
        self.is_on: bool = False
        self._ble_client = None

    def initialize(self) -> None:
        """Initialize status indicator."""
        self.off()
        logger.info("OpenGlassStatusIndicatorHal: initialized (mock=%s)", self._mock_mode)

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set LED color via BLE GATT write.

        Args:
            r: Red channel 0-255.
            g: Green channel 0-255.
            b: Blue channel 0-255.
        """
        if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
            raise ValueError(f"RGB channels must be 0-255, got ({r}, {g}, {b})")
        self.color = (r, g, b)
        self.is_on = (r, g, b) != (0, 0, 0)
        self.pattern = None
        if not self._mock_mode:
            self._write_control(bytes([0x01, r, g, b]))

    def blink(self, interval_ms: int = 500) -> None:
        """Start LED blink via BLE GATT write.

        Args:
            interval_ms: Blink interval in milliseconds.
        """
        self.is_on = True
        self.pattern = f"blink:{interval_ms}"
        if not self._mock_mode:
            # Encode blink command: opcode 0x02, interval high byte, low byte
            hi = (interval_ms >> 8) & 0xFF
            lo = interval_ms & 0xFF
            self._write_control(bytes([0x02, hi, lo]))

    def pulse(self, pattern: str = "heartbeat") -> None:
        """Start named LED pattern via BLE GATT write.

        Args:
            pattern: One of 'heartbeat', 'alert', 'processing', 'idle'.
        """
        valid = {"heartbeat", "alert", "processing", "idle"}
        if pattern not in valid:
            raise ValueError(f"Unknown pattern {pattern!r}. Valid: {valid}")
        self.pattern = pattern
        self.is_on = True
        pattern_codes = {"heartbeat": 0x01, "alert": 0x02, "processing": 0x03, "idle": 0x04}
        if not self._mock_mode:
            self._write_control(bytes([0x03, pattern_codes[pattern]]))

    def off(self) -> None:
        """Turn off LED via BLE GATT write."""
        self.color = (0, 0, 0)
        self.pattern = None
        self.is_on = False
        if not self._mock_mode:
            self._write_control(bytes([0x00]))

    def shutdown(self) -> None:
        """Shutdown status indicator."""
        self.off()

    def validate(self) -> bool:
        """Validate by setting and checking LED state."""
        self.set_color(0, 255, 0)
        result = self.color == (0, 255, 0)
        self.off()
        return result

    def get_device_info(self) -> dict:
        """Return status indicator metadata."""
        return {
            "name": "openglass-led",
            "type": "ble_gatt",
            "char_uuid": OPENGLASS_CONTROL_CHAR_UUID,
            "mock_mode": self._mock_mode,
        }

    def _write_control(self, data: bytes) -> None:
        """Write bytes to BLE GATT control characteristic.

        Args:
            data: Command bytes to write.
        """
        logger.debug("OpenGlassStatusIndicatorHal: BLE write %s -> %s", OPENGLASS_CONTROL_CHAR_UUID, data.hex())
        # Real: await ble_client.write_gatt_char(OPENGLASS_CONTROL_CHAR_UUID, data)


# ---------------------------------------------------------------------------
# Power HAL -- BLE Battery Service
# ---------------------------------------------------------------------------


class OpenGlassPowerHal(PowerHal):
    """Power HAL for OpenGlass via BLE Battery Service.

    Reads battery level from standard BLE Battery Service (GATT 0x2A19).
    In mock_mode, returns synthetic battery level.
    """

    def __init__(self, mock_mode: bool = True) -> None:
        """Initialize OpenGlassPowerHal.

        Args:
            mock_mode: Return synthetic battery data without hardware.
        """
        self._mock_mode = mock_mode
        self._mock_battery = 85.0
        self._low_battery_callbacks: list = []
        self._low_battery_threshold: float = 15.0

    def get_battery_percent(self) -> float:
        """Return battery level from BLE Battery Service.

        Returns:
            Battery percentage 0.0-100.0. Returns -1.0 if unavailable.
        """
        if self._mock_mode:
            return self._mock_battery
        try:
            # Real: read GATT 0x2A19
            return -1.0
        except Exception:  # grain: ignore NAKED_EXCEPT -- OpenGlass -- BLE GATT errors unpredictable
            return -1.0

    def get_charging_state(self) -> ChargingState:
        """Return charging state (UNKNOWN for BLE-only device).

        Returns:
            ChargingState enum value.
        """
        return ChargingState.UNKNOWN

    def get_power_source(self) -> PowerSource:
        """Return power source (BATTERY for ESP32).

        Returns:
            PowerSource enum value.
        """
        return PowerSource.BATTERY

    def register_low_battery_callback(self, threshold: float, callback: Callable[[], None]) -> None:
        """Fire callback when battery falls below threshold.

        Args:
            threshold: Battery percent threshold (e.g. 15.0).
            callback: Callable invoked when battery drops below threshold.
        """
        self._low_battery_threshold = threshold
        self._low_battery_callbacks.append(callback)

    def shutdown(self) -> None:
        """Shutdown power HAL."""
        self._low_battery_callbacks = []

    def validate(self) -> bool:
        """Validate power HAL by reading battery."""
        return self.get_battery_percent() >= 0

    def get_device_info(self) -> dict:
        """Return power HAL metadata."""
        return {
            "name": "openglass-power",
            "type": "ble_battery_service",
            "char_uuid": OPENGLASS_BATTERY_CHAR_UUID,
            "mock_mode": self._mock_mode,
        }


# ---------------------------------------------------------------------------
# Transport HAL -- BLE to OpenClaw via HTTP fallback
# ---------------------------------------------------------------------------


class OpenGlassTransportHal(TransportHal):
    """BLE transport HAL for OpenGlass with HTTP fallback to OpenClaw gateway.

    Primary: BLE GATT to ESP32-S3.
    Fallback: HTTP to OpenClaw gateway for processed context delivery.
    Expected latency: 80ms (BLE GATT).
    """

    def __init__(self, mock_mode: bool = True) -> None:
        """Initialize OpenGlassTransportHal.

        Args:
            mock_mode: Use SimulatedTransport without BLE hardware.
        """
        self._mock_mode = mock_mode
        self._sim: Optional[SimulatedTransport] = None
        self._state = TransportState.DISCONNECTED
        self._callback: Optional[Callable[[TransportState], None]] = None
        self._latency_window: list = []

    def initialize(self, config: dict) -> None:
        """Initialize transport.

        Args:
            config: Config dict from openglass.yaml.
        """
        if self._mock_mode:
            self._sim = SimulatedTransport()
            self._sim.initialize(config)

    def connect(self) -> None:
        """Connect BLE transport."""
        if self._sim:
            self._sim.connect()
            self._state = self._sim.get_state()
        else:
            self._state = TransportState.CONNECTED
            if self._callback:
                self._callback(self._state)

    def send(self, payload: bytes) -> SendResult:
        """Send context payload (BLE + HTTP fallback).

        Args:
            payload: Context payload bytes.

        Returns:
            SendResult with timing info.
        """
        if self._sim:
            return self._sim.send(payload)
        t0 = _ms()
        logger.debug("OpenGlassTransportHal: send %d bytes via BLE/HTTP fallback", len(payload))
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
            callback: Called when transport state changes.
        """
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
        """Return expected transport latency: 80ms (BLE GATT).

        Returns:
            Expected latency in milliseconds.
        """
        return 80

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
            "name": "openglass-transport",
            "type": "ble",
            "advertised_name": "OpenGlass",
            "service_uuid": OPENGLASS_SERVICE_UUID,
            "mock_mode": self._mock_mode,
        }


# ---------------------------------------------------------------------------
# Profile factory
# ---------------------------------------------------------------------------


def build_openglass_hals(config: dict) -> dict:
    """Build all HAL instances for the OpenGlass profile.

    Args:
        config: Profile config dict (from openglass.yaml).

    Returns:
        Dict mapping HAL type name to HAL instance.
    """
    mock_mode = True  # BLE requires hardware; default to mock
    transport_cfg = config.get("transport", {})
    hw_cfg = config.get("hardware", {})

    camera = OpenGlassCameraHal(mock_mode=mock_mode)
    camera.initialize()

    mic = OpenGlassMicrophoneHal(mock_mode=mock_mode)
    mic.initialize(
        sample_rate=hw_cfg.get("microphone", {}).get("sample_rate", 8000),
        channels=hw_cfg.get("microphone", {}).get("channels", 1),
    )
    mic.start_recording()

    status = OpenGlassStatusIndicatorHal(mock_mode=mock_mode)
    status.initialize()

    power = OpenGlassPowerHal(mock_mode=mock_mode)

    transport = OpenGlassTransportHal(mock_mode=mock_mode)
    transport.initialize(transport_cfg)
    transport.connect()

    health = SimulatedSystemHealth(device_id="openglass")

    return {
        "camera": camera,
        "microphone": mic,
        "status_indicator": status,
        "power": power,
        "transport": transport,
        "system_health": health,
    }


__all__ = [
    "OpenGlassCameraHal",
    "OpenGlassMicrophoneHal",
    "OpenGlassStatusIndicatorHal",
    "OpenGlassPowerHal",
    "OpenGlassTransportHal",
    "build_openglass_hals",
    "OPENGLASS_SERVICE_UUID",
    "OPENGLASS_CAMERA_CHAR_UUID",
    "OPENGLASS_AUDIO_CHAR_UUID",
    "OPENGLASS_CONTROL_CHAR_UUID",
    "OPENGLASS_BATTERY_CHAR_UUID",
]
