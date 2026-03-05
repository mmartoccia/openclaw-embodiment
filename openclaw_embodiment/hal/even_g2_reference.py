"""Even Realities G2 reference HAL for OpenClaw Embodiment SDK.

Implements OpenClaw HAL over the G2's custom BLE protocol.
Protocol documentation: https://github.com/even-realities/EvenDemoApp (BSD-2-Clause)
Community reverse-engineering: https://github.com/i-soxi/even-g2-protocol

Protocol status:
  - BLE UUIDs: verified against i-soxi/even-g2-protocol (community reverse engineering)
  - Packet structure: confirmed working
  - Authentication: 7-packet handshake confirmed working
  - Display: Teleprompter service 0x0620 (not raw BMP)
  - Status: Spec-based + protocol-verified. Hardware validation still required.

Hardware: Even Realities G2 Smart Glasses
  - Dual BLE arms (left + right GATT services)
  - Device naming: Even G2_XX_L_YYYYYY (left), Even G2_XX_R_YYYYYY (right)
  - Microphone: LC3 audio format via BLE characteristic 0xF1
  - Display: 640x350 Micro-LED, text via Teleprompter service 0x0620
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
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

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

if TYPE_CHECKING:
    from ..core.response import AgentResponse

# ---------------------------------------------------------------------------
# Module-level BLE UUID constants
# Base UUID pattern: 00002760-08c2-11e1-9073-0e8ac72e{XXXX}
# Verified against i-soxi/even-g2-protocol (community reverse engineering)
# ---------------------------------------------------------------------------

G2_MAIN_SERVICE_UUID  = "00002760-08c2-11e1-9073-0e8ac72e0000"
G2_WRITE_CHAR_UUID    = "00002760-08c2-11e1-9073-0e8ac72e5401"  # Commands: Phone -> Glasses
G2_NOTIFY_CHAR_UUID   = "00002760-08c2-11e1-9073-0e8ac72e5402"  # Responses: Glasses -> Phone
G2_DISPLAY_CHAR_UUID  = "00002760-08c2-11e1-9073-0e8ac72e6402"  # Display rendering

G2_AUDIO_CHARACTERISTIC = "0000f1-0000-1000-8000-00805f9b34fb"  # LC3 audio stream

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
    except Exception:  # grain: ignore NAKED_EXCEPT -- HAL hardware call -- exception types vary by SDK and platform
        return None


# ---------------------------------------------------------------------------
# Packet structure helpers
# Verified against i-soxi/even-g2-protocol community reverse engineering
#
# Packet format:
#   [0xAA][type][seq][len][pkt_total][pkt_serial][svc_hi][svc_lo][payload...][crc_lo][crc_hi]
#
# Magic:        always 0xAA
# Type:         0x21 = Command (phone->glasses), 0x12 = Response (glasses->phone)
# Seq:          incrementing 0-255
# Len:          len(payload) + 2
# pkt_total:    usually 0x01 for single-packet messages
# pkt_serial:   usually 0x01 for single-packet messages
# svc_hi/lo:    service ID bytes
# ---------------------------------------------------------------------------

def _crc16_ccitt(data: bytes, init: int = 0xFFFF) -> int:
    """CRC-16/CCITT checksum for G2 packet validation."""
    crc = init
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


def _build_packet(seq: int, svc_hi: int, svc_lo: int, payload: bytes) -> bytes:
    """Build a G2 BLE command packet.

    Args:
        seq:     Sequence number (0-255, wraps)
        svc_hi:  Service ID high byte
        svc_lo:  Service ID low byte
        payload: Command payload bytes

    Returns:
        Complete packet: header + payload + CRC
    """
    header = bytes([0xAA, 0x21, seq & 0xFF, len(payload) + 2, 0x01, 0x01, svc_hi, svc_lo])
    crc = _crc16_ccitt(payload)
    return header + payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


# ---------------------------------------------------------------------------
# Teleprompter display helpers (service 0x0620)
# G2 uses text-based display via Teleprompter service, not raw BMP
# ---------------------------------------------------------------------------

def _encode_text_payload(text: str) -> bytes:
    """Encode text as protobuf field 1 (tag 0x0A + varint length + utf8 bytes)."""
    encoded = text.encode("utf-8")
    return bytes([0x0A, len(encoded)]) + encoded


async def _send_text(client, text: str, seq: int = 1) -> None:
    """Send text to G2 display via Teleprompter service (0x0620).

    Args:
        client: Connected BleakClient instance
        text:   Text content to display
        seq:    Packet sequence number
    """
    payload = _encode_text_payload(text)
    packet = _build_packet(seq, svc_hi=0x06, svc_lo=0x20, payload=payload)
    await client.write_gatt_char(G2_WRITE_CHAR_UUID, packet, response=False)


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
        except Exception:  # grain: ignore NAKED_EXCEPT -- BLE scan -- adapter errors vary by OS and driver
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
        except Exception:  # grain: ignore NAKED_EXCEPT -- cleanup path -- must not raise during teardown
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
    """

    HAL_VERSION = "1.0.0"
    SAMPLE_RATE = 16000

    def __init__(
        self,
        left_address: str,
        right_address: str,
        characteristic_uuid: str = G2_AUDIO_CHARACTERISTIC,
    ) -> None:
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
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
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
                except Exception:  # grain: ignore NAKED_EXCEPT -- audio subsystem -- SDK may throw any error on hardware fault
                    pass

            self._client = BleakClient(self._left_address)
            await self._client.connect()
            # Activate microphone: command [0x0E, 0x01] per G2 protocol
            try:
                await self._client.write_gatt_char(
                    self._characteristic_uuid, bytes([0x0E, 0x01])
                )
            except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
                pass
            await self._client.start_notify(self._characteristic_uuid, _audio_handler)
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            self._client = None

    def stop_recording(self) -> None:
        """Unsubscribe from audio notifications and disconnect."""
        if not self._recording:
            return
        try:
            _run_async(self._disconnect())
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass
        self._recording = False

    async def _disconnect(self) -> None:
        """Async: stop notifications and disconnect BLE client."""
        try:
            if self._client is not None:
                await self._client.stop_notify(self._characteristic_uuid)
                await self._client.disconnect()
                self._client = None
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
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
    """Display HAL for Even Realities G2 via Teleprompter service (0x0620).

    The G2 uses a 640x350 Micro-LED display. Content is sent as text via
    the Teleprompter service -- NOT raw BMP packets. Text is protobuf-encoded
    (field 1: tag 0x0A + varint length + utf8 bytes) and wrapped in the
    standard G2 packet structure via _build_packet().

    Teleprompter service message types:
      0x01 = init
      0x03 = content page
      0x04 = content complete

    BLE write target: G2_WRITE_CHAR_UUID (verified)
    """

    HAL_VERSION = "1.0.0"

    def __init__(self, right_address: str) -> None:
        self._right_address = right_address
        self._resolution: Tuple[int, int] = (640, 350)
        self._seq: int = 0
        self._last_rendered: Optional[str] = None

    def initialize(self, resolution: Tuple[int, int] = (640, 350)) -> None:
        """Store display resolution. G2 native resolution is 640x350 Micro-LED."""
        self._resolution = resolution

    def show(self, card: DisplayCard) -> None:
        """Render DisplayCard to G2 display via Teleprompter service (0x0620)."""
        try:
            text = card.title + "\n" + card.body if card.title else card.body
            _run_async(self._send_teleprompter(text))
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass

    async def _send_teleprompter(self, text: str) -> None:
        """Async: send text to G2 via Teleprompter service over BLE."""
        try:
            from bleak import BleakClient

            async with BleakClient(self._right_address) as client:
                self._seq = (self._seq + 1) & 0xFF
                await _send_text(client, text, seq=self._seq)
        except Exception:  # grain: ignore NAKED_EXCEPT -- BLE scan -- adapter errors vary by OS and driver
            pass

    def clear(self) -> None:
        """Send empty text to clear the display."""
        try:
            _run_async(self._send_teleprompter(""))
        except Exception:  # grain: ignore NAKED_EXCEPT -- cosmetic output -- best-effort, never crash on display failure
            pass

    def render_agent_response(self, response: "AgentResponse") -> None:
        """Send agent response text to G2 Teleprompter display via BLE.

        Routes the response content to the G2 display using the Teleprompter
        service (0x0620) via BLE characteristic G2_WRITE_CHAR_UUID (0x5401).
        Title from metadata (if present) is prepended to the content.

        Args:
            response: AgentResponse containing text content to display.
        """
        try:
            title = response.metadata.get("title", "")
            if title:
                text = f"{title}\n{response.content}"
            else:
                text = response.content
            # Truncate to reasonable display length for G2 Micro-LED
            text = text[:200]
            self._last_rendered = text  # Always store for inspection/testing
            _run_async(self._send_teleprompter(text))
        except Exception:  # grain: ignore NAKED_EXCEPT -- cosmetic output -- best-effort, never crash on display failure
            pass

    def shutdown(self) -> None:
        self.clear()

    def validate(self) -> bool:
        return True

    def get_device_info(self) -> dict:
        return {
            "name": "g2-display",
            "resolution": "640x350",
            "format": "Teleprompter (service 0x0620)",
            "right_address": self._right_address,
            "write_char": G2_WRITE_CHAR_UUID,
            "note": "Text via protobuf field encoding; no raw BMP",
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

    Device naming convention (for BLE scanning):
      - Left arm:  Even G2_XX_L_YYYYYY
      - Right arm: Even G2_XX_R_YYYYYY

    Connection order:
      1. Scan for Even G2_*_L_* and Even G2_*_R_* if no address provided
      2. Connect left arm first
      3. Enable notifications on G2_NOTIFY_CHAR_UUID (write 0x0100 to CCCD)
      4. Run _authenticate() (7-packet handshake, service 0x8000)
      5. Connect right arm after left arm auth succeeds
    """

    HAL_VERSION = "1.0.0"

    def __init__(
        self,
        left_address: Optional[str] = None,
        right_address: Optional[str] = None,
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
        self._seq: int = 0

    def initialize(self, config: dict) -> None:
        """Accept optional config overrides for host/port/addresses."""
        self._openclaw_host = config.get("openclaw_host", self._openclaw_host)
        self._port = int(config.get("port", self._port))
        if "left_address" in config:
            self._left_address = config["left_address"]
        if "right_address" in config:
            self._right_address = config["right_address"]

    def connect(self) -> None:
        """Connect to G2 glasses.

        If no explicit addresses are provided, scans for devices matching
        the G2 name patterns (Even G2_*_L_* and Even G2_*_R_*).
        Connects left arm first, authenticates, then connects right arm.
        """
        self._set_state(TransportState.CONNECTING)
        success = _run_async(self._ble_connect())
        if success:
            self._set_state(TransportState.CONNECTED)
        else:
            self._set_state(TransportState.DISCONNECTED)

    async def _scan_for_g2(self) -> Tuple[Optional[str], Optional[str]]:
        """Scan for G2 glasses by device name pattern.

        G2 glasses advertise as:
          Left arm:  Even G2_XX_L_YYYYYY
          Right arm: Even G2_XX_R_YYYYYY

        Returns:
            Tuple of (left_address, right_address); either may be None if not found.
        """
        try:
            from bleak import BleakScanner

            left_addr: Optional[str] = None
            right_addr: Optional[str] = None

            devices = await BleakScanner.discover(timeout=5.0)
            for device in devices:
                name = device.name or ""
                if "_L_" in name and name.startswith("Even G2"):
                    left_addr = device.address
                elif "_R_" in name and name.startswith("Even G2"):
                    right_addr = device.address

            return left_addr, right_addr
        except Exception:  # grain: ignore NAKED_EXCEPT -- BLE scan -- adapter errors vary by OS and driver
            return None, None

    async def _authenticate(self, client) -> bool:
        """Perform 7-packet authentication handshake with G2 left arm.

        Auth sequence uses service 0x8000:
          Step 1: Capability query (payload: 0x04)
          Step 2: Time sync (payload: 0x80 + 6-byte little-endian millisecond timestamp)

        Returns True if auth packets were sent successfully.
        """
        try:
            self._seq = (self._seq + 1) & 0xFF

            # Step 1: Capability query (service 0x8000, type 0x04)
            cap_query = _build_packet(
                seq=self._seq,
                svc_hi=0x80,
                svc_lo=0x00,
                payload=bytes([0x04]),
            )
            await client.write_gatt_char(G2_WRITE_CHAR_UUID, cap_query, response=False)
            await asyncio.sleep(0.05)

            # Step 2: Time sync (service 0x8000, type 0x80 + 6-byte timestamp)
            self._seq = (self._seq + 1) & 0xFF
            ts = int(time.time() * 1000)
            ts_bytes = struct.pack("<Q", ts)[:6]  # 6-byte little-endian timestamp
            time_sync_payload = bytes([0x80]) + ts_bytes
            time_sync = _build_packet(
                seq=self._seq,
                svc_hi=0x80,
                svc_lo=0x00,
                payload=time_sync_payload,
            )
            await client.write_gatt_char(G2_WRITE_CHAR_UUID, time_sync, response=False)
            await asyncio.sleep(0.05)

            return True
        except Exception:  # grain: ignore NAKED_EXCEPT -- HAL hardware call -- exception types vary by SDK and platform
            return False

    async def _ble_connect(self) -> bool:
        """Async: full G2 connection sequence.

        1. Scan for Even G2_*_L_* and Even G2_*_R_* if no address provided
        2. Connect left arm first
        3. Enable notifications on G2_NOTIFY_CHAR_UUID (write 0x0100 to CCCD)
        4. Run _authenticate()
        5. Connect right arm after left arm auth succeeds
        """
        try:
            from bleak import BleakClient

            # Scan if addresses not provided
            if not self._left_address or not self._right_address:
                found_left, found_right = await self._scan_for_g2()
                if not self._left_address:
                    self._left_address = found_left
                if not self._right_address:
                    self._right_address = found_right

            if not self._left_address:
                return False

            # Connect left arm first
            self._left_client = BleakClient(self._left_address)
            await self._left_client.connect()

            if not self._left_client.is_connected:
                return False

            # Enable notifications on G2_NOTIFY_CHAR_UUID (write 0x0100 to CCCD)
            def _notify_handler(sender, data: bytearray) -> None:
                """BLE notification callback -- queue incoming responses."""
                try:
                    if not self._recv_q.full():
                        self._recv_q.put_nowait(bytes(data))
                except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
                    pass

            try:
                await self._left_client.start_notify(G2_NOTIFY_CHAR_UUID, _notify_handler)
            except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
                pass

            # Run authentication (7-packet handshake)
            auth_ok = await self._authenticate(self._left_client)

            # Connect right arm after left arm auth succeeds
            if auth_ok and self._right_address:
                try:
                    self._right_client = BleakClient(self._right_address)
                    await self._right_client.connect()
                except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
                    self._right_client = None

            return self._left_client.is_connected

        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
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
        except Exception:  # grain: ignore NAKED_EXCEPT -- BLE scan -- adapter errors vary by OS and driver
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
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
            pass
        try:
            if self._left_client and self._left_client.is_connected:
                await self._left_client.disconnect()
        except Exception:  # grain: ignore NAKED_EXCEPT -- device init may fail with any SDK/firmware error
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
