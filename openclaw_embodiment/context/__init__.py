"""OpenClaw Embodiment Context Engine.

Provides sensor context assembly, device capability profiles, and
the ContextBuilder for creating SensorContext from HAL readings.
"""

from .client import ContextClient
from .context_builder import ContextBuilder
from .models import AgentResponse, ContextPayload, MemoryChunk
from .sensor_context import (
    DISTILLER_CM5,
    EVEN_G2,
    IPHONE,
    LIMITLESS_PENDANT,
    RASPBERRY_PI,
    REACHY2,
    AudioContext,
    DeviceCapabilityVector,
    MotionContext,
    ProximityContext,
    SensorContext,
    VisualContext,
)

__all__ = [
    # Client
    "ContextClient",
    # Builder
    "ContextBuilder",
    # Legacy models
    "AgentResponse",
    "ContextPayload",
    "MemoryChunk",
    # Context Engine v0.3
    "SensorContext",
    "AudioContext",
    "VisualContext",
    "MotionContext",
    "ProximityContext",
    "DeviceCapabilityVector",
    # Device profiles
    "DISTILLER_CM5",
    "EVEN_G2",
    "LIMITLESS_PENDANT",
    "IPHONE",
    "REACHY2",
    "RASPBERRY_PI",
]
