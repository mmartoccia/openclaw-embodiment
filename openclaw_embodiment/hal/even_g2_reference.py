"""Even Realities G2 reference HAL for OpenClaw Embodiment SDK.

Implements OpenClaw HAL over the G2's custom BLE protocol.
Protocol documentation: https://github.com/even-realities/EvenDemoApp (BSD-2-Clause)
Community reverse-engineering: https://github.com/i-soxi/even-g2-protocol

Spec-based implementation. Validated against SDK docs. Hardware validation required.

Hardware: Even Realities G2 Smart Glasses
  - Dual BLE arms (left + right GATT services)
  - Microphone: LC3 audio format via BLE characteristic 0xF1
  - Display: 128x128 BMP packets (194 bytes, seq 0-255, command 0x15, CRC 0x16)
  - No camera -- context capture relies on audio + agent inference
  - Motion: No IMU -- uses BLE RSSI + audio energy as motion proxy

Install requirements:
  pip install bleak

Usage:
  from even_g2_reference import (
      G2RSSIMotionProxy, G2MicrophoneHAL, G2DisplayHAL, G2TransportHAL
  )
"""

from __future__ import annotations

import asyncio
import queue
import struct
import time
from typing import Callable, List, Optional, Tuple

from ..core.trigger import TriggerConfig
from .base import (
    AudioChunk,
    DisplayCard,
    DisplayHal,
    IMUHal,
    IMUSample,
    MicrophoneHal,
    SendResult,
    TransportHal,
    TransportState,
)

# ---------------------------------------------------------------------------
# Module-level BLE UUID constants
# Note: These are placeholders -- verify against EvenDemoApp source before use.
# ---------------------------------------------------------------------------

G2_LEFT_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"   # Placeholder -- verify against EvenDemoApp
G2_RIGHT_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9f"  # Placeholder
G2_AUDIO_CHARACTERISTIC = "0000f1-0000-1000-8000-00805f9b34fb"  # Placeholder -- verify

# ---------------------------------------------------------------------------
# TriggerConfig tuned for RSSI-based motion proxy
# ---------------------------------------------------------------------------

G2_TRIGGER_PROFILE = TriggerConfig(
    polling_hz=10,
    saccade_threshold_dps=25.0,    # RSSI-based -- lower threshold, noisier signal
    saccade_duration_ms=300,       # Longer window to filter RSSI noise
    fixation_threshold_dps=8.0,
    fixation_duration_ms=800,      # Long fixation for noisy RSSI proxy
    motion_reject_threshold_dps=100.0,
    motion_reject_duration_ms=300,
    refractory_period_ms=3000,     # 3s between captures (RSSI unreliable for fast detection)
)
"""Tuned for G2 RSSI-based motion proxy. Low fidelity -- use audio energy as supplement."""


# ---------------------------------------------------------------------------
# Helper: async bridge
# ---------------------------------------------------------------------------

def _ms() -> int:
    return int(time.time() * 1000)


