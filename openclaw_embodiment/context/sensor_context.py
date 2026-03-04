"""Context Engine v0.3 - SensorContext and related dataclasses.

All dataclasses representing sensor readings and device capabilities.
Uses stdlib dataclasses only -- no Pydantic, no external deps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class DeviceCapabilityVector:
    """Describes what sensors/outputs a device has available.
    
    Used to distinguish "sensor unavailable" from "sensor returned nothing."
    """
    has_microphone: bool = False
    has_camera: bool = False
    has_imu: bool = False
    has_ble: bool = False
    has_display: bool = False
    has_speaker: bool = False
    has_gps: bool = False
    is_wearable: bool = False
    has_gaze: bool = False


# Predefined device profiles
DISTILLER_CM5 = DeviceCapabilityVector(
    has_microphone=True,
    has_camera=True,
    has_imu=False,
    has_ble=True,
    has_display=True,
    has_speaker=True,
    has_gps=False,
    is_wearable=False,
    has_gaze=False,
)

EVEN_G2 = DeviceCapabilityVector(
    has_microphone=False,
    has_camera=False,
    has_imu=True,
    has_ble=True,
    has_display=True,
    has_speaker=True,
    has_gps=False,
    is_wearable=True,
    has_gaze=False,
)

LIMITLESS_PENDANT = DeviceCapabilityVector(
    has_microphone=True,
    has_camera=False,
    has_imu=True,
    has_ble=True,
    has_display=False,
    has_speaker=False,
    has_gps=False,
    is_wearable=True,
    has_gaze=False,
)

IPHONE = DeviceCapabilityVector(
    has_microphone=True,
    has_camera=True,
    has_imu=True,
    has_ble=True,
    has_display=True,
    has_speaker=True,
    has_gps=True,
    is_wearable=True,  # can be carried
    has_gaze=False,
)

REACHY2 = DeviceCapabilityVector(
    has_microphone=True,
    has_camera=True,
    has_imu=True,
    has_ble=True,
    has_display=False,
    has_speaker=True,
    has_gps=False,
    is_wearable=False,
    has_gaze=False,
)

RASPBERRY_PI = DeviceCapabilityVector(
    has_microphone=True,
    has_camera=True,
    has_imu=False,
    has_ble=True,
    has_display=False,
    has_speaker=True,
    has_gps=False,
    is_wearable=False,
    has_gaze=False,
)


@dataclass
class AudioContext:
    """Context derived from audio sensor readings."""
    transcript: Optional[str]
    speaker_count: Optional[int]
    ambient_class: str  # "speech" | "music" | "silence" | "noise"
    rms_level: float
    language: Optional[str]
    confidence: float


@dataclass
class VisualContext:
    """Context derived from camera/visual sensor readings."""
    description: Optional[str]
    person_count: Optional[int]
    activity: Optional[str]  # "sitting" | "walking" | "talking" | "idle"
    lighting: str  # "bright" | "dim" | "dark"
    frame_path: Optional[str]
    confidence: float


@dataclass
class MotionContext:
    """Context derived from IMU/accelerometer readings."""
    state: str  # "stationary" | "walking" | "running" | "gesture"
    orientation: Optional[Tuple[float, float, float]]  # yaw, pitch, roll
    acceleration: Optional[Tuple[float, float, float]]
    confidence: float


@dataclass
class ProximityContext:
    """Context derived from BLE proximity scanning."""
    known_devices: List[str] = field(default_factory=list)
    unknown_count: int = 0
    rssi_map: Dict[str, int] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class SensorContext:
    """The canonical context object assembled from all available sensors.
    
    Always the same schema. Fields are Optional -- None means "sensor
    unavailable", not "sensor returned nothing."
    """
    # Identity
    timestamp_ms: int
    device_id: str
    trigger: str  # "voice_detected" | "motion" | "ble_new_device" |
                  # "scheduled" | "manual" | "threshold_crossed"
    
    # Sensor channels (all Optional -- graceful degradation)
    audio: Optional[AudioContext]
    visual: Optional[VisualContext]
    motion: Optional[MotionContext]
    proximity: Optional[ProximityContext]
    
    # Fused outputs
    awareness_level: float  # 0.0-1.0: quality of correlation across sensors
    conflicts: List[str] = field(default_factory=list)  # e.g. ["audio:3_speakers visual:1_person"]
    summary: str = ""  # LLM-ready natural language
    
    # Capability fingerprint
    device_capabilities: DeviceCapabilityVector = field(default_factory=DeviceCapabilityVector)
