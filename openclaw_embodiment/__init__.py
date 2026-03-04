
from .core.pipeline import HALRegistry, EmbodimentSDK, WearableSDK
from .context.models import ContextPayload
from .core.response import AgentResponseListener, ResponseType, AgentResponse, DeviceResponseRouter
from .transport.mlx import LocalMLXTransport, ModelSpec, DeviceContext
from .transport.stt_bridge import OpenClawSTTBridge, STTProvider
from .profiles.ios_companion import (
    iOSCompanionProfile,
    iOSCompanionReceiver,
    iOSSensorPayload,
)

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "HALRegistry",
    "EmbodimentSDK",
    "WearableSDK",
    "ContextPayload",
    # Bidirectional agent response
    "AgentResponseListener",
    "ResponseType",
    "AgentResponse",
    "DeviceResponseRouter",
    # Local inference
    "LocalMLXTransport",
    "ModelSpec",
    "DeviceContext",
    # STT bridge
    "OpenClawSTTBridge",
    "STTProvider",
    # iOS companion
    "iOSCompanionProfile",
    "iOSCompanionReceiver",
    "iOSSensorPayload",
]
