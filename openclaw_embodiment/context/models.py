"""Dataclasses used in context and transport layers."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class MemoryChunk:
    """Retrieved context memory chunk."""

    chunk_id: str
    source: str
    content: str
    relevance_score: float
    timestamp_epoch: int
    metadata: Dict[str, str] = field(default_factory=dict)


@dataclass
class AgentResponse:
    """Agent response routed back to wearable."""

    response_id: str
    event_id: str
    trigger_timestamp_ms: int
    mode: str = "card"
    title: str = ""
    body: str = ""
    audio_data: Optional[bytes] = None


@dataclass
class ContextPayload:
    """Wire-level semantic payload before serialization."""

    event_id: str
    device_id: str
    timestamp_epoch: int
    flags: int
    image_data: bytes = b""
    audio_data: bytes = b""
    imu_pitch: int = 0
    imu_yaw: int = 0
    imu_roll: int = 0
    imu_trigger_confidence: int = 0
    scene_gate_confidence: int = 0
    packet_nonce: int = 0
