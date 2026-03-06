"""Tests for DeviceRegistry, DeviceHandle, and MultiDeviceOrchestrator."""

from __future__ import annotations

import pytest

from openclaw_embodiment.core.multi_device import (
    DeviceHandle,
    DeviceRegistry,
    DeviceStatus,
    MultiDeviceOrchestrator,
    ResponseType,
    register_device,
)
from openclaw_embodiment.hal.simulator import (
    SimulatedAudioOutput,
    SimulatedCamera,
    SimulatedDisplay,
    SimulatedTransport,
)


def _make_handle(
    device_id: str = "dev-1",
    profile: str = "test",
    hal_stack: dict | None = None,
    status: DeviceStatus = DeviceStatus.ACTIVE,
) -> DeviceHandle:
    if hal_stack is None:
        hal_stack = {}
    return DeviceHandle(
        device_id=device_id,
        profile=profile,
        hal_stack=hal_stack,
        transport=SimulatedTransport(),
        status=status,
        config={},
    )


class TestDeviceHandle:
    def test_capabilities_empty(self) -> None:
        h = _make_handle(hal_stack={})
        assert h.capabilities == []

    def test_capabilities_display(self) -> None:
        h = _make_handle(hal_stack={"display": SimulatedDisplay()})
        assert "display" in h.capabilities

    def test_capabilities_audio(self) -> None:
        h = _make_handle(hal_stack={"audio": SimulatedAudioOutput()})
        assert "audio_output" in h.capabilities

    def test_capabilities_camera(self) -> None:
        h = _make_handle(hal_stack={"camera": SimulatedCamera()})
        assert "camera" in h.capabilities

    def test_has_capability(self) -> None:
        h = _make_handle(hal_stack={"display": SimulatedDisplay()})
        assert h.has_capability("display") is True
        assert h.has_capability("actuator") is False

    def test_is_active_active(self) -> None:
        h = _make_handle(status=DeviceStatus.ACTIVE)
        assert h.is_active() is True

    def test_is_active_degraded(self) -> None:
        h = _make_handle(status=DeviceStatus.DEGRADED)
        assert h.is_active() is True

    def test_is_active_inactive(self) -> None:
        h = _make_handle(status=DeviceStatus.INACTIVE)
        assert h.is_active() is False


class TestDeviceRegistry:
    def test_register_and_get(self) -> None:
        reg = DeviceRegistry()
        h = _make_handle("dev-1")
        reg.register(h)
        assert reg.get("dev-1") is h

    def test_register_duplicate_raises(self) -> None:
        reg = DeviceRegistry()
        h = _make_handle("dev-1")
        reg.register(h)
        with pytest.raises(ValueError, match="already registered"):
            reg.register(h)

    def test_unregister(self) -> None:
        reg = DeviceRegistry()
        h = _make_handle("dev-1")
        reg.register(h)
        reg.unregister("dev-1")
        with pytest.raises(KeyError):
            reg.get("dev-1")

    def test_unregister_unknown_raises(self) -> None:
        reg = DeviceRegistry()
        with pytest.raises(KeyError):
            reg.unregister("nonexistent")

    def test_get_unknown_raises(self) -> None:
        reg = DeviceRegistry()
        with pytest.raises(KeyError):
            reg.get("no-such-device")

    def test_list_active(self) -> None:
        reg = DeviceRegistry()
        reg.register(_make_handle("a", status=DeviceStatus.ACTIVE))
        reg.register(_make_handle("b", status=DeviceStatus.INACTIVE))
        reg.register(_make_handle("c", status=DeviceStatus.DEGRADED))
        active = reg.list_active()
        ids = {h.device_id for h in active}
        assert "a" in ids
        assert "c" in ids
        assert "b" not in ids

    def test_get_by_capability(self) -> None:
        reg = DeviceRegistry()
        reg.register(_make_handle("d1", hal_stack={"display": SimulatedDisplay()}, status=DeviceStatus.ACTIVE))
        reg.register(_make_handle("d2", hal_stack={}, status=DeviceStatus.ACTIVE))
        caps = reg.get_by_capability("display")
        assert len(caps) == 1
        assert caps[0].device_id == "d1"

    def test_all(self) -> None:
        reg = DeviceRegistry()
        reg.register(_make_handle("x"))
        reg.register(_make_handle("y"))
        assert len(reg.all()) == 2


class TestMultiDeviceOrchestrator:
    def _setup(self):
        reg = DeviceRegistry()
        # Glasses: display + audio
        glasses = _make_handle(
            "glasses",
            profile="even-g2",
            hal_stack={
                "display": SimulatedDisplay(),
                "audio": SimulatedAudioOutput(),
            },
        )
        # Robot: actuator only
        from openclaw_embodiment.hal.simulator import SimulatedActuator
        robot = _make_handle(
            "robot",
            profile="reachy2",
            hal_stack={"actuator": SimulatedActuator()},
        )
        reg.register(glasses)
        reg.register(robot)
        return reg, MultiDeviceOrchestrator(registry=reg)

    def test_route_text_gets_display_and_audio(self) -> None:
        reg, orch = self._setup()
        targets = orch.route(ResponseType.TEXT)
        ids = {h.device_id for h in targets}
        assert "glasses" in ids
        assert "robot" not in ids

    def test_route_image_display_only(self) -> None:
        reg, orch = self._setup()
        targets = orch.route(ResponseType.IMAGE)
        ids = {h.device_id for h in targets}
        assert "glasses" in ids
        assert "robot" not in ids

    def test_route_actuate_robot_only(self) -> None:
        reg, orch = self._setup()
        targets = orch.route(ResponseType.ACTUATE)
        ids = {h.device_id for h in targets}
        assert "robot" in ids
        assert "glasses" not in ids

    def test_route_audio_glasses_only(self) -> None:
        reg, orch = self._setup()
        targets = orch.route(ResponseType.AUDIO)
        ids = {h.device_id for h in targets}
        assert "glasses" in ids
        assert "robot" not in ids

    def test_route_heartbeat_all(self) -> None:
        reg, orch = self._setup()
        targets = orch.route(ResponseType.HEARTBEAT)
        ids = {h.device_id for h in targets}
        assert "glasses" in ids
        assert "robot" in ids

    def test_route_empty_registry(self) -> None:
        reg = DeviceRegistry()
        orch = MultiDeviceOrchestrator(registry=reg)
        assert orch.route(ResponseType.TEXT) == []

    def test_route_text_convenience(self) -> None:
        _, orch = self._setup()
        assert len(orch.route_text()) > 0

    def test_route_actuate_convenience(self) -> None:
        _, orch = self._setup()
        assert len(orch.route_actuate()) > 0


class TestRegisterDevice:
    def test_register_device_creates_handle(self) -> None:
        reg = DeviceRegistry()
        h = register_device("test-profile", {"key": "val"}, registry=reg)
        assert h.profile == "test-profile"
        assert h.status == DeviceStatus.PENDING
        assert h in reg.all()

    def test_register_device_auto_id(self) -> None:
        reg = DeviceRegistry()
        h = register_device("test-profile", {}, registry=reg)
        assert h.device_id.startswith("test-profile-")

    def test_register_device_explicit_id(self) -> None:
        reg = DeviceRegistry()
        h = register_device("profile", {}, device_id="my-device", registry=reg)
        assert h.device_id == "my-device"
