import pytest

from openclaw_wearable.hal.base import (
    ActuatorCommand,
    ActuatorHal,
    ActuatorResult,
    AudioChunk,
    AudioOutputHal,
    CameraHal,
    ChargingState,
    ClassifierHal,
    DisplayHal,
    IMUHal,
    JointState,
    MicrophoneHal,
    PowerHal,
    PowerSource,
    TransportHal,
)
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


class TestPowerHal:
    """Tests for PowerHal ABC and related enums."""

    def test_power_hal_cannot_be_instantiated_directly(self):
        """ABC enforcement: PowerHal cannot be instantiated without implementing abstract methods."""
        with pytest.raises(TypeError):
            PowerHal()  # type: ignore[abstract]

    def test_charging_state_enum_values(self):
        """ChargingState enum must have CHARGING, DISCHARGING, FULL, UNKNOWN."""
        assert ChargingState.CHARGING.value == "charging"
        assert ChargingState.DISCHARGING.value == "discharging"
        assert ChargingState.FULL.value == "full"
        assert ChargingState.UNKNOWN.value == "unknown"

    def test_power_source_enum_values(self):
        """PowerSource enum must have BATTERY, WALL, USB, UNKNOWN."""
        assert PowerSource.BATTERY.value == "battery"
        assert PowerSource.WALL.value == "wall"
        assert PowerSource.USB.value == "usb"
        assert PowerSource.UNKNOWN.value == "unknown"


class TestMicrophoneDoA:
    """Tests for MicrophoneHal.get_doa() default stub."""

    def test_default_doa_returns_none(self):
        """Default get_doa() returns None (stub -- no DoA hardware)."""
        mic = SimulatedMicrophone()
        result = mic.get_doa()
        assert result is None


class TestJointState:
    """Tests for JointState dataclass."""

    def test_joint_state_dataclass(self):
        """JointState fields are correct and optional temperature defaults to None."""
        js = JointState(
            joint_id="head_pitch",
            position_degrees=15.0,
            velocity_dps=5.0,
            load_percent=42.0,
        )
        assert js.joint_id == "head_pitch"
        assert js.position_degrees == 15.0
        assert js.velocity_dps == 5.0
        assert js.load_percent == 42.0
        assert js.temperature_celsius is None

    def test_joint_state_with_temperature(self):
        """JointState accepts optional temperature_celsius."""
        js = JointState(
            joint_id="head_yaw",
            position_degrees=0.0,
            velocity_dps=0.0,
            load_percent=10.0,
            temperature_celsius=37.5,
        )
        assert js.temperature_celsius == 37.5


class TestNewProfiles:
    """Smoke tests for Pi Zero 2W, OAK-D, and Frame AR glasses HAL profiles."""

    def test_oakd_frame_change_imu_init(self):
        """OakDFrameChangeIMU with None camera_hal is an IMUHal instance."""
        from openclaw_wearable.hal.oakd_reference import OakDFrameChangeIMU
        from openclaw_wearable.hal.base import IMUHal

        imu = OakDFrameChangeIMU(camera_hal=None, change_threshold=0.05)
        assert isinstance(imu, IMUHal)

    def test_frame_transport_hal_init(self):
        """FrameTransportHAL with None frame instance is a TransportHal instance."""
        from openclaw_wearable.hal.frame_reference import FrameTransportHAL
        from openclaw_wearable.hal.base import TransportHal

        transport = FrameTransportHAL(frame_instance=None)
        assert isinstance(transport, TransportHal)

    def test_load_profile_pi_zero2w(self):
        """load_profile('pi-zero2w') returns dict with 'name' key."""
        from openclaw_wearable.profiles import load_profile

        profile = load_profile("pi-zero2w")
        assert isinstance(profile, dict)
        assert "name" in profile
        assert profile["name"] == "pi-zero2w"

    def test_load_profile_oakd(self):
        """load_profile('luxonis-oakd') returns dict with 'name' key."""
        from openclaw_wearable.profiles import load_profile

        profile = load_profile("luxonis-oakd")
        assert isinstance(profile, dict)
        assert "name" in profile
        assert profile["name"] == "luxonis-oakd"

    def test_load_profile_frame(self):
        """load_profile('frame-glasses') returns dict with 'name' key."""
        from openclaw_wearable.profiles import load_profile

        profile = load_profile("frame-glasses")
        assert isinstance(profile, dict)
        assert "name" in profile
        assert profile["name"] == "frame-glasses"
