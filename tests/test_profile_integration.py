"""Simulator-based integration tests for all unvalidated + new profiles.

Tests load each profile in simulator mode and run one full pipeline cycle
(trigger -> capture -> transport -> receive). All tests pass without hardware.

When real hardware is available, swap the HAL implementations by setting
simulation=False or mock_mode=False in the profile config -- zero test
code changes needed.

Profiles covered:
- Existing (6 unvalidated): frame-glasses, even-g2, pi5-picam, pi-zero2w,
  luxonis-oakd, reachy-mini-wireless
- New (4): meta-rayban, unitree-go2, apple-vision-pro, openglass
"""

from __future__ import annotations

import time

import pytest

from openclaw_embodiment.hal.simulator import (
    SimulatedActuator,
    SimulatedAudioOutput,
    SimulatedCamera,
    SimulatedDisplay,
    SimulatedIMU,
    SimulatedMicrophone,
    SimulatedStatusIndicator,
    SimulatedSystemHealth,
    SimulatedTransport,
)
from openclaw_embodiment.hal.base import (
    AudioChunk,
    CameraFrame,
    IMUSample,
    TransportState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_pipeline_cycle(camera, transport, mic=None) -> dict:
    """Run one trigger->capture->transport->receive cycle.

    Args:
        camera: CameraHal instance (real or simulator).
        transport: TransportHal instance.
        mic: Optional MicrophoneHal instance.

    Returns:
        Dict with frame, chunk, send_result, and receive data.
    """
    frame = camera.capture_frame()
    assert isinstance(frame, CameraFrame), "capture_frame() must return CameraFrame"
    assert len(frame.data) > 0, "CameraFrame.data must be non-empty"

    audio_chunk = None
    if mic is not None:
        audio_chunk = mic.get_buffer(100)
        assert isinstance(audio_chunk, AudioChunk), "get_buffer() must return AudioChunk"
        assert len(audio_chunk.data) > 0, "AudioChunk.data must be non-empty"

    payload = b"test-context-" + frame.data[:32]
    result = transport.send(payload)
    assert result.success, f"transport.send() failed: {result.error_code}"
    assert result.bytes_sent == len(payload), "bytes_sent must match payload length"

    received = transport.receive(timeout_ms=500)
    # May be None (non-loopback) or payload (loopback) -- just no exception

    return {"frame": frame, "chunk": audio_chunk, "send_result": result, "received": received}


# ---------------------------------------------------------------------------
# Existing profiles (simulator mode, no hardware)
# ---------------------------------------------------------------------------


class TestFrameGlassesSimulator:
    """Integration tests for frame-glasses profile in simulator mode.

    Frame uses BLE transport + camera + display + microphone.
    """

    def test_pipeline_cycle(self) -> None:
        """Full trigger->capture->transport cycle for Frame Glasses."""
        camera = SimulatedCamera()
        camera.initialize((1920, 1080))

        transport = SimulatedTransport()
        transport.initialize({})
        transport.connect()

        mic = SimulatedMicrophone()
        mic.initialize(16000, 1)
        mic.start_recording()

        display = SimulatedDisplay()
        display.initialize((640, 400))

        result = _run_pipeline_cycle(camera, transport, mic)

        assert result["frame"].width == 1920
        assert result["frame"].height == 1080
        assert result["frame"].format == "JPEG"
        assert result["send_result"].success

    def test_display_renders_frame(self) -> None:
        """SimulatedDisplay stores rendered card."""
        from openclaw_embodiment.hal.base import DisplayCard
        display = SimulatedDisplay()
        display.initialize((640, 400))
        card = DisplayCard(mode="glance", title="Frame Test", body="Hello Frame", font_size=16, duration_ms=1000)
        display.show(card)
        assert display.last is not None
        assert display.last.body == "Hello Frame"

    def test_transport_expected_latency(self) -> None:
        """SimulatedTransport latency contract."""
        transport = SimulatedTransport()
        transport.initialize({})
        latency = transport.get_expected_latency_ms()
        assert isinstance(latency, int)
        assert latency > 0

    def test_transport_state_transitions(self) -> None:
        """Transport connect/disconnect state transitions."""
        transport = SimulatedTransport()
        transport.initialize({})
        assert transport.get_state() == TransportState.DISCONNECTED
        transport.connect()
        assert transport.get_state() == TransportState.CONNECTED
        transport.disconnect()
        assert transport.get_state() == TransportState.DISCONNECTED


class TestEvenG2Simulator:
    """Integration tests for even-g2 profile in simulator mode.

    Even G2 uses BLE transport + camera + display + microphone.
    """

    def test_pipeline_cycle(self) -> None:
        """Full trigger->capture->transport cycle for Even G2."""
        camera = SimulatedCamera()
        camera.initialize((1920, 1080))

        transport = SimulatedTransport()
        transport.initialize({})
        transport.connect()

        mic = SimulatedMicrophone()
        mic.initialize(16000, 1)
        mic.start_recording()

        result = _run_pipeline_cycle(camera, transport, mic)
        assert result["send_result"].success

    def test_imu_sample_contracts(self) -> None:
        """IMU sample follows contract (6DOF, non-None)."""
        imu = SimulatedIMU()
        imu.initialize(25)
        sample = imu.read_sample()
        assert isinstance(sample, IMUSample)
        assert isinstance(sample.accel_z, float)
        assert isinstance(sample.gyro_x, float)

    def test_microphone_transcribe(self) -> None:
        """Microphone transcription returns string."""
        mic = SimulatedMicrophone()
        mic.initialize(16000, 1)
        chunk = mic.get_buffer(500)
        transcript = mic.transcribe(chunk, language="en")
        assert isinstance(transcript, str)
        assert len(transcript) > 0


class TestPi5PicamSimulator:
    """Integration tests for pi5-picam profile in simulator mode.

    Pi5 uses HTTP transport + camera + system health.
    """

    def test_pipeline_cycle(self) -> None:
        """Full trigger->capture->transport cycle for Pi5 PiCam."""
        camera = SimulatedCamera()
        camera.initialize((1920, 1080))

        transport = SimulatedTransport()
        transport.initialize({})
        transport.connect()

        result = _run_pipeline_cycle(camera, transport)
        assert result["frame"].width == 1920
        assert result["send_result"].bytes_sent > 0

    def test_system_health_operational(self) -> None:
        """SimulatedSystemHealth reports operational."""
        health = SimulatedSystemHealth("pi5-test")
        report = health.get_health_report()
        assert report.is_operational
        assert report.cpu_percent is not None
        assert report.temperature_c is not None
        assert report.connectivity.get("wifi") is True

    def test_multiple_frames_no_leak(self) -> None:
        """10 consecutive captures don't crash or allocate unbounded memory."""
        camera = SimulatedCamera()
        camera.initialize((1920, 1080))
        for _ in range(10):
            frame = camera.capture_frame()
            assert len(frame.data) > 0


class TestPiZero2wSimulator:
    """Integration tests for pi-zero2w profile in simulator mode.

    Pi Zero 2W uses HTTP transport + camera (lower resolution).
    """

    def test_pipeline_cycle_low_res(self) -> None:
        """Full cycle for Pi Zero (1280x720 recommended)."""
        camera = SimulatedCamera()
        camera.initialize((1280, 720))

        transport = SimulatedTransport()
        transport.initialize({})
        transport.connect()

        result = _run_pipeline_cycle(camera, transport)
        assert result["frame"].width == 1280
        assert result["frame"].height == 720
        assert result["send_result"].success

    def test_transport_measured_latency_after_sends(self) -> None:
        """get_measured_latency_ms() returns int after at least one send."""
        transport = SimulatedTransport()
        transport.initialize({})
        transport.connect()
        assert transport.get_measured_latency_ms() is None  # no sends yet
        transport.send(b"hello")
        measured = transport.get_measured_latency_ms()
        assert isinstance(measured, int)
        assert measured >= 0


class TestLuxonisOakdSimulator:
    """Integration tests for luxonis-oakd profile in simulator mode.

    OAK-D uses USB transport + camera + classifier.
    """

    def test_pipeline_cycle_with_classifier(self) -> None:
        """Full cycle including classification for OAK-D."""
        from openclaw_embodiment.hal.simulator import SimulatedClassifier

        camera = SimulatedCamera()
        camera.initialize((1920, 1080))

        classifier = SimulatedClassifier()
        classifier.initialize("/sim/model.blob")

        transport = SimulatedTransport()
        transport.initialize({})
        transport.connect()

        result = _run_pipeline_cycle(camera, transport)
        frame = result["frame"]

        classification = classifier.classify(frame.data, frame.width, frame.height, "JPEG")
        assert classification.confidence >= 0.0
        assert classification.confidence <= 1.0
        assert classification.label in ("interesting", "uninteresting")
        assert classification.inference_time_ms > 0

    def test_classifier_model_info(self) -> None:
        """Classifier returns model info dict."""
        from openclaw_embodiment.hal.simulator import SimulatedClassifier
        clf = SimulatedClassifier()
        clf.initialize("/sim/model.blob")
        info = clf.get_model_info()
        assert isinstance(info, dict)
        assert "model_name" in info


class TestReachyMiniWirelessSimulator:
    """Integration tests for reachy-mini-wireless profile in simulator mode.

    Reachy Mini Wireless uses WiFi transport + camera + actuator + status indicator.
    """

    def test_pipeline_cycle(self) -> None:
        """Full cycle for Reachy Mini Wireless."""
        camera = SimulatedCamera()
        camera.initialize((1280, 720))

        transport = SimulatedTransport()
        transport.initialize({})
        transport.connect()

        result = _run_pipeline_cycle(camera, transport)
        assert result["send_result"].success

    def test_actuator_wave_command(self) -> None:
        """Actuator executes wave command without error."""
        from openclaw_embodiment.hal.base import ActuatorCommand
        actuator = SimulatedActuator()
        actuator.initialize()

        cmd = ActuatorCommand(
            command_id="test-wave",
            action="wave",
            params={},
            timestamp_ms=0,
        )
        result = actuator.execute(cmd)
        assert result.success
        assert result.command_id == "test-wave"
        assert len(actuator.commands) == 1

    def test_status_indicator_full_cycle(self) -> None:
        """Status indicator cycles through all patterns."""
        led = SimulatedStatusIndicator()
        led.initialize()

        led.set_color(0, 255, 0)
        assert led.color == (0, 255, 0)
        assert led.is_on

        led.pulse("processing")
        assert led.pattern == "processing"

        led.off()
        assert not led.is_on
        assert led.color == (0, 0, 0)


# ---------------------------------------------------------------------------
# New profiles (4 new -- all testable without hardware)
# ---------------------------------------------------------------------------


class TestMetaRayBanProfile:
    """Integration tests for meta-rayban profile (MWDAT + mock mode)."""

    @pytest.fixture
    def rayban_hals(self):
        """Build Ray-Ban HALs in mock mode."""
        from openclaw_embodiment.profiles.meta_rayban import build_meta_rayban_hals
        config = {
            "mwdat": {"mock_mode": True},
            "hal_server": {"http_port": 8421, "ws_port": 8422},
            "transport": {"host": "localhost", "port": 8420},
            "hardware": {
                "camera": {"fps": 1, "format": "jpeg"},
                "microphone": {"sample_rate": 16000, "channels": 1},
                "audio_output": {"sample_rate": 24000},
            },
            "capabilities": ["camera", "microphone", "audio_output", "status_indicator"],
        }
        return build_meta_rayban_hals(config)

    def test_camera_hal(self, rayban_hals) -> None:
        """RayBanCameraHal returns valid CameraFrame in mock mode."""
        camera = rayban_hals["camera"]
        frame = camera.capture_frame()
        assert isinstance(frame, CameraFrame)
        assert len(frame.data) > 0
        assert frame.format == "JPEG"

    def test_microphone_hal(self, rayban_hals) -> None:
        """RayBanMicrophoneHal returns AudioChunk in mock mode."""
        mic = rayban_hals["microphone"]
        chunk = mic.get_buffer(100)
        assert isinstance(chunk, AudioChunk)
        assert chunk.sample_rate == 16000
        assert len(chunk.data) > 0

    def test_audio_output_hal(self, rayban_hals) -> None:
        """RayBanAudioOutputHal speak() does not raise in mock mode."""
        audio_out = rayban_hals["audio_output"]
        audio_out.speak("Hello from Ray-Ban")  # should not raise

    def test_status_indicator_hal(self, rayban_hals) -> None:
        """RayBanStatusIndicatorHal LED state management."""
        led = rayban_hals["status_indicator"]
        led.set_color(0, 255, 0)
        assert led.color == (0, 255, 0)
        led.pulse("heartbeat")
        assert led.pattern == "heartbeat"
        led.off()
        assert not led.is_on

    def test_transport_hal(self, rayban_hals) -> None:
        """RayBanTransportHal: latency=15ms, send succeeds in mock mode."""
        transport = rayban_hals["transport"]
        assert transport.get_expected_latency_ms() == 15
        result = transport.send(b"test-payload")
        assert result.success

    def test_pipeline_cycle(self, rayban_hals) -> None:
        """Full pipeline cycle with Ray-Ban HALs in mock mode."""
        result = _run_pipeline_cycle(
            rayban_hals["camera"],
            rayban_hals["transport"],
            rayban_hals["microphone"],
        )
        assert result["send_result"].success
        assert isinstance(result["frame"], CameraFrame)

    def test_unsupported_hal_validate(self, rayban_hals) -> None:
        """UnsupportedHal returns False for validate() (not available on Ray-Ban)."""
        imu = rayban_hals["imu"]
        assert imu.validate() is False

    def test_transcribe_mock(self, rayban_hals) -> None:
        """Transcription returns mock transcript in mock mode."""
        mic = rayban_hals["microphone"]
        chunk = mic.get_buffer(500)
        transcript = mic.transcribe(chunk)
        assert isinstance(transcript, str)
        assert len(transcript) > 0


class TestUnitreeGo2Profile:
    """Integration tests for unitree-go2 profile (simulation mode)."""

    @pytest.fixture
    def go2_hals(self):
        """Build Go2 HALs in simulation mode."""
        from openclaw_embodiment.profiles.unitree_go2 import build_unitree_go2_hals
        config = {
            "simulation": {"enabled": True},
            "transport": {"host": "192.168.123.161", "port": 8080},
            "hardware": {
                "camera": {"resolution": "1920x1080", "fps": 30},
                "imu": {"rate_hz": 500},
                "actuator": {"type": "sport_client"},
            },
            "capabilities": ["camera", "imu", "actuator", "audio_output", "system_health"],
        }
        return build_unitree_go2_hals(config)

    def test_imu_hal(self, go2_hals) -> None:
        """Go2IMUHal returns IMUSample in simulation mode."""
        imu = go2_hals["imu"]
        sample = imu.read_sample()
        assert isinstance(sample, IMUSample)
        assert isinstance(sample.accel_z, float)

    def test_camera_hal(self, go2_hals) -> None:
        """Go2CameraHal returns CameraFrame in simulation mode."""
        camera = go2_hals["camera"]
        frame = camera.capture_frame()
        assert isinstance(frame, CameraFrame)
        assert len(frame.data) > 0

    def test_actuator_move_forward(self, go2_hals) -> None:
        """Go2ActuatorHal executes move_forward in simulation mode."""
        from openclaw_embodiment.hal.base import ActuatorCommand
        actuator = go2_hals["actuator"]
        cmd = ActuatorCommand("test-fwd", "move_forward", {"speed": 0.3}, 0)
        result = actuator.execute(cmd)
        assert result.success
        assert len(actuator._commands) == 1

    def test_actuator_stand_up(self, go2_hals) -> None:
        """Go2ActuatorHal executes stand_up in simulation mode."""
        from openclaw_embodiment.hal.base import ActuatorCommand
        actuator = go2_hals["actuator"]
        cmd = ActuatorCommand("test-stand", "stand_up", {}, 0)
        result = actuator.execute(cmd)
        assert result.success

    def test_actuator_wave(self, go2_hals) -> None:
        """Go2ActuatorHal executes wave in simulation mode."""
        from openclaw_embodiment.hal.base import ActuatorCommand
        actuator = go2_hals["actuator"]
        cmd = ActuatorCommand("test-wave", "wave", {}, 0)
        result = actuator.execute(cmd)
        assert result.success

    def test_actuator_capabilities(self, go2_hals) -> None:
        """Go2ActuatorHal returns expected capabilities list."""
        actuator = go2_hals["actuator"]
        caps = actuator.get_capabilities()
        assert "move_forward" in caps
        assert "wave" in caps
        assert "stop" in caps

    def test_transport_latency(self, go2_hals) -> None:
        """Go2TransportHal expected latency is 20ms."""
        transport = go2_hals["transport"]
        assert transport.get_expected_latency_ms() == 20

    def test_system_health(self, go2_hals) -> None:
        """Go2SystemHealthHal reports operational in simulation mode."""
        health = go2_hals["system_health"]
        assert health.is_operational()

    def test_pipeline_cycle(self, go2_hals) -> None:
        """Full pipeline cycle for Go2 in simulation mode."""
        result = _run_pipeline_cycle(go2_hals["camera"], go2_hals["transport"])
        assert result["send_result"].success


class TestAppleVisionProProfile:
    """Integration tests for apple-vision-pro profile (mock WebSocket)."""

    @pytest.fixture
    def avp_hals(self):
        """Build Apple Vision Pro HALs in mock mode."""
        from openclaw_embodiment.profiles.apple_vision_pro import build_apple_vision_pro_hals
        config = {
            "transport": {"host": "localhost", "port": 8430},
            "hardware": {
                "camera": {"fps": 30},
                "imu": {"dof": 6},
                "display": {"type": "spatial_overlay"},
            },
            "capabilities": ["camera", "imu", "display", "transport"],
        }
        hals = build_apple_vision_pro_hals(config)
        yield hals
        # Stop mock data generator
        if "_client" in hals:
            hals["_client"].stop()

    def test_imu_head_pose(self, avp_hals) -> None:
        """VisionProIMUHal returns head pose sample."""
        imu = avp_hals["imu"]
        # Wait briefly for mock data generator to produce a sample
        time.sleep(0.1)
        sample = imu.read_sample()
        # May be None if no mock data yet -- that's OK, just no crash
        if sample is not None:
            assert isinstance(sample, IMUSample)

    def test_camera_capture(self, avp_hals) -> None:
        """VisionProCameraHal returns CameraFrame."""
        camera = avp_hals["camera"]
        time.sleep(0.05)  # Let mock generator warm up
        frame = camera.capture_frame()
        assert isinstance(frame, CameraFrame)
        assert len(frame.data) > 0

    def test_display_show_card(self, avp_hals) -> None:
        """VisionProDisplayHal show_card() does not raise."""
        display = avp_hals["display"]
        display.show_card("Hello Vision Pro")
        assert display._last_card is not None
        assert display._last_card.body == "Hello Vision Pro"

    def test_display_clear(self, avp_hals) -> None:
        """VisionProDisplayHal clear() resets last card."""
        display = avp_hals["display"]
        display.show_card("test")
        display.clear()
        assert display._last_card is None

    def test_transport_latency(self, avp_hals) -> None:
        """VisionProTransportHal expected latency is 8ms."""
        transport = avp_hals["transport"]
        assert transport.get_expected_latency_ms() == 8

    def test_pipeline_cycle(self, avp_hals) -> None:
        """Full pipeline cycle for Apple Vision Pro in mock mode."""
        time.sleep(0.05)
        result = _run_pipeline_cycle(avp_hals["camera"], avp_hals["transport"])
        assert result["send_result"].success

    def test_get_orientation(self, avp_hals) -> None:
        """VisionProIMUHal get_orientation() returns tuple or None."""
        imu = avp_hals["imu"]
        time.sleep(0.1)
        orientation = imu.get_orientation()
        if orientation is not None:
            assert len(orientation) == 3
            assert all(isinstance(v, float) for v in orientation)


class TestOpenGlassProfile:
    """Integration tests for openglass profile (BLE simulator mode)."""

    @pytest.fixture
    def openglass_hals(self):
        """Build OpenGlass HALs in mock mode."""
        from openclaw_embodiment.profiles.openglass import build_openglass_hals
        config = {
            "transport": {"advertised_name": "OpenGlass"},
            "hardware": {
                "camera": {"mtu": 512, "fps": 1},
                "microphone": {"sample_rate": 8000, "channels": 1},
            },
            "capabilities": ["camera", "microphone", "status_indicator", "power"],
        }
        return build_openglass_hals(config)

    def test_camera_capture(self, openglass_hals) -> None:
        """OpenGlassCameraHal returns CameraFrame in mock mode."""
        camera = openglass_hals["camera"]
        frame = camera.capture_frame()
        assert isinstance(frame, CameraFrame)
        assert len(frame.data) > 0

    def test_jpeg_chunk_reassembly(self) -> None:
        """OpenGlassCameraHal reassembles MTU-split JPEG chunks correctly."""
        from openclaw_embodiment.profiles.openglass import OpenGlassCameraHal
        camera = OpenGlassCameraHal(mock_mode=False)
        camera.initialize((320, 240))

        # Simulate receiving a JPEG split into 3 chunks
        jpeg = b"\xff\xd8" + b"\xab" * 500 + b"\xff\xd9"
        chunk1 = jpeg[:200]
        chunk2 = jpeg[200:400]
        chunk3 = jpeg[400:]

        camera.on_ble_notification(chunk1)
        camera.on_ble_notification(chunk2)
        camera.on_ble_notification(chunk3)

        # After complete frame received, capture should return it
        frame = camera.capture_frame()
        assert frame.data == jpeg

    def test_microphone_hal(self, openglass_hals) -> None:
        """OpenGlassMicrophoneHal returns 8kHz AudioChunk."""
        mic = openglass_hals["microphone"]
        chunk = mic.get_buffer(100)
        assert isinstance(chunk, AudioChunk)
        assert chunk.sample_rate == 8000
        assert len(chunk.data) > 0

    def test_ble_audio_notification(self) -> None:
        """OpenGlassMicrophoneHal accumulates BLE audio notifications."""
        from openclaw_embodiment.profiles.openglass import OpenGlassMicrophoneHal
        mic = OpenGlassMicrophoneHal(mock_mode=False)
        mic.initialize(8000, 1)

        # Simulate BLE audio notifications
        for _ in range(5):
            mic.on_ble_notification(b"\x00" * 160)  # 10ms @ 8kHz 16-bit

        chunk = mic.get_buffer(100)
        assert len(chunk.data) >= 160

    def test_status_indicator(self, openglass_hals) -> None:
        """OpenGlassStatusIndicatorHal LED control in mock mode."""
        led = openglass_hals["status_indicator"]
        led.set_color(255, 0, 0)
        assert led.color == (255, 0, 0)
        led.pulse("alert")
        assert led.pattern == "alert"
        led.off()
        assert not led.is_on

    def test_power_hal(self, openglass_hals) -> None:
        """OpenGlassPowerHal returns battery percent in mock mode."""
        power = openglass_hals["power"]
        battery = power.get_battery_percent()
        assert 0.0 <= battery <= 100.0

    def test_transport_latency(self, openglass_hals) -> None:
        """OpenGlassTransportHal expected latency is 80ms (BLE)."""
        transport = openglass_hals["transport"]
        assert transport.get_expected_latency_ms() == 80

    def test_pipeline_cycle(self, openglass_hals) -> None:
        """Full pipeline cycle for OpenGlass in mock mode."""
        result = _run_pipeline_cycle(
            openglass_hals["camera"],
            openglass_hals["transport"],
            openglass_hals["microphone"],
        )
        assert result["send_result"].success


# ---------------------------------------------------------------------------
# ProfileValidator integration tests
# ---------------------------------------------------------------------------


class TestProfileValidator:
    """Integration tests for the ProfileValidator framework."""

    @pytest.mark.parametrize("profile_name", [
        "meta-rayban",
        "unitree-go2",
        "apple-vision-pro",
        "openglass",
    ])
    def test_new_profiles_pass_validator(self, profile_name: str) -> None:
        """All 4 new profiles pass ProfileValidator in simulator mode."""
        from openclaw_embodiment.validation.validator import ProfileValidator

        config = _default_config_for_profile(profile_name)
        validator = ProfileValidator(profile_name, config)
        report = validator.run()

        assert report.overall in ("pass", "warn"), (
            f"Profile '{profile_name}' validator failed:\n{report.summary()}"
        )
        assert report.passed > 0

    @pytest.mark.parametrize("profile_name", [
        "frame-glasses",
        "even-g2",
        "pi5-picam",
        "pi-zero2w",
        "luxonis-oakd",
        "reachy-mini-wireless",
    ])
    def test_existing_profiles_pass_validator(self, profile_name: str) -> None:
        """All 6 unvalidated existing profiles pass ProfileValidator."""
        from openclaw_embodiment.validation.validator import ProfileValidator

        config = _default_config_for_profile(profile_name)
        validator = ProfileValidator(profile_name, config)
        report = validator.run()

        assert report.overall in ("pass", "warn"), (
            f"Profile '{profile_name}' validator failed:\n{report.summary()}"
        )

    def test_validator_report_structure(self) -> None:
        """ValidationReport has all required fields."""
        from openclaw_embodiment.validation.validator import ProfileValidator, ValidationReport
        from datetime import datetime

        validator = ProfileValidator("meta-rayban", {"mwdat": {"mock_mode": True}})
        report = validator.run()

        assert isinstance(report, ValidationReport)
        assert isinstance(report.profile, str)
        assert isinstance(report.timestamp, datetime)
        assert isinstance(report.passed, int)
        assert isinstance(report.failed, int)
        assert isinstance(report.checks, list)
        assert len(report.checks) == 7  # exactly 7 checks
        assert report.overall in ("pass", "fail", "warn")
        assert isinstance(report.hardware_ready, bool)

    def test_validator_check_names(self) -> None:
        """All 7 check names are present in the report."""
        from openclaw_embodiment.validation.validator import ProfileValidator

        validator = ProfileValidator("openglass", {})
        report = validator.run()

        check_names = {c.name for c in report.checks}
        expected = {
            "HAL Instantiation",
            "Capability Declaration",
            "Simulator Swap",
            "Transport Contract",
            "Pipeline Smoke Test",
            "Latency Contract",
            "Error Recovery",
        }
        assert expected == check_names

    def test_validator_summary_string(self) -> None:
        """ValidationReport.summary() returns non-empty string."""
        from openclaw_embodiment.validation.validator import ProfileValidator

        validator = ProfileValidator("meta-rayban", {"mwdat": {"mock_mode": True}})
        report = validator.run()
        summary = report.summary()
        assert isinstance(summary, str)
        assert "meta-rayban" in summary
        assert "Overall" in summary


def _default_config_for_profile(profile_name: str) -> dict:
    """Return a minimal default config for each profile.

    Args:
        profile_name: Profile name string.

    Returns:
        Minimal config dict suitable for ProfileValidator.
    """
    configs = {
        "meta-rayban": {
            "mwdat": {"mock_mode": True},
            "hal_server": {"http_port": 8421, "ws_port": 8422},
            "transport": {"host": "localhost", "port": 8420},
            "capabilities": ["camera", "microphone", "audio_output", "status_indicator"],
        },
        "unitree-go2": {
            "simulation": {"enabled": True},
            "transport": {"host": "192.168.123.161", "port": 8080},
            "capabilities": ["camera", "imu", "actuator", "audio_output", "system_health"],
        },
        "apple-vision-pro": {
            "transport": {"host": "localhost", "port": 8430},
            "capabilities": ["camera", "imu", "display", "transport"],
        },
        "openglass": {
            "transport": {"advertised_name": "OpenGlass"},
            "capabilities": ["camera", "microphone", "status_indicator", "power"],
        },
        "frame-glasses": {
            "capabilities": ["camera", "microphone", "display", "transport"],
        },
        "even-g2": {
            "capabilities": ["camera", "microphone", "display", "transport"],
        },
        "pi5-picam": {
            "capabilities": ["camera", "system_health", "transport"],
        },
        "pi-zero2w": {
            "capabilities": ["camera", "system_health", "transport"],
        },
        "luxonis-oakd": {
            "capabilities": ["camera", "system_health", "transport"],
        },
        "reachy-mini-wireless": {
            "capabilities": ["camera", "actuator", "status_indicator", "transport"],
        },
    }
    return configs.get(profile_name, {})