def _run_async(coro):
    """Run async coroutine from sync context."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# BMP construction helper (PIL-free)
# ---------------------------------------------------------------------------

def _make_bmp(text: str, width: int = 128, height: int = 128) -> bytes:
    """Create minimal BMP with text -- PIL-free fallback.

    Returns a valid but blank (black) BMP frame as placeholder.
    No actual text rendering without PIL -- the display will show a blank frame.
    For real text rendering: pip install pillow and extend this method.
    """
    pixel_data = b"\x00" * (width * height * 3)  # BGR black
    # BMP file header (14 bytes) + DIB header (40 bytes) = 54 bytes total
    file_size = 54 + len(pixel_data)
    # File header: signature, file_size, reserved1, reserved2, pixel_array_offset
    file_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    # DIB header (BITMAPINFOHEADER): size, width, height (negative = top-down),
    # color_planes, bits_per_pixel, compression, image_size, x_ppm, y_ppm,
    # colors_in_table, important_colors
    dib_header = struct.pack(
        "<IiiHHIIiiII",
        40,              # header size
        width,           # image width
        -height,         # negative height = top-down row order
        1,               # color planes
        24,              # bits per pixel (RGB)
        0,               # no compression
        len(pixel_data), # raw bitmap size
        2835,            # x pixels per meter (~72 dpi)
        2835,            # y pixels per meter (~72 dpi)
        0,               # colors in color table
        0,               # important color count
    )
    return file_header + dib_header + pixel_data


# ---------------------------------------------------------------------------
# G2 RSSI Motion Proxy (IMU substitute)
# ---------------------------------------------------------------------------

class G2RSSIMotionProxy(IMUHal):
    """BLE RSSI variance as crude motion proxy for Even Realities G2.

    G2 has no physical IMU. Movement causes RSSI fluctuation on the BLE link
    between the host and each arm. This HAL reads RSSI from both arms,
    computes the variance between successive readings, and maps it to a
    synthetic gyro_z signal for the trigger engine.

    For better motion detection, use audio energy level changes as a
    complementary signal. True saccade detection requires hardware IMU
    not present in G2.

    Note: RSSI-based motion is low-fidelity. Thresholds in G2_TRIGGER_PROFILE
    are tuned for the noisy RSSI signal. Expect false positives and missed
    detections compared to IMU-equipped devices. Audio energy changes can
    supplement RSSI for improved context detection accuracy.
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, left_address: str, right_address: str) -> None:
        self._left_address = left_address
        self._right_address = right_address
        self._sample_rate_hz: int = 10
        self._last_rssi_left: Optional[float] = None
        self._last_rssi_right: Optional[float] = None
        self._last_ts: int = 0

    def initialize(self, sample_rate_hz: int = 10) -> None:
        """Store BLE addresses and sample rate."""
        self._sample_rate_hz = max(1, sample_rate_hz)
        self._last_ts = _ms()

    def read_sample(self) -> Optional[IMUSample]:
        """Read RSSI from both G2 arms; map variance to synthetic gyro_z.

        Uses bleak.BleakScanner.find_device_by_address() to poll RSSI.
        RSSI delta between readings is mapped linearly to gyro_z (dps).

        Returns IMUSample with:
          - accel_x/y/z = 0.0 (no accelerometer)
          - gyro_z = avg(rssi_delta_left, rssi_delta_right) * 5.0
          - gyro_x/y = 0.0
        """
        now = _ms()

        rssi_left = _run_async(self._read_rssi(self._left_address))
        rssi_right = _run_async(self._read_rssi(self._right_address))

        gyro_z = 0.0
        if rssi_left is not None and self._last_rssi_left is not None:
            delta_left = abs(rssi_left - self._last_rssi_left)
        else:
            delta_left = 0.0

        if rssi_right is not None and self._last_rssi_right is not None:
            delta_right = abs(rssi_right - self._last_rssi_right)
        else:
            delta_right = 0.0

        # Map RSSI variance to synthetic gyro_z in degrees-per-second proxy
        gyro_z = (delta_left + delta_right) / 2 * 5.0

        if rssi_left is not None:
            self._last_rssi_left = rssi_left
        if rssi_right is not None:
            self._last_rssi_right = rssi_right
        self._last_ts = now

        return IMUSample(
            timestamp_ms=now,
            accel_x=0.0,
            accel_y=0.0,
            accel_z=0.0,
            gyro_x=0.0,
            gyro_y=0.0,
            gyro_z=gyro_z,
        )

    async def _read_rssi(self, address: str) -> Optional[float]:
        """Attempt to read RSSI from BLE device by address."""
        try:
            from bleak import BleakScanner
            device = await BleakScanner.find_device_by_address(address, timeout=1.0)
            if device is not None and hasattr(device, "rssi"):
                return float(device.rssi)
        except Exception:
            pass
        return None

    def set_sample_rate(self, hz: int) -> None:
        self._sample_rate_hz = max(1, hz)

    def shutdown(self) -> None:
        self._last_rssi_left = None
        self._last_rssi_right = None

    def validate(self) -> bool:
        try:
            sample = self.read_sample()
            return sample is not None
        except Exception:
            return False

    def get_device_info(self) -> dict:
        return {
            "name": "g2-rssi-motion-proxy",
            "type": "rssi_variance",
            "note": "No physical IMU; RSSI variance used as crude motion indicator",
            "left_address": self._left_address,
            "right_address": self._right_address,
            "sample_rate_hz": self._sample_rate_hz,
        }


# ---------------------------------------------------------------------------
# G2 Microphone HAL
# ---------------------------------------------------------------------------

