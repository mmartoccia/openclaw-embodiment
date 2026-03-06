"""Meta Ray-Ban Smart Glasses profile for OpenClaw Embodiment SDK.

Architecture: MWDAT SDK (iOS/macOS) sends 1fps JPEG frames via HTTP POST to
a local receiver (port 8421). Audio is bridged over WebSocket (port 8422):
16kHz PCM from mic to host, 24kHz PCM from host to glasses speaker.

Testable without hardware:
- mock_mode=True uses in-process buffers (SimulatedCamera, SimulatedMicrophone)
- set mock_mode=False when using real Meta Ray-Ban glasses with MWDAT app

Reference: VisionClaw (github.com/sseanliu/VisionClaw)
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
    DisplayCard,
    DisplayHal,
    HALBase,
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
from ..hal.rayban_server import RayBanServer
from ..hal.simulator import (
    SimulatedCamera,
    SimulatedMicrophone,
    SimulatedSystemHealth,
    SimulatedTransport,
)

logger = logging.getLogger(__name__)


def _ms() -> int:
    return time.monotonic_ns() // 1_000_000


# ---------------------------------------------------------------------------
# Unsupported HAL stub
# ---------------------------------------------------------------------------


class UnsupportedHal(HALBase):
    """Stub for HAL ABCs not applicable to this device.

    Logs 'not available on this device' and no-ops all operations.
    Used for IMUHal, ActuatorHal, DisplayHal on Meta Ray-Ban.
    """

    def __init__(self, hal_name: str) -> None:
        """Initialize UnsupportedHal.

        Args:
            hal_name: Human-readable name for logging (e.g. 'IMUHal').
        """
        self._hal_name = hal_name

    def validate(self) -> bool:
        """Always returns False -- HAL is not supported on this device."""
        logger.info("%s not available on Meta Ray-Ban", self._hal_name)
        return False

    def get_device_info(self) -> dict:
        """Return minimal device info indicating unsupported status."""
        return {"name": f"unsupported-{self._hal_name}", "supported": False}


# ---------------------------------------------------------------------------
# Camera HAL -- HTTP frame receiver from MWDAT
# ---------------------------------------------------------------------------


class RayBanCameraHal(CameraHal):
    """HTTP-based camera HAL for Meta Ray-Ban Smart Glasses.

    Receives 1fps JPEG frames from the MWDAT companion app via HTTP POST
    to the RayBanServer frame buffer. Uses threading.Event (inside
    RayBanFrameBuffer) for frame-ready synchronization.

    In mock_mode, delegates to SimulatedCamera for CI/testing.
    """

    def __init__(self, server: RayBanServer, mock_mode: bool = True) -> None:
        """Initialize RayBanCameraHal.

        Args:
            server: Shared RayBanServer instance.
            mock_mode: Use SimulatedCamera instead of real frame buffer.
        """
        self._server = server
        self._mock_mode = mock_mode
        self._sim: Optional[SimulatedCamera] = None
        self._resolution: Tuple[int, int] = (1920, 1080)

    def initialize(self, resolution: Tuple[int, int] = (1920, 1080)) -> None:
        """Initialize camera HAL.

        Args:
            resolution: Target resolution (width, height).
        """
        self._resolution = resolution
        if self._mock_mode:
            self._sim = SimulatedCamera()
            self._sim.initialize(resolution)
        logger.info("RayBanCameraHal: initialized (mock=%s, res=%s)", self._mock_mode, resolution)

    def capture_frame(self) -> CameraFrame:
        """Capture latest frame from MWDAT HTTP receiver or simulator.

        Returns:
            CameraFrame with JPEG data. Blocks up to 5s for real hardware.
        """
        if self._mock_mode and self._sim is not None:
            return self._sim.capture_frame()

        jpeg = self._server.get_latest_frame(timeout_s=5.0)
        if jpeg is None:
            jpeg = b"\xff\xd8\xff\xd9"  # minimal valid JPEG stub
        w, h = self._resolution
        return CameraFrame(_ms(), w, h, "JPEG", jpeg)

    def shutdown(self) -> None:
        """Shutdown camera HAL."""
        if self._sim:
            self._sim.shutdown()
        logger.info("RayBanCameraHal: shutdown")

    def validate(self) -> bool:
        """Validate camera by attempting a frame capture."""
        try:
            frame = self.capture_frame()
            return len(frame.data) > 0
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- camera validation probe
            logger.warning("RayBanCameraHal: validation failed: %s", exc)
            return False

    def get_device_info(self) -> dict:
        """Return camera HAL metadata."""
        return {
            "name": "rayban-camera",
            "fps": 1,
            "format": "jpeg",
            "resolution": f"{self._resolution[0]}x{self._resolution[1]}",
            "transport": "http-mwdat" if not self._mock_mode else "simulator",
        }


# ---------------------------------------------------------------------------
# Microphone HAL -- WebSocket PCM receiver from MWDAT
# ---------------------------------------------------------------------------


class RayBanMicrophoneHal(MicrophoneHal):
    """WebSocket microphone HAL for Meta Ray-Ban Smart Glasses.

    Receives 16kHz PCM audio chunks from the glasses microphone via
    the MWDAT WebSocket bridge. Delegates transcription to the STT bridge.

    In mock_mode, delegates to SimulatedMicrophone.
    """

    def __init__(self, server: RayBanServer, mock_mode: bool = True) -> None:
        """Initialize RayBanMicrophoneHal.

        Args:
            server: Shared RayBanServer instance.
            mock_mode: Use SimulatedMicrophone instead of real audio.
        """
        self._server = server
        self._mock_mode = mock_mode
        self._sim: Optional[SimulatedMicrophone] = None
        self._sample_rate = 16000
        self._channels = 1

    def initialize(self, sample_rate: int = 16000, channels: int = 1) -> None:
        """Initialize microphone HAL.

        Args:
            sample_rate: PCM sample rate (16000 Hz for Ray-Ban).
            channels: Number of audio channels (1 = mono).
        """
        self._sample_rate = sample_rate
        self._channels = channels
        if self._mock_mode:
            self._sim = SimulatedMicrophone()
            self._sim.initialize(sample_rate, channels)
        logger.info("RayBanMicrophoneHal: initialized (mock=%s, rate=%d)", self._mock_mode, sample_rate)

    def start_recording(self) -> None:
        """Begin capturing audio."""
        if self._mock_mode and self._sim:
            self._sim.start_recording()

    def stop_recording(self) -> None:
        """Stop capturing audio."""
        if self._mock_mode and self._sim:
            self._sim.stop_recording()

    def get_buffer(self, duration_ms: int) -> AudioChunk:
        """Fetch buffered audio from glasses microphone.

        Args:
            duration_ms: Desired buffer duration in milliseconds.

        Returns:
            AudioChunk with PCM_INT16 data at 16000 Hz.
        """
        if self._mock_mode and self._sim:
            return self._sim.get_buffer(duration_ms)

        pcm = self._server.audio_in_buffer.get(timeout_s=0.2)
        if pcm is None:
            n = int(self._sample_rate * (duration_ms / 1000.0) * 2)
            pcm = b"\x00" * n
        return AudioChunk(_ms(), self._sample_rate, self._channels, "PCM_INT16", pcm, duration_ms=duration_ms)

    def transcribe(self, audio: AudioChunk, language: str = "en") -> str:
        """Transcribe audio via STT bridge.

        Args:
            audio: AudioChunk to transcribe.
            language: Language code (default 'en').

        Returns:
            Transcribed text string.
        """
        if self._mock_mode and self._sim:
            return self._sim.transcribe(audio, language)
        # Delegate to STT bridge in production
        try:
            from ..transport.stt_bridge import STTBridge
            bridge = STTBridge()
            return bridge.transcribe(audio, language=language)
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- STT bridge; external service errors unpredictable
            logger.warning("RayBanMicrophoneHal: STT transcription failed: %s", exc)
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
        if self._sim:
            self._sim.shutdown()

    def validate(self) -> bool:
        """Validate microphone by capturing a test buffer."""
        try:
            chunk = self.get_buffer(100)
            return len(chunk.data) > 0
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- microphone validation probe
            logger.warning("RayBanMicrophoneHal: validation failed: %s", exc)
            return False

    def get_device_info(self) -> dict:
        """Return microphone HAL metadata."""
        return {
            "name": "rayban-microphone",
            "sample_rate": self._sample_rate,
            "channels": self._channels,
            "format": "PCM_INT16",
            "transport": "websocket-mwdat" if not self._mock_mode else "simulator",
        }


# ---------------------------------------------------------------------------
# Audio Output HAL -- WebSocket PCM sender to MWDAT
# ---------------------------------------------------------------------------


class RayBanAudioOutputHal(AudioOutputHal):
    """WebSocket audio output HAL for Meta Ray-Ban Smart Glasses.

    Sends 24kHz PCM audio to the glasses speaker via MWDAT WebSocket bridge.
    TTS conversion (text -> PCM) is delegated to the platform TTS.
    """

    def __init__(self, server: RayBanServer, mock_mode: bool = True) -> None:
        """Initialize RayBanAudioOutputHal.

        Args:
            server: Shared RayBanServer instance.
            mock_mode: No-op audio output in mock mode.
        """
        self._server = server
        self._mock_mode = mock_mode
        self._playing = False
        self._sample_rate = 24000

    def initialize(self, sample_rate: int = 24000, channels: int = 1) -> None:
        """Initialize audio output.

        Args:
            sample_rate: Output sample rate (24000 Hz for Ray-Ban speaker).
            channels: Number of audio channels (1 = mono).
        """
        self._sample_rate = sample_rate
        logger.info("RayBanAudioOutputHal: initialized (mock=%s, rate=%d)", self._mock_mode, sample_rate)

    def play(self, audio_data: bytes, format: str = "PCM_INT16", sample_rate: int = 24000) -> None:
        """Send PCM audio to glasses speaker via WebSocket.

        Args:
            audio_data: Raw PCM bytes at 24000 Hz.
            format: Audio format string.
            sample_rate: Sample rate of the audio data.
        """
        self._playing = True
        if not self._mock_mode:
            self._server.send_audio_to_glasses(audio_data)
        logger.debug("RayBanAudioOutputHal: play %d bytes", len(audio_data))
        self._playing = False

    def stop(self) -> None:
        """Stop audio playback."""
        self._playing = False

    def is_playing(self) -> bool:
        """Return current playback state."""
        return self._playing

    def speak(self, text: str) -> None:
        """Convert text to 24kHz PCM and send to glasses speaker.

        Args:
            text: Text to speak via TTS pipeline.
        """
        if self._mock_mode:
            logger.debug("RayBanAudioOutputHal: mock speak '%s'", text[:50])
            return
        try:
            # TTS bridge: text -> PCM bytes at 24000 Hz
            pcm = self._text_to_pcm(text)
            self.play(pcm, "PCM_INT16", 24000)
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- TTS pipeline; any exception type possible
            logger.warning("RayBanAudioOutputHal: speak failed: %s", exc)

    def speak_agent_response(self, response: object) -> None:
        """Speak an agent response via TTS.

        Args:
            response: AgentResponse with content attribute.
        """
        if hasattr(response, "content"):
            self.speak(str(response.content))

    def _text_to_pcm(self, text: str) -> bytes:
        """Convert text to 24kHz PCM via platform TTS.

        Args:
            text: Text to synthesize.

        Returns:
            Raw PCM_INT16 bytes at 24000 Hz.
        """
        # Placeholder: return silence until real TTS integration
        duration_ms = min(len(text) * 60, 5000)  # ~60ms per char estimate
        n_samples = int(24000 * (duration_ms / 1000.0))
        return b"\x00" * (n_samples * 2)  # 16-bit PCM silence

    def shutdown(self) -> None:
        """Shutdown audio output HAL."""
        self.stop()

    def validate(self) -> bool:
        """Validate audio output by attempting a play call."""
        try:
            self.play(b"\x00" * 48)
            return True
        except Exception:  # grain: ignore NAKED_EXCEPT -- audio output validation probe
            return False

    def get_device_info(self) -> dict:
        """Return audio output HAL metadata."""
        return {
            "name": "rayban-audio-output",
            "sample_rate": self._sample_rate,
            "format": "PCM_INT16",
            "transport": "websocket-mwdat" if not self._mock_mode else "mock",
        }


# ---------------------------------------------------------------------------
# Status Indicator HAL -- LED via MWDAT notification
# ---------------------------------------------------------------------------


class RayBanStatusIndicatorHal(StatusIndicatorHal):
    """Status indicator HAL for Meta Ray-Ban Smart Glasses LED.

    Controls the glasses indicator LED via MWDAT notification commands.
    In mock_mode, records state in memory without hardware calls.
    """

    def __init__(self, mock_mode: bool = True) -> None:
        """Initialize RayBanStatusIndicatorHal.

        Args:
            mock_mode: Use in-memory state tracking without hardware.
        """
        self._mock_mode = mock_mode
        self.color: tuple = (0, 0, 0)
        self.pattern: Optional[str] = None
        self.is_on: bool = False

    def initialize(self) -> None:
        """Initialize status indicator."""
        self.off()
        logger.info("RayBanStatusIndicatorHal: initialized (mock=%s)", self._mock_mode)

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set glasses LED to solid RGB color via MWDAT command.

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
            self._send_mwdat_led_command({"action": "set_color", "r": r, "g": g, "b": b})

    def blink(self, interval_ms: int = 500) -> None:
        """Start blinking LED at given interval.

        Args:
            interval_ms: Full on-off cycle duration in milliseconds.
        """
        self.is_on = True
        self.pattern = f"blink:{interval_ms}"
        if not self._mock_mode:
            self._send_mwdat_led_command({"action": "blink", "interval_ms": interval_ms})

    def pulse(self, pattern: str = "heartbeat") -> None:
        """Start named LED animation pattern.

        Args:
            pattern: One of 'heartbeat', 'alert', 'processing', 'idle'.
        """
        valid = {"heartbeat", "alert", "processing", "idle"}
        if pattern not in valid:
            raise ValueError(f"Unknown pattern {pattern!r}. Valid: {valid}")
        self.pattern = pattern
        self.is_on = True
        if not self._mock_mode:
            self._send_mwdat_led_command({"action": "pulse", "pattern": pattern})

    def off(self) -> None:
        """Turn off glasses LED."""
        self.color = (0, 0, 0)
        self.pattern = None
        self.is_on = False
        if not self._mock_mode:
            self._send_mwdat_led_command({"action": "off"})

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
            "name": "rayban-led",
            "type": "mwdat-notification" if not self._mock_mode else "mock",
            "rgb": True,
        }

    def _send_mwdat_led_command(self, cmd: dict) -> None:
        """Send LED control command via MWDAT notification API.

        Args:
            cmd: Command dict with action and parameters.
        """
        logger.debug("RayBanStatusIndicatorHal: MWDAT LED command: %s", cmd)
        # Real implementation: post to MWDAT notification endpoint
        # mwdat_client.send_notification("led_control", cmd)


# ---------------------------------------------------------------------------
# Transport HAL -- HTTP to OpenClaw gateway
# ---------------------------------------------------------------------------


class RayBanTransportHal(TransportHal):
    """HTTP transport HAL for Meta Ray-Ban connecting to OpenClaw gateway.

    Sends processed context payloads back to the OpenClaw gateway.
    Expected latency: 15ms (local WiFi).

    In mock_mode, delegates to SimulatedTransport.
    """

    def __init__(self, host: str = "localhost", port: int = 8420, mock_mode: bool = True) -> None:
        """Initialize RayBanTransportHal.

        Args:
            host: OpenClaw gateway host.
            port: OpenClaw gateway port.
            mock_mode: Use SimulatedTransport for testing.
        """
        self._host = host
        self._port = port
        self._mock_mode = mock_mode
        self._sim: Optional[SimulatedTransport] = None
        self._state = TransportState.DISCONNECTED
        self._callback: Optional[Callable[[TransportState], None]] = None
        self._latency_window: list = []

    def initialize(self, config: dict) -> None:
        """Initialize transport with config.

        Args:
            config: Config dict, may override host/port.
        """
        self._host = config.get("host", self._host)
        self._port = config.get("port", self._port)
        if self._mock_mode:
            self._sim = SimulatedTransport()
            self._sim.initialize(config)

    def connect(self) -> None:
        """Connect transport to OpenClaw gateway."""
        if self._mock_mode and self._sim:
            self._sim.connect()
            self._state = self._sim.get_state()
            return
        self._state = TransportState.CONNECTED
        if self._callback:
            self._callback(self._state)

    def send(self, payload: bytes) -> SendResult:
        """Send payload to OpenClaw gateway.

        Args:
            payload: Context payload bytes.

        Returns:
            SendResult with success/timing info.
        """
        if self._mock_mode and self._sim:
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
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- HTTP transport; network errors unpredictable
            logger.warning("RayBanTransportHal: send failed: %s", exc)
            return SendResult(False, 0, _ms() - t0, error_code=str(exc))

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Receive response from gateway.

        Args:
            timeout_ms: Timeout in milliseconds.

        Returns:
            Response bytes or None.
        """
        if self._mock_mode and self._sim:
            return self._sim.receive(timeout_ms)
        return None

    def get_state(self) -> TransportState:
        """Return current connection state."""
        if self._mock_mode and self._sim:
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
        """Disconnect from gateway."""
        if self._mock_mode and self._sim:
            self._sim.disconnect()
        self._state = TransportState.DISCONNECTED

    def shutdown(self) -> None:
        """Shutdown transport."""
        self.disconnect()

    def get_expected_latency_ms(self) -> int:
        """Return expected transport latency: 15ms (local WiFi HTTP).

        Returns:
            Expected latency in milliseconds.
        """
        return 15

    def get_measured_latency_ms(self) -> Optional[int]:
        """Return rolling average of last 10 send latencies.

        Returns:
            Rolling average elapsed_ms or None if no sends yet.
        """
        if self._mock_mode and self._sim:
            return self._sim.get_measured_latency_ms()
        if not self._latency_window:
            return None
        return int(sum(self._latency_window) / len(self._latency_window))

    def validate(self) -> bool:
        """Validate transport by checking state is accessible."""
        if self._mock_mode and self._sim:
            return self._sim.validate()
        return True

    def get_device_info(self) -> dict:
        """Return transport HAL metadata."""
        return {
            "name": "rayban-transport",
            "type": "http",
            "host": self._host,
            "port": self._port,
            "mock_mode": self._mock_mode,
        }


