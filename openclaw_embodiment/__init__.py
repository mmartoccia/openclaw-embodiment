"""OpenClaw Embodiment SDK package exports."""

from .core.pipeline import HALRegistry, EmbodimentSDK, WearableSDK
from .context.models import AgentResponse, ContextPayload
from .hal.base import AudioChunk
from .transport.stt_bridge import OpenClawSTTBridge, STTProvider
from .profiles.ios_companion import (
    iOSCompanionProfile,
    iOSCompanionReceiver,
    iOSSensorPayload,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "HALRegistry",
    "EmbodimentSDK",
    "WearableSDK",
    "ContextPayload",
    "AgentResponse",
    # iOS Companion Profile
    "iOSCompanionProfile",
    "iOSCompanionReceiver",
    "iOSSensorPayload",
    # STT bridge
    "AudioChunk",
    "OpenClawSTTBridge",
    "STTProvider",
]
