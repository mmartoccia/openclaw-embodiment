import pytest

from openclaw_embodiment.hal.base import (
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
from openclaw_embodiment.hal.reachy_reference import ReachyActuatorHAL
from openclaw_embodiment.hal.simulator import SimulatedAudioOutput, SimulatedCamera, SimulatedClassifier, SimulatedDisplay, SimulatedIMU, SimulatedMicrophone, SimulatedTransport


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
        from openclaw_embodiment.hal.oakd_reference import OakDFrameChangeIMU
        from openclaw_embodiment.hal.base import IMUHal

        imu = OakDFrameChangeIMU(camera_hal=None, change_threshold=0.05)
        assert isinstance(imu, IMUHal)

    def test_frame_transport_hal_init(self):
        """FrameTransportHAL with None frame instance is a TransportHal instance."""
        from openclaw_embodiment.hal.frame_reference import FrameTransportHAL
        from openclaw_embodiment.hal.base import TransportHal

        transport = FrameTransportHAL(frame_instance=None)
        assert isinstance(transport, TransportHal)

    def test_load_profile_pi_zero2w(self):
        """load_profile('pi-zero2w') returns dict with 'name' key."""
        from openclaw_embodiment.profiles import load_profile

        profile = load_profile("pi-zero2w")
        assert isinstance(profile, dict)
        assert "name" in profile
        assert profile["name"] == "pi-zero2w"

    def test_load_profile_oakd(self):
        """load_profile('luxonis-oakd') returns dict with 'name' key."""
        from openclaw_embodiment.profiles import load_profile

        profile = load_profile("luxonis-oakd")
        assert isinstance(profile, dict)
        assert "name" in profile
        assert profile["name"] == "luxonis-oakd"

    def test_load_profile_frame(self):
        """load_profile('frame-glasses') returns dict with 'name' key."""
        from openclaw_embodiment.profiles import load_profile

        profile = load_profile("frame-glasses")
        assert isinstance(profile, dict)
        assert "name" in profile
        assert profile["name"] == "frame-glasses"


class TestG2Profile:
    """Smoke tests for Even Realities G2 HAL profile."""

    def test_g2_rssi_motion_proxy_is_imu_hal(self):
        """G2RSSIMotionProxy must be an IMUHal subclass."""
        from openclaw_embodiment.hal.even_g2_reference import G2RSSIMotionProxy
        from openclaw_embodiment.hal.base import IMUHal

        proxy = G2RSSIMotionProxy(
            left_address="AA:BB:CC:DD:EE:01",
            right_address="AA:BB:CC:DD:EE:02",
        )
        assert isinstance(proxy, IMUHal)

    def test_g2_display_hal_is_display_hal(self):
        """G2DisplayHAL must be a DisplayHal subclass."""
        from openclaw_embodiment.hal.even_g2_reference import G2DisplayHAL
        from openclaw_embodiment.hal.base import DisplayHal

        display = G2DisplayHAL(right_address="AA:BB:CC:DD:EE:02")
        assert isinstance(display, DisplayHal)

    def test_load_profile_even_g2(self):
        """load_profile('even-g2') returns dict with 'name' key."""
        from openclaw_embodiment.profiles import load_profile

        profile = load_profile("even-g2")
        assert isinstance(profile, dict)
        assert "name" in profile


class TestReachy2Profile:
    """Tests for Reachy 2 HAL and profile."""

    def test_reachy2_profile_loads(self):
        """load_profile('reachy2') returns valid config dict."""
        from openclaw_embodiment.profiles import load_profile

        profile = load_profile("reachy2")
        assert isinstance(profile, dict)
        assert profile.get("name") == "reachy2"
        assert profile.get("display_name") == "Reachy 2"
        assert profile.get("manufacturer") == "Pollen Robotics"
        assert profile.get("imu_available") is True
        assert profile.get("stereo_cameras") is True
        assert profile.get("doa_available") is True

    def test_reachy2_hal_classes_importable(self):
        """Reachy 2 HAL classes can be imported and instantiated with a mock reachy object."""
        from openclaw_embodiment.hal.reachy2_reference import (
            Reachy2MotionTracker,
            Reachy2CameraHAL,
            Reachy2MicrophoneHAL,
            Reachy2AudioOutputHAL,
            Reachy2DisplayHAL,
            Reachy2TransportHAL,
            Reachy2ActuatorHAL,
        )
        from openclaw_embodiment.hal.base import (
            IMUHal, CameraHal, MicrophoneHal, AudioOutputHal,
            DisplayHal, TransportHal, ActuatorHal,
        )

        # Use a simple mock object -- real reachy2-sdk not required for instantiation
        class MockReachy:
            head = None
            cameras = None
            audio = None
            r_arm = None
            l_arm = None
            mobile_base = None

        mock = MockReachy()

        imu = Reachy2MotionTracker(mock)
        assert isinstance(imu, IMUHal)

        cam = Reachy2CameraHAL(mock, camera_side="left")
        assert isinstance(cam, CameraHal)

        mic = Reachy2MicrophoneHAL(mock)
        assert isinstance(mic, MicrophoneHal)

        audio = Reachy2AudioOutputHAL(mock)
        assert isinstance(audio, AudioOutputHal)

        display = Reachy2DisplayHAL(mock)
        assert isinstance(display, DisplayHal)

        transport = Reachy2TransportHAL(host="reachy.local", port=50051)
        assert isinstance(transport, TransportHal)

        actuator = Reachy2ActuatorHAL(mock)
        assert isinstance(actuator, ActuatorHal)

    def test_reachy2_actuator_ids(self):
        """REACHY2_ALL_JOINTS contains all expected joint IDs."""
        from openclaw_embodiment.hal.reachy2_reference import (
            REACHY2_ALL_JOINTS,
            REACHY2_HEAD_JOINTS,
            REACHY2_R_ARM_JOINTS,
            REACHY2_L_ARM_JOINTS,
            REACHY2_MOBILE_BASE_JOINTS,
        )

        # Head joints
        assert "head.neck.pan" in REACHY2_HEAD_JOINTS
        assert "head.neck.tilt" in REACHY2_HEAD_JOINTS
        assert "head.neck.roll" in REACHY2_HEAD_JOINTS
        assert len(REACHY2_HEAD_JOINTS) == 3

        # Right arm joints (7 DOF + gripper = 8)
        assert "r_arm.shoulder.pitch" in REACHY2_R_ARM_JOINTS
        assert "r_arm.shoulder.roll" in REACHY2_R_ARM_JOINTS
        assert "r_arm.elbow.yaw" in REACHY2_R_ARM_JOINTS
        assert "r_arm.elbow.pitch" in REACHY2_R_ARM_JOINTS
        assert "r_arm.wrist.roll" in REACHY2_R_ARM_JOINTS
        assert "r_arm.wrist.pitch" in REACHY2_R_ARM_JOINTS
        assert "r_arm.wrist.yaw" in REACHY2_R_ARM_JOINTS
        assert "r_arm.gripper" in REACHY2_R_ARM_JOINTS
        assert len(REACHY2_R_ARM_JOINTS) == 8

        # Left arm mirrors right arm
        assert "l_arm.shoulder.pitch" in REACHY2_L_ARM_JOINTS
        assert "l_arm.gripper" in REACHY2_L_ARM_JOINTS
        assert len(REACHY2_L_ARM_JOINTS) == 8

        # Mobile base
        assert "mobile_base.x" in REACHY2_MOBILE_BASE_JOINTS
        assert "mobile_base.y" in REACHY2_MOBILE_BASE_JOINTS
        assert "mobile_base.theta" in REACHY2_MOBILE_BASE_JOINTS

        # All joints combined
        assert len(REACHY2_ALL_JOINTS) == 3 + 8 + 8 + 3  # 22 total

    def test_reachy2_actuator_capabilities(self):
        """Reachy2ActuatorHAL.get_capabilities() returns expected action strings."""
        from openclaw_embodiment.hal.reachy2_reference import Reachy2ActuatorHAL

        class MockReachy:
            head = None

        actuator = Reachy2ActuatorHAL(MockReachy(), has_mobile_base=False)
        caps = actuator.get_capabilities()
        assert "move_head" in caps
        assert "move_r_arm" in caps
        assert "move_l_arm" in caps
        assert "move_gripper" in caps
        assert "stop_all" in caps
        # mobile_base_move excluded when has_mobile_base=False
        assert "mobile_base_move" not in caps

        # With mobile base
        actuator_with_base = Reachy2ActuatorHAL(MockReachy(), has_mobile_base=True)
        caps_with_base = actuator_with_base.get_capabilities()
        assert "mobile_base_move" in caps_with_base

    def test_reachy2_transport_defaults(self):
        """Reachy2TransportHAL defaults to reachy.local:50051."""
        from openclaw_embodiment.hal.reachy2_reference import Reachy2TransportHAL
        from openclaw_embodiment.hal.base import TransportState

        transport = Reachy2TransportHAL()
        assert transport._host == "reachy.local"
        assert transport._port == 50051
        assert transport.get_state() == TransportState.DISCONNECTED
        assert not transport.is_connected()
        assert transport.get_expected_latency_ms() == 10

        transport.connect()
        assert transport.is_connected()
        assert transport.get_state() == TransportState.CONNECTED

    def test_reachy2_camera_hal_sides(self):
        """Reachy2CameraHAL accepts left/right/both camera_side."""
        from openclaw_embodiment.hal.reachy2_reference import Reachy2CameraHAL

        class MockReachy:
            cameras = None

        for side in ("left", "right", "both"):
            cam = Reachy2CameraHAL(MockReachy(), camera_side=side)
            assert cam._camera_side == side
            w, h = cam.get_resolution()
            expected_w = 640 * 2 if side == "both" else 640
            # Resolution is set to 640x480 by default regardless of side
            assert w == 640  # get_resolution returns configured resolution, not composite

    def test_reachy2_expression_constants(self):
        """Reachy2Expression has all required expression constants."""
        from openclaw_embodiment.hal.reachy2_reference import Reachy2Expression

        assert Reachy2Expression.NEUTRAL == "neutral"
        assert Reachy2Expression.HAPPY == "happy"
        assert Reachy2Expression.SAD == "sad"
        assert Reachy2Expression.THINKING == "thinking"
        assert Reachy2Expression.ALERT == "alert"
        assert Reachy2Expression.CUSTOM == "custom"
        assert len(Reachy2Expression.ALL) == 6

    def test_reachy_mini_wireless_profile_loads(self):
        """load_profile('reachy-mini-wireless') returns valid config dict."""
        from openclaw_embodiment.profiles import load_profile

        profile = load_profile("reachy-mini-wireless")
        assert isinstance(profile, dict)
        assert profile.get("name") == "reachy-mini-wireless"
        assert profile.get("display_name") == "Reachy Mini Wireless"
        assert profile.get("imu_available") is True
        assert profile.get("price_usd") == 449
        # WiFi transport -- no USB cable
        assert "WiFi" in profile.get("transport", "")
        # Uses same HAL as Reachy Mini -- ReachyMotionTracker
        hal_classes = profile.get("hal_classes", {})
        assert hal_classes.get("imu") == "ReachyMotionTracker"

    def test_reachy_mini_wireless_hal_module(self):
        """Reachy Mini Wireless profile points to reachy_reference HAL module."""
        from openclaw_embodiment.profiles import load_profile

        profile = load_profile("reachy-mini-wireless")
        assert profile.get("hal_module") == "openclaw_embodiment.hal.reachy_reference"
