import pytest

from openclaw_wearable.hal.base import ActuatorCommand, ActuatorHal, ActuatorResult, AudioOutputHal, CameraHal, ClassifierHal, DisplayHal, IMUHal, MicrophoneHal, TransportHal
from openclaw_wearable.hal.reachy_reference import ReachyActuatorHAL
from openclaw_wearable.hal.simulator import SimulatedAudioOutput, SimulatedCamera, SimulatedClassifier, SimulatedDisplay, SimulatedIMU, SimulatedMicrophone, SimulatedTransport


def test_hal_contract_instances():
    assert isinstance(SimulatedIMU(), IMUHal)
    assert isinstance(SimulatedCamera(), CameraHal)
    assert isinstance(SimulatedMicrophone(), MicrophoneHal)
    assert isinstance(SimulatedClassifier(), ClassifierHal)
    assert isinstance(SimulatedTransport(), TransportHal)
    assert isinstance(SimulatedDisplay(), DisplayHal)
    assert isinstance(SimulatedAudioOutput(), AudioOutputHal)


class TestActuatorHal:
    """Tests for the ActuatorHAL abstraction and ReachyActuatorHAL implementation."""

    def test_actuator_hal_cannot_be_instantiated_directly(self):
        """ABC enforcement: ActuatorHal cannot be instantiated without implementing abstract methods."""
        with pytest.raises(TypeError):
            ActuatorHal()  # type: ignore[abstract]

    def test_reachy_actuator_capabilities(self):
        """ReachyActuatorHAL must expose all 6 supported action strings."""
        hal = ReachyActuatorHAL()
        caps = hal.get_capabilities()
        expected = {"move_head", "rotate_body", "animate_antennas", "set_expression", "nod", "shake_head"}
        assert set(caps) == expected
        assert len(caps) == 6

    def test_actuator_command_dataclass(self):
        """ActuatorCommand dataclass fields are correct and defaults apply."""
        cmd = ActuatorCommand(
            command_id="cmd-001",
            action="move_head",
            params={"pitch": 10.0, "yaw": 5.0, "speed": 1.0},
            timestamp_ms=1234567890,
        )
        assert cmd.command_id == "cmd-001"
        assert cmd.action == "move_head"
        assert cmd.params == {"pitch": 10.0, "yaw": 5.0, "speed": 1.0}
        assert cmd.timestamp_ms == 1234567890
        assert cmd.timeout_ms == 5000  # default
