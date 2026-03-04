"""Device profile for local MLX inference on Apple Silicon.

Use this profile when running fully offline on an M-series Mac with mlx_lm installed.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Type


@dataclass
class DeviceProfile:
    """Descriptor for a specific device + transport configuration.

    Attributes:
        name: Human-readable profile name (used as identifier).
        description: Short description of the device and transport.
        transport: Transport class (not instance) to instantiate for this profile.
        model_id: Model ID string passed to the transport constructor.
        requires: List of pip dependency specifiers needed for this profile.
        validated_on: List of hardware configurations this profile was validated on.
    """

    name: str
    description: str
    transport: Optional[Type] = None
    model_id: str = ""
    requires: List[str] = field(default_factory=list)
    validated_on: List[str] = field(default_factory=list)


def _get_transport_class() -> Optional[Type]:
    """Lazy import LocalMLXTransport to avoid circular imports."""
    try:
        from ..transport.mlx import LocalMLXTransport
        return LocalMLXTransport
    except ImportError:
        return None


LOCAL_INFERENCE_PROFILE = DeviceProfile(
    name="local-inference",
    description="Apple Silicon Mac -- on-device MLX inference, no gateway",
    transport=None,  # Populated lazily via get_local_inference_profile()
    model_id="mlx-community/Qwen3-0.6B-4bit",
    requires=["mlx_lm>=0.30.7"],
    validated_on=["MacBook Pro M1/M2/M3/M4"],
)


def get_local_inference_profile() -> DeviceProfile:
    """Return a fully-populated LocalMLXTransport profile.

    Lazily resolves the transport class to avoid circular import at module load.

    Returns:
        DeviceProfile with transport class set to LocalMLXTransport.
    """
    from ..transport.mlx import LocalMLXTransport

    return DeviceProfile(
        name="local-inference",
        description="Apple Silicon Mac -- on-device MLX inference, no gateway",
        transport=LocalMLXTransport,
        model_id="mlx-community/Qwen3-0.6B-4bit",
        requires=["mlx_lm>=0.30.7"],
        validated_on=["MacBook Pro M1/M2/M3/M4"],
    )


__all__ = ["DeviceProfile", "LOCAL_INFERENCE_PROFILE", "get_local_inference_profile"]
