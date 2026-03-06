"""OpenClaw Embodiment -- Trigger detectors."""

from .audio_trigger import AudioTriggerDetector, AudioTriggerState, AudioTriggerConfig
from .arbiter import TriggerArbiter, ArbiterPolicy, ArbiterConfig

__all__ = [
    "AudioTriggerDetector", "AudioTriggerState", "AudioTriggerConfig",
    "TriggerArbiter", "ArbiterPolicy", "ArbiterConfig",
]
