"""HAL exports."""

from .base import (
    ActuatorCommand,
    ActuatorHal,
    ActuatorResult,
    AudioChunk,
    AudioOutputHal,
    CameraFrame,
    CameraHal,
    ChargingState,
    ClassificationResult,
    ClassifierHal,
    DisplayCard,
    DisplayHal,
    IMUHal,
    IMUSample,
    JointState,
    MicrophoneHal,
    PowerHal,
    PowerSource,
    SendResult,
    TransportHal,
    TransportState,
)
from .reachy_reference import ReachyActuatorHAL, ReachyMotionTracker
from .oakd_reference import OakDCameraHAL, OakDFrameChangeIMU, OakDTransportHAL, OAKD_TRIGGER_PROFILE
from .frame_reference import (
    FrameCameraHAL,
    FrameIMUHAL,
    FrameMicrophoneHAL,
    FrameDisplayHAL,
    FrameTransportHAL,
    FRAME_TRIGGER_PROFILE,
)
from .pi_zero2w_reference import (
    PiZero2WCameraHAL,
    PiZero2WMicrophoneHAL,
    PiZero2WTransportHAL,
    PIZERO2W_TRIGGER_PROFILE,
)
from .even_g2_reference import (
    G2RSSIMotionProxy,
    G2MicrophoneHAL,
    G2DisplayHAL,
    G2TransportHAL,
    G2_TRIGGER_PROFILE,
)

__all__ = [
    "IMUHal",
    "IMUSample",
    "CameraHal",
    "CameraFrame",
    "MicrophoneHal",
    "AudioChunk",
    "ClassifierHal",
    "ClassificationResult",
    "TransportHal",
    "TransportState",
    "SendResult",
    "DisplayHal",
    "DisplayCard",
    "AudioOutputHal",
    "ActuatorCommand",
    "ActuatorHal",
    "ActuatorResult",
    "JointState",
    "ChargingState",
    "PowerSource",
    "PowerHal",
    "ReachyActuatorHAL",
    "ReachyMotionTracker",
    # OAK-D
    "OakDCameraHAL",
    "OakDFrameChangeIMU",
    "OakDTransportHAL",
    "OAKD_TRIGGER_PROFILE",
    # Frame AR Glasses
    "FrameCameraHAL",
    "FrameIMUHAL",
    "FrameMicrophoneHAL",
    "FrameDisplayHAL",
    "FrameTransportHAL",
    "FRAME_TRIGGER_PROFILE",
    # Pi Zero 2W
    "PiZero2WCameraHAL",
    "PiZero2WMicrophoneHAL",
    "PiZero2WTransportHAL",
    "PIZERO2W_TRIGGER_PROFILE",
    # Even Realities G2 Smart Glasses
    "G2RSSIMotionProxy",
    "G2MicrophoneHAL",
    "G2DisplayHAL",
    "G2TransportHAL",
    "G2_TRIGGER_PROFILE",
]
