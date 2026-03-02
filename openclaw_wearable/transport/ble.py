"""BLE packet serializer and fragmentation protocol implementation."""

import json
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from ..context.models import AgentResponse, ContextPayload
from ..core.exceptions import PayloadCRCError, PayloadFragmentOrderError, PayloadFragmentTimeoutError, PayloadMagicError, PayloadSizeError, PayloadTruncatedError, PayloadVersionError

MAGIC = 0x0C1A
RESPONSE_MAGIC = 0x0C1B
VERSION = 0x01
HEADER_SIZE = 16
MAX_PACKET_SIZE = 25028


class PacketSerializer:
    """Serialize and deserialize Wearable Packet v1 and response packets."""

    @staticmethod
    def serialize(payload: ContextPayload) -> bytes:
        """Serialize ContextPayload to v1 wire packet with CRC32."""
        image_len = len(payload.image_data)
        audio_len = len(payload.audio_data)
        imu_block = struct.pack("<hhhH", payload.imu_pitch, payload.imu_yaw, payload.imu_roll, payload.imu_trigger_confidence)
        header = struct.pack("<HBBIHHHH", MAGIC, VERSION, payload.flags, payload.timestamp_epoch, image_len, audio_len, payload.scene_gate_confidence, 0)
        data = header + imu_block + payload.image_data + payload.audio_data
        crc = zlib.crc32(data) & 0xFFFFFFFF
        packet = data + struct.pack("<I", crc)
        if len(packet) > MAX_PACKET_SIZE:
            raise PayloadSizeError("payload too large", "PAYLOAD_SIZE_INVALID", "Reduce image/audio payload")
        return packet

    @staticmethod
    def deserialize(data: bytes) -> ContextPayload:
        """Parse and validate wire packet into ContextPayload."""
        if len(data) < HEADER_SIZE + 8 + 4:
            raise PayloadTruncatedError("packet too short", "PAYLOAD_TRUNCATED", "Ensure complete packet received")
        magic, version, flags, ts, image_len, audio_len, conf, _ = struct.unpack("<HBBIHHHH", data[:HEADER_SIZE])
        if magic != MAGIC:
            raise PayloadMagicError("bad magic", "PAYLOAD_MAGIC_INVALID", "Validate protocol sync")
        if version != VERSION:
            raise PayloadVersionError("unsupported version", "PAYLOAD_VERSION_UNSUPPORTED", "Upgrade SDK or sender")
        expected = HEADER_SIZE + 8 + image_len + audio_len + 4
        if expected > MAX_PACKET_SIZE:
            raise PayloadSizeError("declared packet too large", "PAYLOAD_SIZE_INVALID", "Reduce payload size")
        if len(data) < expected:
            raise PayloadTruncatedError("packet truncated", "PAYLOAD_TRUNCATED", "Retry transfer")
        body = data[: expected - 4]
        crc_expected = struct.unpack("<I", data[expected - 4:expected])[0]
        crc_actual = zlib.crc32(body) & 0xFFFFFFFF
        if crc_actual != crc_expected:
            raise PayloadCRCError("crc mismatch", "PAYLOAD_CRC_MISMATCH", "Retry send")
        imu_pitch, imu_yaw, imu_roll, imu_conf = struct.unpack("<hhhH", data[HEADER_SIZE:HEADER_SIZE + 8])
        image_start = HEADER_SIZE + 8
        image_data = data[image_start:image_start + image_len]
        audio_data = data[image_start + image_len:image_start + image_len + audio_len]
        return ContextPayload(event_id="", device_id="", timestamp_epoch=ts, flags=flags, image_data=image_data, audio_data=audio_data, imu_pitch=imu_pitch, imu_yaw=imu_yaw, imu_roll=imu_roll, imu_trigger_confidence=imu_conf, scene_gate_confidence=conf)

    @staticmethod
    def deserialize_response(data: bytes) -> AgentResponse:
        """Deserialize response packet magic 0x0C1B."""
        if len(data) < 18:
            raise PayloadTruncatedError("response too short", "PAYLOAD_TRUNCATED", "Retry receive")
        magic, version, flags, ts, nonce, dlen, alen = struct.unpack("<HBBIIHH", data[:16])
        if magic != RESPONSE_MAGIC:
            raise PayloadMagicError("bad response magic", "PAYLOAD_MAGIC_INVALID", "Check response protocol")
        if version != VERSION:
            raise PayloadVersionError("unsupported response version", "PAYLOAD_VERSION_UNSUPPORTED", "Upgrade SDK")
        payload = data[16:16 + dlen]
        body = json.loads(payload.decode("utf-8")) if payload else {}
        audio = data[16 + dlen:16 + dlen + alen] if alen else None
        return AgentResponse(response_id=str(nonce), event_id=body.get("event_id", ""), trigger_timestamp_ms=int(ts * 1000), mode=body.get("mode", "card"), title=body.get("title", ""), body=body.get("body", ""), audio_data=audio)


@dataclass
class _FragState:
    created_ms: int
    count: int
    frags: Dict[int, bytes]
    last_seen: bool = False


class Fragmenter:
    """Fragmentation protocol with out-of-order tolerant reassembly."""

    def __init__(self, mtu: int = 247) -> None:
        self.mtu = mtu
        self.payload_max = mtu - 10
        self.buffers = {}  # type: Dict[Tuple[int, int], _FragState]

    def fragment(self, packet: bytes, message_id: int = 1, packet_seq: int = 1) -> list:
        """Split packet into fragments with 10-byte fragmentation header."""
        if self.payload_max <= 0:
            raise PayloadSizeError("invalid mtu", "PAYLOAD_SIZE_INVALID", "Set mtu >= 20")
        chunks = [packet[i:i + self.payload_max] for i in range(0, len(packet), self.payload_max)]
        total = len(chunks)
        out = []
        for idx, chunk in enumerate(chunks):
            flags = (1 if idx == 0 else 0) | (2 if idx == total - 1 else 0)
            hdr = struct.pack("<BBIBBH", flags, packet_seq & 0xFF, message_id & 0xFFFFFFFF, idx & 0xFF, total & 0xFF, len(chunk))
            out.append(hdr + chunk)
        return out

    def defragment(self, fragment: bytes) -> Optional[bytes]:
        """Consume one fragment and return completed packet when ready."""
        if len(fragment) < 10:
            raise PayloadFragmentOrderError("short fragment", "PAYLOAD_FRAGMENT_OUT_OF_ORDER", "Drop fragment")
        flags, seq, message_id, idx, count, plen = struct.unpack("<BBIBBH", fragment[:10])
        data = fragment[10:]
        if plen != len(data):
            raise PayloadFragmentOrderError("bad fragment length", "PAYLOAD_FRAGMENT_OUT_OF_ORDER", "Drop fragment")
        key = (message_id, seq)
        now = int(time.monotonic() * 1000)
        state = self.buffers.get(key)
        if not state:
            state = _FragState(now, count, {})
            self.buffers[key] = state
        if now - state.created_ms > 2000:
            del self.buffers[key]
            raise PayloadFragmentTimeoutError("fragment timeout", "PAYLOAD_FRAGMENT_TIMEOUT", "Retry send")
        if idx < count and idx not in state.frags:
            state.frags[idx] = data
        if flags & 2:
            state.last_seen = True
        if state.last_seen and len(state.frags) == state.count:
            ordered = [state.frags[i] for i in range(state.count)]
            del self.buffers[key]
            return b"".join(ordered)
        return None

    def reset(self) -> None:
        """Clear all in-progress reassembly state."""
        self.buffers.clear()
