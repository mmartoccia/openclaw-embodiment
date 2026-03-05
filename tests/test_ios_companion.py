"""Tests for the iOS Companion Profile -- iPhone sensor node.

Covers:
  - iOSSensorPayload creation and validation
  - iOSCompanionReceiver endpoint routing (mock HTTP)
  - IMU payload -> IMUSample conversion
  - Camera payload -> CameraFrame handling
  - Audio payload -> AudioChunk conversion
  - HMAC signature validation (valid + invalid)
  - Profile registration in profiles __init__
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import io
import json
import time
from dataclasses import fields
from http.client import HTTPResponse
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from openclaw_embodiment.profiles.ios_companion import (
    COMPANION_PORT,
    FORMAT_VERSION,
    PROFILE,
    SIGNATURE_HEADER,
    CompanionProtocolSpec,
    _audio_payload_to_chunk,
    _camera_payload_to_frame,
    _compute_hmac,
    _imu_payload_to_sample,
    iOSCameraHal,
    iOSCompanionProfile,
    iOSCompanionReceiver,
    iOSIMUHal,
    iOSMicrophoneHal,
    iOSSensorPayload,
    verify_hmac,
)
from openclaw_embodiment.hal.base import AudioChunk, CameraFrame, IMUSample


# ── Fixtures ──────────────────────────────────────────────────────────────────


DEVICE_ID = "TEST-DEVICE-UUID-1234"
SECRET = b"test-hmac-secret-32bytes!!!!!!!!"


def _make_imu_payload(**overrides) -> iOSSensorPayload:
    data = {
        "accel_x": 1.0,
        "accel_y": -9.806,
        "accel_z": 0.1,
        "gyro_x": 0.01,
        "gyro_y": -0.02,
        "gyro_z": 0.0,
        "sample_rate_hz": 50,
    }
    return iOSSensorPayload(
        device_id=overrides.get("device_id", DEVICE_ID),
        sensor_type=overrides.get("sensor_type", "imu"),
        timestamp=overrides.get("timestamp", 1709500000.123),
        data=overrides.get("data", data),
        format_version=overrides.get("format_version", FORMAT_VERSION),
    )


def _make_camera_payload(raw_bytes: bytes = b"\xff\xd8\xff" + b"\x00" * 100) -> iOSSensorPayload:
    compressed = gzip.compress(raw_bytes)
    encoded = base64.b64encode(compressed).decode()
    return iOSSensorPayload(
        device_id=DEVICE_ID,
        sensor_type="camera",
        timestamp=1709500000.033,
        data={
            "width": 320,
            "height": 240,
            "format": "JPEG",
            "encoding": "gzip+b64",
            "frame_data": encoded,
        },
        format_version=FORMAT_VERSION,
    )


def _make_audio_payload(pcm_bytes: bytes = b"\x00\x01" * 1600) -> iOSSensorPayload:
    encoded = base64.b64encode(pcm_bytes).decode()
    return iOSSensorPayload(
        device_id=DEVICE_ID,
        sensor_type="audio",
        timestamp=1709500000.100,
        data={
            "sample_rate": 16000,
            "channels": 1,
            "format": "PCM_S16LE",
            "encoding": "b64",
            "audio_data": encoded,
            "duration_ms": 100,
        },
        format_version=FORMAT_VERSION,
    )


# ── Test 1: iOSSensorPayload dataclass creation and validation ─────────────────


class TestiOSSensorPayload:
    def test_creation_defaults(self):
        """iOSSensorPayload can be created with required fields."""
        p = iOSSensorPayload(
            device_id=DEVICE_ID,
            sensor_type="imu",
            timestamp=1709500000.0,
            data={"accel_x": 0.0},
        )
        assert p.device_id == DEVICE_ID
        assert p.sensor_type == "imu"
        assert p.timestamp == 1709500000.0
        assert p.format_version == FORMAT_VERSION

    def test_validation_passes_valid_payload(self):
        """Valid payload passes validation without raising."""
        p = _make_imu_payload()
        p.validate()  # should not raise

    def test_validation_rejects_empty_device_id(self):
        """Validation raises ValueError for empty device_id."""
        p = _make_imu_payload(device_id="")
        with pytest.raises(ValueError, match="device_id"):
            p.validate()

    def test_validation_rejects_unknown_sensor_type(self):
        """Validation raises ValueError for unknown sensor_type."""
        p = _make_imu_payload(sensor_type="lidar")
        with pytest.raises(ValueError, match="sensor_type"):
            p.validate()

    def test_validation_rejects_zero_timestamp(self):
        """Validation raises ValueError for zero timestamp."""
        p = _make_imu_payload(timestamp=0.0)
        with pytest.raises(ValueError, match="timestamp"):
            p.validate()

    def test_validation_rejects_wrong_format_version(self):
        """Validation raises ValueError for unsupported format_version."""
        p = _make_imu_payload(format_version="9.9")
        with pytest.raises(ValueError, match="format_version"):
            p.validate()

    def test_from_dict_roundtrip(self):
        """from_dict correctly deserialises a dict representation."""
        raw = {
            "device_id": DEVICE_ID,
            "sensor_type": "location",
            "timestamp": 1709500000.0,
            "data": {"latitude": 37.77, "longitude": -122.41},
            "format_version": FORMAT_VERSION,
        }
        p = iOSSensorPayload.from_dict(raw)
        assert p.device_id == DEVICE_ID
        assert p.sensor_type == "location"
        assert p.data["latitude"] == 37.77

    def test_all_valid_sensor_types_pass_validation(self):
        for sensor_type in ("imu", "camera", "audio", "location", "battery"):
            p = iOSSensorPayload(
                device_id=DEVICE_ID,
                sensor_type=sensor_type,
                timestamp=1709500000.0,
                data={},
                format_version=FORMAT_VERSION,
            )
            p.validate()  # should not raise


# ── Test 2: IMU payload -> IMUSample conversion ───────────────────────────────


class TestIMUConversion:
    def test_imu_payload_to_sample_values(self):
        """IMU payload converts to IMUSample with correct field values."""
        payload = _make_imu_payload()
        sample = _imu_payload_to_sample(payload)

        assert isinstance(sample, IMUSample)
        assert sample.accel_x == 1.0
        assert sample.accel_y == pytest.approx(-9.806)
        assert sample.accel_z == 0.1
        assert sample.gyro_x == 0.01
        assert sample.gyro_y == pytest.approx(-0.02)
        assert sample.gyro_z == 0.0

    def test_imu_sample_timestamp_in_ms(self):
        """IMUSample timestamp is in milliseconds."""
        payload = _make_imu_payload(timestamp=1709500000.5)
        sample = _imu_payload_to_sample(payload)
        assert sample.timestamp_ms == 1709500000500

    def test_imu_hal_push_and_read(self):
        """iOSIMUHal.push_sample() and read_sample() work as a FIFO queue."""
        hal = iOSIMUHal()
        hal.initialize()
        payload = _make_imu_payload()
        sample = _imu_payload_to_sample(payload)
        hal.push_sample(sample, DEVICE_ID)

        result = hal.read_sample()
        assert result is not None
        assert result.accel_y == pytest.approx(-9.806)

        # Buffer now empty
        assert hal.read_sample() is None

    def test_imu_hal_validate_after_init(self):
        """iOSIMUHal.validate() returns True after initialize()."""
        hal = iOSIMUHal()
        assert not hal.validate()
        hal.initialize()
        assert hal.validate()

    def test_imu_hal_get_device_info(self):
        """iOSIMUHal.get_device_info() returns expected keys."""
        hal = iOSIMUHal()
        hal.initialize()
        info = hal.get_device_info()
        assert info["hal"] == "iOSIMUHal"
        assert "sample_rate_hz" in info


# ── Test 3: Camera payload -> CameraFrame handling ────────────────────────────


class TestCameraConversion:
    def test_camera_payload_decompresses_gzip(self):
        """Camera payload with gzip+b64 encoding is correctly decompressed."""
        raw_jpeg = b"\xff\xd8\xff" + b"\xAB" * 200
        payload = _make_camera_payload(raw_bytes=raw_jpeg)
        frame = _camera_payload_to_frame(payload)

        assert isinstance(frame, CameraFrame)
        assert frame.data == raw_jpeg
        assert frame.width == 320
        assert frame.height == 240
        assert frame.format == "JPEG"

    def test_camera_frame_timestamp_ms(self):
        """CameraFrame timestamp is correctly converted to milliseconds."""
        payload = _make_camera_payload()
        payload = iOSSensorPayload(
            device_id=payload.device_id,
            sensor_type=payload.sensor_type,
            timestamp=1709500000.033,
            data=payload.data,
            format_version=payload.format_version,
        )
        frame = _camera_payload_to_frame(payload)
        assert frame.timestamp_ms == 1709500000033

    def test_camera_hal_push_and_capture(self):
        """iOSCameraHal returns the most recently pushed frame."""
        hal = iOSCameraHal()
        hal.initialize()
        payload = _make_camera_payload()
        frame = _camera_payload_to_frame(payload)
        hal.push_frame(frame, DEVICE_ID)

        captured = hal.capture_frame()
        assert captured.width == 320
        assert captured.height == 240

    def test_camera_hal_capture_raises_when_empty(self):
        """capture_frame() raises RuntimeError when no frame has been received."""
        hal = iOSCameraHal()
        hal.initialize()
        with pytest.raises(RuntimeError, match="No camera frame"):
            hal.capture_frame()

    def test_camera_hal_get_raw_frame_returns_bytes(self):
        """get_raw_frame() returns bytes after a frame is pushed."""
        hal = iOSCameraHal()
        hal.initialize()
        payload = _make_camera_payload(raw_bytes=b"\xff\xd8\xff" + b"\x00" * 50)
        hal.push_frame(_camera_payload_to_frame(payload), DEVICE_ID)

        raw = hal.get_raw_frame()
        assert isinstance(raw, bytes)
        assert raw[:3] == b"\xff\xd8\xff"


# ── Test 4: Audio payload -> AudioChunk conversion ────────────────────────────


class TestAudioConversion:
    def test_audio_payload_to_chunk_values(self):
        """Audio payload converts to AudioChunk with correct field values."""
        pcm = b"\x00\x01" * 1600
        payload = _make_audio_payload(pcm_bytes=pcm)
        chunk = _audio_payload_to_chunk(payload)

        assert isinstance(chunk, AudioChunk)
        assert chunk.data == pcm
        assert chunk.sample_rate == 16000
        assert chunk.channels == 1
        assert chunk.format == "PCM_S16LE"

    def test_audio_chunk_timestamp_ms(self):
        """AudioChunk timestamp is in milliseconds."""
        payload = _make_audio_payload()
        chunk = _audio_payload_to_chunk(payload)
        assert chunk.timestamp_ms == 1709500000100

    def test_mic_hal_push_and_get(self):
        """iOSMicrophoneHal.push_chunk() and get_buffer() work as a FIFO queue."""
        hal = iOSMicrophoneHal()
        hal.initialize()
        hal.start_recording()
        payload = _make_audio_payload()
        chunk = _audio_payload_to_chunk(payload)
        hal.push_chunk(chunk, DEVICE_ID)

        result = hal.get_buffer(100)
        assert result.sample_rate == 16000
        assert result.data == chunk.data

    def test_mic_hal_get_buffer_empty_returns_silence(self):
        """get_buffer() returns a silent chunk when buffer is empty."""
        hal = iOSMicrophoneHal()
        hal.initialize(sample_rate=16000, channels=1)
        result = hal.get_buffer(100)
        assert isinstance(result.data, bytes)
        assert len(result.data) > 0  # silent chunk has non-zero length
        assert all(b == 0 for b in result.data)

    def test_mic_hal_doa_returns_none(self):
        """get_doa() returns None (iPhone has single mic, no DoA)."""
        hal = iOSMicrophoneHal()
        hal.initialize()
        assert hal.get_doa() is None


# ── Test 5: HMAC signature validation ─────────────────────────────────────────


class TestHMACValidation:
    def test_valid_signature_passes(self):
        """verify_hmac returns True for a correctly signed body."""
        body = b'{"sensor_type": "imu"}'
        sig = _compute_hmac(SECRET, body)
        assert verify_hmac(SECRET, body, sig) is True

    def test_invalid_signature_fails(self):
        """verify_hmac returns False for a tampered signature."""
        body = b'{"sensor_type": "imu"}'
        assert verify_hmac(SECRET, body, "deadbeef" * 8) is False

    def test_tampered_body_fails(self):
        """verify_hmac returns False when body has been modified after signing."""
        original_body = b'{"sensor_type": "imu"}'
        sig = _compute_hmac(SECRET, original_body)
        tampered_body = b'{"sensor_type": "camera"}'
        assert verify_hmac(SECRET, tampered_body, sig) is False

    def test_different_secrets_fail(self):
        """verify_hmac returns False when verified with wrong secret."""
        body = b'{"sensor_type": "imu"}'
        sig = _compute_hmac(b"correct-secret", body)
        assert verify_hmac(b"wrong-secret!!", body, sig) is False

    def test_hmac_is_case_insensitive(self):
        """verify_hmac accepts uppercase hex signatures."""
        body = b'{"sensor_type": "imu"}'
        sig = _compute_hmac(SECRET, body).upper()
        assert verify_hmac(SECRET, body, sig) is True


# ── Test 6: iOSCompanionReceiver endpoint routing (mock HTTP) ──────────────────


class TestReceiverEndpoints:
    """Test the receiver's handler logic by directly calling internal methods."""

    def _make_receiver(self) -> iOSCompanionReceiver:
        # Create receiver without actually binding a socket
        receiver = iOSCompanionReceiver.__new__(iOSCompanionReceiver)
        receiver.hmac_secret = None
        receiver.imu_hal = iOSIMUHal()
        receiver.camera_hal = iOSCameraHal()
        receiver.mic_hal = iOSMicrophoneHal()
        receiver.location_callback = None
        receiver.battery_callback = None
        receiver._thread = None
        receiver.imu_hal.initialize()
        receiver.camera_hal.initialize()
        receiver.mic_hal.initialize()
        return receiver

    def test_handle_imu_pushes_to_hal(self):
        """_handle_imu() pushes a sample into imu_hal buffer."""
        receiver = self._make_receiver()
        payload = _make_imu_payload()
        result = receiver._handle_imu(payload)

        assert result["status"] == "ok"
        sample = receiver.imu_hal.read_sample()
        assert sample is not None
        assert sample.accel_y == pytest.approx(-9.806)

    def test_handle_camera_pushes_to_hal(self):
        """_handle_camera() pushes a frame into camera_hal."""
        receiver = self._make_receiver()
        payload = _make_camera_payload()
        result = receiver._handle_camera(payload)

        assert result["status"] == "ok"
        frame = receiver.camera_hal.capture_frame()
        assert frame.width == 320

    def test_handle_audio_pushes_to_hal(self):
        """_handle_audio() pushes a chunk into mic_hal buffer."""
        receiver = self._make_receiver()
        payload = _make_audio_payload()
        result = receiver._handle_audio(payload)

        assert result["status"] == "ok"
        chunk = receiver.mic_hal.get_buffer(100)
        assert chunk.sample_rate == 16000

    def test_handle_location_fires_callback(self):
        """_handle_location() invokes location_callback with data dict."""
        received = []
        receiver = self._make_receiver()
        receiver.location_callback = received.append

        payload = iOSSensorPayload(
            device_id=DEVICE_ID,
            sensor_type="location",
            timestamp=1709500000.0,
            data={"latitude": 37.77, "longitude": -122.41},
            format_version=FORMAT_VERSION,
        )
        result = receiver._handle_location(payload)

        assert result["status"] == "ok"
        assert len(received) == 1
        assert received[0]["latitude"] == 37.77

    def test_handle_battery_fires_callback(self):
        """_handle_battery() invokes battery_callback with data dict."""
        received = []
        receiver = self._make_receiver()
        receiver.battery_callback = received.append

        payload = iOSSensorPayload(
            device_id=DEVICE_ID,
            sensor_type="battery",
            timestamp=1709500000.0,
            data={"level": 0.82, "state": "unplugged"},
            format_version=FORMAT_VERSION,
        )
        result = receiver._handle_battery(payload)

        assert result["status"] == "ok"
        assert received[0]["level"] == 0.82