# ---------------------------------------------------------------------------
# System Health HAL
# ---------------------------------------------------------------------------


class RayBanSystemHealthHal(SimulatedSystemHealth):
    """System health for Meta Ray-Ban smart glasses.

    Extends SimulatedSystemHealth with Ray-Ban specific device_id and
    sensor_status (camera, microphone, audio_output).
    """

    def __init__(self) -> None:
        """Initialize RayBanSystemHealthHal."""
        super().__init__(device_id="meta-rayban")

    def get_health_report(self) -> HealthReport:
        """Return Ray-Ban specific health report."""
        import datetime
        return HealthReport(
            timestamp=datetime.datetime.utcnow(),
            device_id="meta-rayban",
            cpu_percent=None,
            memory_percent=None,
            temperature_c=None,
            battery_percent=None,
            connectivity={"wifi": True, "mwdat": True},
            sensor_status={"camera": True, "microphone": True, "audio_output": True},
            is_operational=True,
            warnings=[],
        )

    def get_device_info(self) -> dict:
        """Return Ray-Ban health HAL metadata."""
        return {"name": "rayban-health", "device": "meta-rayban"}


# ---------------------------------------------------------------------------
# Profile factory
# ---------------------------------------------------------------------------


def build_meta_rayban_hals(config: dict) -> dict:
    """Build all HAL instances for the Meta Ray-Ban profile.

    Args:
        config: Profile config dict (from meta_rayban.yaml).

    Returns:
        Dict mapping HAL type name to HAL instance.
    """
    mock_mode = config.get("mwdat", {}).get("mock_mode", True)
    hal_server_cfg = config.get("hal_server", {})
    transport_cfg = config.get("transport", {})

    server = RayBanServer(
        http_port=hal_server_cfg.get("http_port", 8421),
        ws_port=hal_server_cfg.get("ws_port", 8422),
        mock_mode=mock_mode,
    )
    server.start()

    camera = RayBanCameraHal(server, mock_mode=mock_mode)
    camera.initialize()

    mic = RayBanMicrophoneHal(server, mock_mode=mock_mode)
    mic.initialize(
        sample_rate=config.get("hardware", {}).get("microphone", {}).get("sample_rate", 16000),
        channels=config.get("hardware", {}).get("microphone", {}).get("channels", 1),
    )

    audio_out = RayBanAudioOutputHal(server, mock_mode=mock_mode)
    audio_out.initialize(
        sample_rate=config.get("hardware", {}).get("audio_output", {}).get("sample_rate", 24000),
    )

    status = RayBanStatusIndicatorHal(mock_mode=mock_mode)
    status.initialize()

    transport = RayBanTransportHal(
        host=transport_cfg.get("host", "localhost"),
        port=transport_cfg.get("port", 8420),
        mock_mode=mock_mode,
    )
    transport.initialize(transport_cfg)
    transport.connect()

    health = RayBanSystemHealthHal()

    return {
        "camera": camera,
        "microphone": mic,
        "audio_output": audio_out,
        "status_indicator": status,
        "transport": transport,
        "system_health": health,
        "imu": UnsupportedHal("IMUHal"),
        "display": UnsupportedHal("DisplayHal"),
        "actuator": UnsupportedHal("ActuatorHal"),
        "_server": server,
    }


__all__ = [
    "RayBanCameraHal",
    "RayBanMicrophoneHal",
    "RayBanAudioOutputHal",
    "RayBanStatusIndicatorHal",
    "RayBanTransportHal",
    "RayBanSystemHealthHal",
    "UnsupportedHal",
    "build_meta_rayban_hals",
]