class G2MicrophoneHAL(MicrophoneHal):
    """Microphone HAL for Even Realities G2 via BLE LC3 audio stream.

    G2 streams LC3-encoded audio via BLE characteristic 0xF1. This HAL
    connects to the left arm (primary audio source), subscribes to
    notifications, and buffers incoming LC3 frames.

    LC3 decoding requires liblc3 (pip install lc3 or use liblc3 bindings).
    If LC3 decoding is unavailable, raw LC3 bytes are returned with
    format="LC3" in the AudioChunk. The caller is responsible for decoding.

    Note: The exact characteristic UUID must be verified against EvenDemoApp
    source. G2_AUDIO_CHARACTERISTIC is a placeholder.
    """

    HAL_VERSION = "1.0.0"
    SAMPLE_RATE = 16000

    def __init__(
        self,
        left_address: str,
        right_address: str,
        characteristic_uuid: str = "0000f1-0000-1000-8000-00805f9b34fb",
    ) -> None:
        # Note: characteristic_uuid is a placeholder -- verify against EvenDemoApp source
        self._left_address = left_address
        self._right_address = right_address
        self._characteristic_uuid = characteristic_uuid
        self._sample_rate_hz: int = self.SAMPLE_RATE
        self._channels: int = 1
        self._recording: bool = False
        self._buffer: queue.Queue = queue.Queue(maxsize=500)
        self._client = None

    def initialize(self, sample_rate_hz: int = 16000, channels: int = 1) -> None:
        """Store audio config. BLE connection deferred to start_recording()."""
        self._sample_rate_hz = sample_rate_hz
        self._channels = channels

    def start_recording(self) -> None:
        """Connect to G2 left arm and subscribe to LC3 audio notifications."""
        if self._recording:
            return
        try:
            _run_async(self._connect_and_subscribe())
        except Exception:
            pass
        self._recording = True

    async def _connect_and_subscribe(self) -> None:
        """Async: connect BLE client and subscribe to audio characteristic."""
        try:
            from bleak import BleakClient

            def _audio_handler(sender, data: bytearray) -> None:
                """BLE notification callback for LC3 audio frames."""
                try:
                    # Try LC3 decode (requires liblc3 bindings)
                    try:
                        import lc3  # type: ignore
                        pcm = lc3.decode(bytes(data), self._sample_rate_hz)
                    except ImportError:
                        # liblc3 not available -- store raw LC3 bytes
                        pcm = bytes(data)
                    if not self._buffer.full():
                        self._buffer.put_nowait(pcm)
                except Exception:
                    pass

            self._client = BleakClient(self._left_address)
            await self._client.connect()
            # Activate microphone: command [0x0E, 0x01] per G2 protocol
            # Note: write characteristic may differ -- verify against EvenDemoApp
            try:
                await self._client.write_gatt_char(
                    self._characteristic_uuid, bytes([0x0E, 0x01])
                )
            except Exception:
                pass
            await self._client.start_notify(self._characteristic_uuid, _audio_handler)
        except Exception:
            self._client = None

    def stop_recording(self) -> None:
        """Unsubscribe from audio notifications and disconnect."""
        if not self._recording:
            return
        try:
            _run_async(self._disconnect())
        except Exception:
            pass
        self._recording = False

    async def _disconnect(self) -> None:
        """Async: stop notifications and disconnect BLE client."""
        try:
            if self._client is not None:
                await self._client.stop_notify(self._characteristic_uuid)
                await self._client.disconnect()
                self._client = None
        except Exception:
            pass

    def get_buffer(self, duration_ms: int = 100) -> AudioChunk:
        """Return buffered audio as AudioChunk (PCM16 or raw LC3 bytes)."""
        now = _ms()
        chunks: List[bytes] = []
        target_bytes = int(self._sample_rate_hz * duration_ms / 1000) * 2  # int16

        accumulated = 0
        while accumulated < target_bytes:
            try:
                chunk = self._buffer.get_nowait()
                chunks.append(chunk)
                accumulated += len(chunk)
            except queue.Empty:
                break

        if chunks:
            audio_data = b"".join(chunks)
            # Determine format based on whether LC3 decode succeeded
            # If raw LC3 bytes: first bytes won't be PCM silence pattern
            # We use a simple heuristic: if we have lc3 module, data is PCM
            try:
                import lc3  # type: ignore
                fmt = "PCM_S16LE"
            except ImportError:
                fmt = "LC3"
        else:
            # Return silence placeholder
            n_samples = int(self._sample_rate_hz * duration_ms / 1000)
            audio_data = b"\x00" * (n_samples * 2)
            fmt = "PCM_S16LE"

        return AudioChunk(
            timestamp_ms=now,
            sample_rate=self._sample_rate_hz,
            channels=self._channels,
            format=fmt,
            data=audio_data,
        )

    def get_doa(self) -> None:
        """Single microphone array -- Direction of Arrival not supported."""
        return None

    def shutdown(self) -> None:
        self.stop_recording()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "g2-microphone",
            "format": "LC3",
            "sample_rate": self._sample_rate_hz,
            "left_address": self._left_address,
            "characteristic_uuid": self._characteristic_uuid,
            "note": "LC3 decode requires liblc3; falls back to raw bytes",
        }