# ── Test 7: Profile registration in profiles __init__ ─────────────────────────


class TestProfileRegistration:
    def test_ios_companion_profile_in_native_registry(self):
        """'ios-companion' is registered in _NATIVE_PROFILES."""
        from openclaw_embodiment.profiles import _NATIVE_PROFILES
        assert "ios-companion" in _NATIVE_PROFILES

    def test_load_profile_returns_ios_companion_dict(self):
        """load_profile('ios-companion') returns a dict with expected keys."""
        from openclaw_embodiment.profiles import load_profile
        config = load_profile("ios-companion")
        assert config["name"] == "ios-companion"
        assert "capabilities" in config
        assert "receiver_port" in config
        assert config["receiver_port"] == COMPANION_PORT

    def test_ios_companion_profile_exported_from_top_level(self):
        """iOSCompanionProfile, iOSCompanionReceiver, iOSSensorPayload are in top-level __all__."""
        import openclaw_embodiment
        assert "iOSCompanionProfile" in openclaw_embodiment.__all__
        assert "iOSCompanionReceiver" in openclaw_embodiment.__all__
        assert "iOSSensorPayload" in openclaw_embodiment.__all__

    def test_ios_companion_profile_importable_from_top_level(self):
        """Can import iOS companion types directly from openclaw_embodiment."""
        from openclaw_embodiment import iOSCompanionProfile, iOSCompanionReceiver, iOSSensorPayload
        assert iOSCompanionProfile is not None
        assert iOSCompanionReceiver is not None
        assert iOSSensorPayload is not None

    def test_module_profile_constant_is_ios_companion_profile(self):
        """The PROFILE module constant is an iOSCompanionProfile instance."""
        assert isinstance(PROFILE, iOSCompanionProfile)
        assert PROFILE.name == "ios-companion"
        assert "camera" in PROFILE.capabilities
        assert "imu" in PROFILE.capabilities
        assert "microphone" in PROFILE.capabilities

    def test_protocol_spec_has_all_sensor_examples(self):
        """CompanionProtocolSpec.schema_for() returns dicts for all main sensor types."""
        for sensor_type in ("imu", "camera", "audio", "location"):
            schema = CompanionProtocolSpec.schema_for(sensor_type)
            assert schema["sensor_type"] == sensor_type
            assert "data" in schema

    def test_protocol_spec_raises_for_unknown_sensor(self):
        """CompanionProtocolSpec.schema_for() raises ValueError for unknown types."""
        with pytest.raises(ValueError, match="No schema"):
            CompanionProtocolSpec.schema_for("lidar")
