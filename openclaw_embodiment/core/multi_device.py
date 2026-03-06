"""Multi-device orchestration for OpenClaw Embodiment SDK v2.0.

Allows a single OpenClaw agent to interact with multiple simultaneous device
types (glasses + robot + phone) and routes agent responses to the most
appropriate device based on response type and device capabilities.

Classes:
    DeviceStatus: Enum of device lifecycle states.
    DeviceHandle: Handle representing a registered device with its HAL stack.
    DeviceRegistry: Registry for managing multiple DeviceHandles.
    MultiDeviceOrchestrator: Routes agent responses to appropriate devices.

Convenience:
    register_device(): Top-level function to register a device by profile name.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from ..hal.base import (
    ActuatorHal,
    AudioOutputHal,
    CameraHal,
    DisplayHal,
    MicrophoneHal,
    TransportHal,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DeviceStatus
# ---------------------------------------------------------------------------


class DeviceStatus(Enum):
    """Lifecycle state of a registered device.

    PENDING: Registered but not yet started.
    ACTIVE: HALs initialized and running.
    DEGRADED: HALs partially operational (health warnings present).
    INACTIVE: Stopped or disconnected.
    ERROR: Unrecoverable error -- requires re-registration.
    """

    PENDING = "pending"
    ACTIVE = "active"
    DEGRADED = "degraded"
    INACTIVE = "inactive"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Response types
# ---------------------------------------------------------------------------


class ResponseType(Enum):
    """Categorizes agent responses for routing decisions.

    TEXT: Plain text to display and/or speak.
    IMAGE: Image or visual content -- display-capable devices only.
    AUDIO: Audio content -- audio-output-capable devices only.
    ACTUATE: Physical command -- actuator-capable devices only.
    HEARTBEAT: Status ping -- any device.
    """

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    ACTUATE = "actuate"
    HEARTBEAT = "heartbeat"


# ---------------------------------------------------------------------------
# DeviceHandle
# ---------------------------------------------------------------------------


@dataclass
class DeviceHandle:
    """Handle representing a registered device with its HAL stack.

    Attributes:
        device_id: Unique identifier for this device instance.
        profile: Profile name (e.g. 'even-g2', 'reachy2').
        hal_stack: Dict of HAL name -> HAL instance (e.g. {'display': DisplayHal}).
        transport: Active transport HAL for this device.
        status: Current lifecycle status.
        config: Profile configuration dict used to create this handle.
        capabilities: Derived set of capability strings based on available HALs.
    """

    device_id: str
    profile: str
    hal_stack: Dict[str, Any]
    transport: Optional[TransportHal]
    status: DeviceStatus
    config: dict = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Derive capabilities from the hal_stack on creation."""
        self.capabilities = _derive_capabilities(self.hal_stack)

    def has_capability(self, capability: str) -> bool:
        """Return True if this device has the specified capability.

        Args:
            capability: One of 'display', 'audio_output', 'actuator',
                        'camera', 'microphone'.

        Returns:
            bool
        """
        return capability in self.capabilities

    def is_active(self) -> bool:
        """Return True if the device is in ACTIVE or DEGRADED status."""
        return self.status in (DeviceStatus.ACTIVE, DeviceStatus.DEGRADED)


def _derive_capabilities(hal_stack: Dict[str, Any]) -> List[str]:
    """Derive capability strings from a hal_stack dict.

    Args:
        hal_stack: Dict of HAL name -> HAL instance.

    Returns:
        List of capability strings.
    """
    caps: List[str] = []
    hal_cap_map = {
        "display": DisplayHal,
        "audio_output": AudioOutputHal,
        "actuator": ActuatorHal,
        "camera": CameraHal,
        "microphone": MicrophoneHal,
    }
    for cap_name, hal_class in hal_cap_map.items():
        for hal in hal_stack.values():
            if isinstance(hal, hal_class):
                caps.append(cap_name)
                break
    return caps


# ---------------------------------------------------------------------------
# DeviceRegistry
# ---------------------------------------------------------------------------


class DeviceRegistry:
    """Registry for managing multiple DeviceHandles.

    Thread-safe: all mutations are protected by an internal lock.
    """

    def __init__(self) -> None:
        self._handles: Dict[str, DeviceHandle] = {}

    def register(self, handle: DeviceHandle) -> None:
        """Register a DeviceHandle.

        Args:
            handle: DeviceHandle to register.

        Raises:
            ValueError: If a handle with the same device_id already exists.
        """
        if handle.device_id in self._handles:
            raise ValueError(f"Device '{handle.device_id}' is already registered. Unregister first.")
        self._handles[handle.device_id] = handle
        logger.info("Registered device '%s' (profile=%s)", handle.device_id, handle.profile)

    def unregister(self, device_id: str) -> None:
        """Unregister a device by ID.

        Args:
            device_id: ID of the device to remove.

        Raises:
            KeyError: If no device with that ID is registered.
        """
        if device_id not in self._handles:
            raise KeyError(f"No device registered with id '{device_id}'")
        del self._handles[device_id]
        logger.info("Unregistered device '%s'", device_id)

    def get(self, device_id: str) -> DeviceHandle:
        """Return the DeviceHandle for the given ID.

        Args:
            device_id: Device identifier.

        Returns:
            DeviceHandle

        Raises:
            KeyError: If no device with that ID is registered.
        """
        if device_id not in self._handles:
            raise KeyError(f"No device registered with id '{device_id}'")
        return self._handles[device_id]

    def list_active(self) -> List[DeviceHandle]:
        """Return all handles with ACTIVE or DEGRADED status.

        Returns:
            List of active DeviceHandles.
        """
        return [h for h in self._handles.values() if h.is_active()]

    def get_by_capability(self, capability: str) -> List[DeviceHandle]:
        """Return all active devices with the specified capability.

        Args:
            capability: One of 'display', 'audio_output', 'actuator',
                        'camera', 'microphone'.

        Returns:
            List of matching DeviceHandles.
        """
        return [h for h in self.list_active() if h.has_capability(capability)]

    def all(self) -> List[DeviceHandle]:
        """Return all registered handles regardless of status."""
        return list(self._handles.values())