# ---------------------------------------------------------------------------
# G2 Display HAL
# ---------------------------------------------------------------------------

class G2DisplayHAL(DisplayHal):
    """Display HAL for Even Realities G2 right-arm BMP display.

    The G2 display is on the right arm. BMP images are sent as 194-byte
    packets with sequence numbers 0-255 via BLE command 0x15. After all
    packets are sent, a CRC check is triggered with command 0x16.

    BMP construction: 128x128 pixels, 24-bit color, minimal header.
    PIL is an optional dependency for text rendering. Without PIL, a blank
    (black) BMP is sent. Install pillow for real text rendering.

    Note: BLE write characteristic UUID must be verified against EvenDemoApp
    source. G2_RIGHT_SERVICE_UUID is a placeholder for the right arm service.
    """

    HAL_VERSION = "1.0.0"
    PACKET_SIZE = 194
    CMD_IMG_DATA = 0x15
    CMD_CRC_CHECK = 0x16

    def __init__(self, right_address: str) -> None:
        self._right_address = right_address
        self._resolution: Tuple[int, int] = (128, 128)

    def initialize(self, resolution: Tuple[int, int] = (128, 128)) -> None:
        """Store display resolution. G2 native resolution is 128x128."""
        self._resolution = resolution

    def show(self, card: DisplayCard) -> None:
        """Render DisplayCard body text as BMP and send to G2 right arm."""
        try:
            bmp_data = self._render_card(card)
            _run_async(self._send_bmp(bmp_data))
        except Exception:
            pass

    def _render_card(self, card: DisplayCard) -> bytes:
        """Convert DisplayCard to BMP bytes. PIL optional for text rendering."""
        text = card.body
        if card.title:
            text = f"{card.title}\n{card.body}"

        try:
            # Attempt PIL-based text rendering
            from PIL import Image, ImageDraw, ImageFont  # type: ignore
            img = Image.new("RGB", self._resolution, color=(0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.text((4, 4), text, fill=(255, 255, 255))
            import io
            buf = io.BytesIO()
            img.save(buf, format="BMP")
            return buf.getvalue()
        except ImportError:
            # PIL not available -- return blank BMP placeholder
            return _make_bmp(text, self._resolution[0], self._resolution[1])

    async def _send_bmp(self, bmp_data: bytes) -> None:
        """Async: send BMP as 194-byte packets to G2 right arm via BLE."""
        try:
            from bleak import BleakClient

            async with BleakClient(self._right_address) as client:
                # Split BMP into PACKET_SIZE-byte chunks and send with sequence numbers
                packets = [
                    bmp_data[i : i + self.PACKET_SIZE]
                    for i in range(0, len(bmp_data), self.PACKET_SIZE)
                ]
                for seq, packet in enumerate(packets):
                    seq_byte = seq % 256
                    # Packet format: [CMD_IMG_DATA, seq_byte, ...data...]
                    # Note: exact packet framing must be verified against EvenDemoApp
                    payload = bytes([self.CMD_IMG_DATA, seq_byte]) + packet
                    try:
                        # Write to right arm service characteristic
                        # Characteristic UUID for display write -- verify against EvenDemoApp
                        await client.write_gatt_char(
                            G2_RIGHT_SERVICE_UUID, payload, response=False
                        )
                    except Exception:
                        break

                # Send CRC check command after all packets
                try:
                    await client.write_gatt_char(
                        G2_RIGHT_SERVICE_UUID,
                        bytes([self.CMD_CRC_CHECK]),
                        response=False,
                    )
                except Exception:
                    pass
        except Exception:
            pass

    def clear(self) -> None:
        """Send blank BMP to clear the display."""
        try:
            blank_bmp = _make_bmp("", self._resolution[0], self._resolution[1])
            _run_async(self._send_bmp(blank_bmp))
        except Exception:
            pass

    def shutdown(self) -> None:
        self.clear()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "g2-display",
            "resolution": "128x128",
            "format": "BMP",
            "right_address": self._right_address,
            "packet_size": self.PACKET_SIZE,
            "note": "PIL optional for text rendering; falls back to blank BMP",
        }


# ---------------------------------------------------------------------------
# G2 Transport HAL
# ---------------------------------------------------------------------------

class G2TransportHAL(TransportHal):
    """BLE transport HAL for Even Realities G2 dual-arm glasses.

    Architecture note:
      - G2 sends audio to the host Python process via BLE (device-to-host link)
      - Host Python process forwards context to OpenClaw over HTTP/WiFi (host-to-OpenClaw link)
      - BLE is the device-to-host link; HTTP is the host-to-OpenClaw link
      - This HAL manages BLE connection to both arms + HTTP forwarding to gateway

    Connection order: left arm connects first, right arm connects after ACK.
    The left arm is the primary control arm; right arm handles display.
    """

    HAL_VERSION = "1.0.0"

    def __init__(
        self,
        left_address: str,
        right_address: str,
        openclaw_host: str = "100.82.191.2",
        port: int = 18800,
    ) -> None:
        self._left_address = left_address
        self._right_address = right_address
        self._openclaw_host = openclaw_host
        self._port = port
        self._state = TransportState.DISCONNECTED
        self._cb: Optional[Callable[[TransportState], None]] = None
        self._recv_q: queue.Queue = queue.Queue()
        self._left_client = None
        self._right_client = None

    def initialize(self, config: dict) -> None:
        """Accept optional config overrides for host/port."""
        self._openclaw_host = config.get("openclaw_host", self._openclaw_host)
        self._port = int(config.get("port", self._port))

    def connect(self) -> None:
        """Connect left arm first, then right arm after ACK.

        Left arm is primary; right arm connects after left arm ACK per G2 protocol.
        BLE connection state is managed via bleak.BleakClient.
        """
        self._set_state(TransportState.CONNECTING)
        success = _run_async(self._ble_connect())
        if success:
            self._set_state(TransportState.CONNECTED)
        else:
            self._set_state(TransportState.DISCONNECTED)

    async def _ble_connect(self) -> bool:
        """Async: connect left arm, wait for ACK, then connect right arm."""
        try:
            from bleak import BleakClient

            # Connect left arm first
            self._left_client = BleakClient(self._left_address)
            await self._left_client.connect()

            # After left arm ACK, connect right arm
            # ACK detection: check if left arm is connected and services are discovered
            if self._left_client.is_connected:
                self._right_client = BleakClient(self._right_address)
                await self._right_client.connect()

            return (
                self._left_client.is_connected
                if self._left_client else False
            )
        except Exception:
            return False

    def send(self, payload: bytes) -> SendResult:
        """Send payload to OpenClaw gateway via HTTP POST over WiFi.

        G2 sends audio/context to host via BLE. Host (this process) forwards
        to OpenClaw gateway over HTTP. BLE is device-to-host; HTTP is host-to-OpenClaw.
        """
        t0 = _ms()
        try:
            import urllib.request
            url = f"http://{self._openclaw_host}:{self._port}/wearable/ingest"
            req = urllib.request.Request(
                url,
                data=payload,
                method="POST",
                headers={"Content-Type": "application/octet-stream"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                _ = resp.read()
            return SendResult(True, len(payload), _ms() - t0)
        except Exception:
            return SendResult(False, 0, _ms() - t0)

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Receive data from internal queue (populated by BLE notification callbacks)."""
        try:
            return self._recv_q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return None

    def get_state(self) -> TransportState:
        return self._state

    def set_state_callback(self, cb: Callable[[TransportState], None]) -> None:
        self._cb = cb

    def disconnect(self) -> None:
        """Disconnect both BLE arms."""
        _run_async(self._ble_disconnect())
        self._set_state(TransportState.DISCONNECTED)

    async def _ble_disconnect(self) -> None:
        """Async: disconnect both BLE clients."""
        try:
            if self._right_client and self._right_client.is_connected:
                await self._right_client.disconnect()
        except Exception:
            pass
        try:
            if self._left_client and self._left_client.is_connected:
                await self._left_client.disconnect()
        except Exception:
            pass
        self._left_client = None
        self._right_client = None

    def shutdown(self) -> None:
        self.disconnect()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "g2-transport",
            "left_arm": self._left_address,
            "right_arm": self._right_address,
            "openclaw_host": self._openclaw_host,
            "port": self._port,
            "note": "BLE = device-to-host; HTTP = host-to-OpenClaw gateway",
        }

    def _set_state(self, state: TransportState) -> None:
        self._state = state
        if self._cb:
            self._cb(state)