# ---------------------------------------------------------------------------
# MultiDeviceOrchestrator
# ---------------------------------------------------------------------------


class MultiDeviceOrchestrator:
    """Routes agent responses to the most appropriate registered devices.

    Routing rules (BROADCAST policy by default):
    - TEXT     -> display + audio_output capable devices (all that qualify)
    - IMAGE    -> display-capable devices only
    - ACTUATE  -> actuator-capable devices only
    - AUDIO    -> audio-output-capable devices only
    - HEARTBEAT -> all active devices

    Usage::

        registry = DeviceRegistry()
        registry.register(glasses_handle)
        registry.register(robot_handle)

        orch = MultiDeviceOrchestrator(registry=registry)
        targets = orch.route(response_type=ResponseType.TEXT)
        for device in targets:
            # send to device.transport...
    """

    # Default routing table: response_type -> required capabilities
    _ROUTING_TABLE: Dict[ResponseType, List[str]] = {
        ResponseType.TEXT: ["display", "audio_output"],
        ResponseType.IMAGE: ["display"],
        ResponseType.AUDIO: ["audio_output"],
        ResponseType.ACTUATE: ["actuator"],
        ResponseType.HEARTBEAT: [],  # any device
    }

    def __init__(self, registry: DeviceRegistry) -> None:
        self._registry = registry

    def route(
        self,
        response_type: ResponseType,
        payload: Optional[Any] = None,
    ) -> List[DeviceHandle]:
        """Return the list of devices that should receive a response of the given type.

        Uses a broadcast policy: all active devices with a matching capability
        are returned. For HEARTBEAT, all active devices are returned.

        Args:
            response_type: The type of agent response to route.
            payload: Optional response payload (not used for routing, passed
                     through for caller convenience).

        Returns:
            Ordered list of DeviceHandles that should receive the response.
            Returns [] if no matching devices are active.
        """
        required_capabilities = self._ROUTING_TABLE.get(response_type, [])

        if not required_capabilities:
            # HEARTBEAT: send to all active devices
            targets = self._registry.list_active()
        else:
            # Union of all devices that have at least one required capability
            seen: set = set()
            targets = []
            for cap in required_capabilities:
                for handle in self._registry.get_by_capability(cap):
                    if handle.device_id not in seen:
                        seen.add(handle.device_id)
                        targets.append(handle)

        logger.debug(
            "Routing %s to %d device(s): %s",
            response_type.value,
            len(targets),
            [h.device_id for h in targets],
        )
        return targets

    def route_text(self) -> List[DeviceHandle]:
        """Return devices capable of displaying or speaking text."""
        return self.route(ResponseType.TEXT)

    def route_image(self) -> List[DeviceHandle]:
        """Return display-capable devices."""
        return self.route(ResponseType.IMAGE)

    def route_audio(self) -> List[DeviceHandle]:
        """Return audio-output-capable devices."""
        return self.route(ResponseType.AUDIO)

    def route_actuate(self) -> List[DeviceHandle]:
        """Return actuator-capable devices (robots, not glasses)."""
        return self.route(ResponseType.ACTUATE)


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def register_device(
    profile: str,
    config: dict,
    device_id: Optional[str] = None,
    registry: Optional[DeviceRegistry] = None,
    hal_stack: Optional[Dict[str, Any]] = None,
    transport: Optional[TransportHal] = None,
) -> DeviceHandle:
    """Create and register a DeviceHandle by profile name.

    This is a convenience wrapper. For production use, build the full HAL stack
    via the profile factory and pass it in via ``hal_stack``.

    Args:
        profile: Profile name (e.g. 'even-g2', 'reachy2').
        config: Profile configuration dict.
        device_id: Optional unique device ID. Auto-generated if None.
        registry: DeviceRegistry to register into. Creates a new one if None.
        hal_stack: Optional pre-built HAL stack dict. Empty if None.
        transport: Optional transport HAL. None if not provided.

    Returns:
        DeviceHandle that has been registered in the registry.
    """
    if device_id is None:
        device_id = f"{profile}-{uuid.uuid4().hex[:8]}"
    if registry is None:
        registry = DeviceRegistry()
    if hal_stack is None:
        hal_stack = {}

    handle = DeviceHandle(
        device_id=device_id,
        profile=profile,
        hal_stack=hal_stack,
        transport=transport,
        status=DeviceStatus.PENDING,
        config=config,
    )
    registry.register(handle)
    return handle


__all__ = [
    "DeviceHandle",
    "DeviceRegistry",
    "DeviceStatus",
    "MultiDeviceOrchestrator",
    "ResponseType",
    "register_device",
]
