"""Tests for Context Engine v0.3 - SensorContext, ContextBuilder, DeviceCapabilityVector.

Coverage:
1. SensorContext with all fields populated
2. SensorContext with only audio (graceful degradation)
3. SensorContext with only proximity
4. SensorContext with no sensors at all
5. awareness_level calculation -- single sensor = ~0.25, all sensors = ~1.0
6. Conflict detection -- audio says 3 speakers, visual says 1 person
7. Conflict penalty -- awareness_level reduced when conflicts present
8. summary generation -- each variant produces non-empty, honest string
9. DeviceCapabilityVector predefined profiles
10. ContextBuilder.build() end-to-end
"""

from __future__ import annotations

import pytest

from openclaw_embodiment.context import (
    DISTILLER_CM5,
    EVEN_G2,
    IPHONE,
    LIMITLESS_PENDANT,
    RASPBERRY_PI,
    REACHY2,
    AudioContext,
    ContextBuilder,
    DeviceCapabilityVector,
    MotionContext,
    ProximityContext,
    SensorContext,
    VisualContext,
)


class TestSensorContextAllFields:
    """Test 1: SensorContext with all fields populated."""
    
    def test_all_fields_populated(self) -> None:
        """SensorContext can be created with all sensor channels."""
        audio = AudioContext(
            transcript="Hello world",
            speaker_count=2,
            ambient_class="speech",
            rms_level=0.5,
            language="en",
            confidence=0.9,
        )
        visual = VisualContext(
            description="Office environment",
            person_count=2,
            activity="sitting",
            lighting="bright",
            frame_path="/tmp/frame.jpg",
            confidence=0.85,
        )
        motion = MotionContext(
            state="stationary",
            orientation=(0.1, 0.2, 0.3),
            acceleration=(0.0, 0.0, 9.8),
            confidence=0.95,
        )
        proximity = ProximityContext(
            known_devices=["phone-alice", "watch-bob"],
            unknown_count=1,
            rssi_map={"phone-alice": -45, "watch-bob": -60},
            confidence=0.8,
        )
        
        context = SensorContext(
            timestamp_ms=1709500000000,
            device_id="test-device-001",
            trigger="voice_detected",
            audio=audio,
            visual=visual,
            motion=motion,
            proximity=proximity,
            awareness_level=0.9,
            conflicts=[],
            summary="All sensors active.",
            device_capabilities=DISTILLER_CM5,
        )
        
        assert context.timestamp_ms == 1709500000000
        assert context.device_id == "test-device-001"
        assert context.trigger == "voice_detected"
        assert context.audio is not None
        assert context.audio.speaker_count == 2
        assert context.visual is not None
        assert context.visual.person_count == 2
        assert context.motion is not None
        assert context.motion.state == "stationary"
        assert context.proximity is not None
        assert len(context.proximity.known_devices) == 2
        assert context.awareness_level == 0.9
        assert context.conflicts == []
        assert context.device_capabilities.has_camera is True


class TestGracefulDegradation:
    """Tests 2-4: SensorContext with partial or no sensor data."""
    
    def test_audio_only(self) -> None:
        """Test 2: SensorContext with only audio sensor."""
        audio = AudioContext(
            transcript="Testing one two three",
            speaker_count=1,
            ambient_class="speech",
            rms_level=0.3,
            language="en",
            confidence=0.88,
        )
        
        context = SensorContext(
            timestamp_ms=1709500000000,
            device_id="audio-only-device",
            trigger="voice_detected",
            audio=audio,
            visual=None,
            motion=None,
            proximity=None,
            awareness_level=0.25,
            conflicts=[],
            summary="Audio only.",
            device_capabilities=LIMITLESS_PENDANT,
        )
        
        assert context.audio is not None
        assert context.visual is None
        assert context.motion is None
        assert context.proximity is None
        assert context.audio.transcript == "Testing one two three"
    
    def test_proximity_only(self) -> None:
        """Test 3: SensorContext with only proximity sensor."""
        proximity = ProximityContext(
            known_devices=["beacon-001"],
            unknown_count=3,
            rssi_map={"beacon-001": -50},
            confidence=0.75,
        )
        
        context = SensorContext(
            timestamp_ms=1709500000000,
            device_id="ble-scanner",
            trigger="ble_new_device",
            audio=None,
            visual=None,
            motion=None,
            proximity=proximity,
            awareness_level=0.25,
            conflicts=[],
            summary="BLE only.",
            device_capabilities=DeviceCapabilityVector(has_ble=True),
        )
        
        assert context.audio is None
        assert context.visual is None
        assert context.motion is None
        assert context.proximity is not None
        assert context.proximity.unknown_count == 3
    
    def test_no_sensors(self) -> None:
        """Test 4: SensorContext with no sensors at all."""
        context = SensorContext(
            timestamp_ms=1709500000000,
            device_id="minimal-device",
            trigger="scheduled",
            audio=None,
            visual=None,
            motion=None,
            proximity=None,
            awareness_level=0.0,
            conflicts=[],
            summary="No sensor data.",
            device_capabilities=DeviceCapabilityVector(),
        )
        
        assert context.audio is None
        assert context.visual is None
        assert context.motion is None
        assert context.proximity is None
        assert context.awareness_level == 0.0


class TestAwarenessLevelCalculation:
    
    def test_single_sensor_awareness(self) -> None:
        """Single sensor should yield approximately 0.25 awareness."""
        builder = ContextBuilder(device_id="test", capabilities=DISTILLER_CM5)
        
        audio = AudioContext(
            transcript=None,
            speaker_count=1,
            ambient_class="speech",
            rms_level=0.4,
            language=None,
            confidence=0.8,
        )
        
        context = builder.build(trigger="voice_detected", audio=audio)
        
        assert 0.20 <= context.awareness_level <= 0.35
    
    def test_all_sensors_awareness(self) -> None:
        """All sensors should yield approximately 1.0 awareness."""
        builder = ContextBuilder(device_id="test", capabilities=IPHONE)
        
        audio = AudioContext(
            transcript="Hello",
            speaker_count=1,
            ambient_class="speech",
            rms_level=0.4,
            language="en",
            confidence=0.9,
        )
        visual = VisualContext(
            description="Person at desk",
            person_count=1,
            activity="sitting",
            lighting="bright",
            frame_path=None,
            confidence=0.85,
        )
        motion = MotionContext(
            state="stationary",
            orientation=None,
            acceleration=None,
            confidence=0.9,
        )
        proximity = ProximityContext(
            known_devices=["laptop"],
            unknown_count=0,
            rssi_map={"laptop": -30},
            confidence=0.95,
        )
        
        context = builder.build(
            trigger="voice_detected",
            audio=audio,
            visual=visual,
            motion=motion,
            proximity=proximity,
        )
        
        # 4 sensors = 1.0 base + potential corroboration bonus
        assert context.awareness_level >= 0.95


class TestConflictDetection:
    
    def test_speaker_person_conflict(self) -> None:
        """Test 6: audio says 3 speakers, visual says 1 person -> conflict."""
        builder = ContextBuilder(device_id="test", capabilities=DISTILLER_CM5)
        
        audio = AudioContext(
            transcript=None,
            speaker_count=3,
            ambient_class="speech",
            rms_level=0.5,
            language=None,
            confidence=0.8,
        )
        visual = VisualContext(
            description="One person",
            person_count=1,
            activity="talking",
            lighting="bright",
            frame_path=None,
            confidence=0.9,
        )
        
        context = builder.build(
            trigger="voice_detected",
            audio=audio,
            visual=visual,
        )
        
        assert len(context.conflicts) > 0
        # Check conflict message contains relevant info
        conflict_str = context.conflicts[0]
        assert "3" in conflict_str and "1" in conflict_str
    
    def test_motion_audio_conflict(self) -> None:
        """Motion=running + audio=silence is suspicious."""
        builder = ContextBuilder(device_id="test", capabilities=IPHONE)
        
        audio = AudioContext(
            transcript=None,
            speaker_count=0,
            ambient_class="silence",
            rms_level=0.01,
            language=None,
            confidence=0.9,
        )
        motion = MotionContext(
            state="running",
            orientation=None,
            acceleration=(0.5, 0.8, 10.2),
            confidence=0.95,
        )
        
        context = builder.build(
            trigger="motion",
            audio=audio,
            motion=motion,
        )
        
        assert len(context.conflicts) > 0
        # Should mention motion and silence
        conflict_str = " ".join(context.conflicts)
        assert "running" in conflict_str or "silence" in conflict_str
    
    def test_conflict_penalty(self) -> None:
        """Test 7: awareness_level reduced when conflicts present."""
        builder = ContextBuilder(device_id="test", capabilities=DISTILLER_CM5)
        
        # First: no conflict scenario
        audio_match = AudioContext(
            transcript=None,
            speaker_count=2,
            ambient_class="speech",
            rms_level=0.5,
            language=None,
            confidence=0.8,
        )
        visual_match = VisualContext(
            description="Two people",
            person_count=2,
            activity="talking",
            lighting="bright",
            frame_path=None,
            confidence=0.9,
        )
        
        context_no_conflict = builder.build(
            trigger="voice_detected",
            audio=audio_match,
            visual=visual_match,
        )
        
        # Now: conflict scenario
        audio_conflict = AudioContext(
            transcript=None,
            speaker_count=5,
            ambient_class="speech",
            rms_level=0.5,
            language=None,
            confidence=0.8,
        )
        visual_conflict = VisualContext(
            description="One person",
            person_count=1,
            activity="talking",
            lighting="bright",
            frame_path=None,
            confidence=0.9,
        )
        
        context_with_conflict = builder.build(
            trigger="voice_detected",
            audio=audio_conflict,
            visual=visual_conflict,
        )
        
        # Conflict version should have lower awareness
        assert context_with_conflict.awareness_level < context_no_conflict.awareness_level
        assert len(context_with_conflict.conflicts) > 0
        assert len(context_no_conflict.conflicts) == 0


class TestSummaryGeneration:
    """Test 8: summary generation produces non-empty, honest strings."""
    
    def test_speech_detected_summary(self) -> None:
        """Summary for speech detection is meaningful."""
        builder = ContextBuilder(device_id="test", capabilities=DISTILLER_CM5)
        
        audio = AudioContext(
            transcript="Hello there",
            speaker_count=1,
            ambient_class="speech",
            rms_level=0.4,
            language="en",
            confidence=0.9,
        )
        
        context = builder.build(trigger="voice_detected", audio=audio)
        
        assert len(context.summary) > 0
        assert "Speech detected" in context.summary or "speaking" in context.summary
    
    def test_motion_only_summary(self) -> None:
        """Summary for motion-only capture is meaningful."""
        builder = ContextBuilder(device_id="test", capabilities=EVEN_G2)
        
        motion = MotionContext(
            state="walking",
            orientation=(45.0, 10.0, 0.0),
            acceleration=(0.2, 0.1, 9.9),
            confidence=0.85,
        )
        
        context = builder.build(trigger="motion", motion=motion)
        
        assert len(context.summary) > 0
        assert "Motion" in context.summary or "Walking" in context.summary
    
    def test_no_sensor_summary(self) -> None:
        """Summary for no-sensor capture is honest about limitations."""
        builder = ContextBuilder(device_id="test", capabilities=DeviceCapabilityVector())
        
        context = builder.build(trigger="scheduled")
        
        assert len(context.summary) > 0
        assert "No sensor data" in context.summary or "scheduled" in context.summary.lower()
    
    def test_conflict_mentioned_in_summary(self) -> None:
        """Summary mentions conflicts when present."""
        builder = ContextBuilder(device_id="test", capabilities=DISTILLER_CM5)
        
        audio = AudioContext(
            transcript=None,
            speaker_count=4,
            ambient_class="speech",
            rms_level=0.5,
            language=None,
            confidence=0.8,
        )
        visual = VisualContext(
            description="Empty room",
            person_count=0,
            activity=None,
            lighting="bright",
            frame_path=None,
            confidence=0.9,
        )
        
        context = builder.build(
            trigger="voice_detected",
            audio=audio,
            visual=visual,
        )
        
        assert len(context.summary) > 0
        # Should mention conflict or warning
        assert "conflict" in context.summary.lower() or "warning" in context.summary.lower()
    
    def test_low_awareness_warning(self) -> None:
        """Summary warns when awareness is low."""
        builder = ContextBuilder(device_id="test", capabilities=DeviceCapabilityVector())
        
        # No sensors = 0 awareness
        context = builder.build(trigger="scheduled")
        
        assert "confidence" in context.summary.lower() or "No sensor data" in context.summary


class TestDeviceCapabilityProfiles:
    """Test 9: DeviceCapabilityVector predefined profiles."""
    
    def test_distiller_cm5_profile(self) -> None:
        """DISTILLER_CM5 has correct capabilities."""
        assert DISTILLER_CM5.has_microphone is True
        assert DISTILLER_CM5.has_camera is True
        assert DISTILLER_CM5.has_imu is False
        assert DISTILLER_CM5.has_ble is True
        assert DISTILLER_CM5.has_display is True
        assert DISTILLER_CM5.has_speaker is True
        assert DISTILLER_CM5.is_wearable is False
    
    def test_even_g2_profile(self) -> None:
        """EVEN_G2 has correct capabilities (wearable glasses)."""
        assert EVEN_G2.has_microphone is False
        assert EVEN_G2.has_camera is False
        assert EVEN_G2.has_imu is True
        assert EVEN_G2.has_ble is True
        assert EVEN_G2.has_display is True
        assert EVEN_G2.has_speaker is True
        assert EVEN_G2.is_wearable is True
    
    def test_limitless_pendant_profile(self) -> None:
        """LIMITLESS_PENDANT has correct capabilities."""
        assert LIMITLESS_PENDANT.has_microphone is True
        assert LIMITLESS_PENDANT.has_camera is False
        assert LIMITLESS_PENDANT.has_imu is True
        assert LIMITLESS_PENDANT.has_ble is True
        assert LIMITLESS_PENDANT.has_display is False
        assert LIMITLESS_PENDANT.has_speaker is False
        assert LIMITLESS_PENDANT.is_wearable is True
    
    def test_iphone_profile(self) -> None:
        """IPHONE has all capabilities."""
        assert IPHONE.has_microphone is True
        assert IPHONE.has_camera is True
        assert IPHONE.has_imu is True
        assert IPHONE.has_ble is True
        assert IPHONE.has_display is True
        assert IPHONE.has_speaker is True
        assert IPHONE.has_gps is True
        assert IPHONE.is_wearable is True
    
    def test_reachy2_profile(self) -> None:
        """REACHY2 robot has correct capabilities."""
        assert REACHY2.has_microphone is True
        assert REACHY2.has_camera is True
        assert REACHY2.has_imu is True
        assert REACHY2.has_display is False
        assert REACHY2.has_speaker is True
        assert REACHY2.is_wearable is False
    
    def test_raspberry_pi_profile(self) -> None:
        """RASPBERRY_PI has correct capabilities."""
        assert RASPBERRY_PI.has_microphone is True
        assert RASPBERRY_PI.has_camera is True
        assert RASPBERRY_PI.has_imu is False
        assert RASPBERRY_PI.has_ble is True
        assert RASPBERRY_PI.has_display is False
        assert RASPBERRY_PI.has_speaker is True
        assert RASPBERRY_PI.is_wearable is False


class TestContextBuilderEndToEnd:
    """Test 10: ContextBuilder.build() end-to-end."""
    
    def test_full_context_build(self) -> None:
        """ContextBuilder produces valid SensorContext with all inputs."""
        builder = ContextBuilder(
            device_id="distiller-3aff",
            capabilities=DISTILLER_CM5,
        )
        
        audio = AudioContext(
            transcript="Schedule a meeting for tomorrow",
            speaker_count=1,
            ambient_class="speech",
            rms_level=0.35,
            language="en",
            confidence=0.92,
        )
        visual = VisualContext(
            description="Person at desk with laptop",
            person_count=1,
            activity="sitting",
            lighting="bright",
            frame_path="/tmp/capture_001.jpg",
            confidence=0.88,
        )
        motion = MotionContext(
            state="stationary",
            orientation=(0.0, 5.0, 0.0),
            acceleration=(0.0, 0.0, 9.81),
            confidence=0.99,
        )
        proximity = ProximityContext(
            known_devices=["iphone-mike", "macbook-mike"],
            unknown_count=0,
            rssi_map={"iphone-mike": -40, "macbook-mike": -25},
            confidence=0.95,
        )
        
        context = builder.build(
            trigger="voice_detected",
            audio=audio,
            visual=visual,
            motion=motion,
            proximity=proximity,
            timestamp_ms=1709510000000,
        )
        
        # Verify structure
        assert isinstance(context, SensorContext)
        assert context.device_id == "distiller-3aff"
        assert context.trigger == "voice_detected"
        assert context.timestamp_ms == 1709510000000
        
        # Verify sensor data passed through
        assert context.audio is audio
        assert context.visual is visual
        assert context.motion is motion
        assert context.proximity is proximity
        
        # Verify computed fields
        assert 0.0 <= context.awareness_level <= 1.0
        assert isinstance(context.conflicts, list)
        assert isinstance(context.summary, str)
        assert len(context.summary) > 0
        
        # Verify capabilities
        assert context.device_capabilities is DISTILLER_CM5
    
    def test_minimal_context_build(self) -> None:
        """ContextBuilder handles minimal input gracefully."""
        builder = ContextBuilder(
            device_id="minimal-001",
            capabilities=DeviceCapabilityVector(),
        )
        
        context = builder.build(trigger="scheduled")
        
        assert isinstance(context, SensorContext)
        assert context.device_id == "minimal-001"
        assert context.trigger == "scheduled"
        assert context.audio is None
        assert context.visual is None
        assert context.motion is None
        assert context.proximity is None
        assert context.awareness_level == 0.0
        assert len(context.summary) > 0
    
    def test_timestamp_auto_generated(self) -> None:
        """ContextBuilder auto-generates timestamp if not provided."""
        builder = ContextBuilder(device_id="test", capabilities=DISTILLER_CM5)
        
        context = builder.build(trigger="manual")
        
        assert context.timestamp_ms > 0
        # Should be recent (within last minute)
        import time
        now_ms = int(time.time() * 1000)
        assert abs(context.timestamp_ms - now_ms) < 60000
    
    def test_builder_with_custom_capabilities(self) -> None:
        """ContextBuilder accepts custom DeviceCapabilityVector."""
        custom_caps = DeviceCapabilityVector(
            has_microphone=True,
            has_camera=False,
            has_imu=True,
            has_ble=True,
            has_display=False,
            has_speaker=True,
            has_gps=True,
            is_wearable=True,
            has_gaze=True,
        )
        
        builder = ContextBuilder(
            device_id="custom-wearable",
            capabilities=custom_caps,
        )
        
        context = builder.build(trigger="manual")
        
        assert context.device_capabilities.has_gaze is True
        assert context.device_capabilities.has_gps is True
        assert context.device_capabilities.has_camera is False


class TestCorroborationBonus:
    """Additional tests for sensor corroboration."""
    
    def test_corroboration_increases_awareness(self) -> None:
        """When sensors agree, awareness should be higher."""
        builder = ContextBuilder(device_id="test", capabilities=IPHONE)
        
        # Sensors that corroborate: speech + 1 person speaking + 1 person visible
        audio = AudioContext(
            transcript="Hello",
            speaker_count=1,
            ambient_class="speech",
            rms_level=0.4,
            language="en",
            confidence=0.9,
        )
        visual = VisualContext(
            description="Person speaking",
            person_count=1,
            activity="talking",
            lighting="bright",
            frame_path=None,
            confidence=0.9,
        )
        
        context = builder.build(
            trigger="voice_detected",
            audio=audio,
            visual=visual,
        )
        
        # Base would be 0.5 (2 sensors), should have corroboration bonus
        assert context.awareness_level >= 0.55  # At least some bonus
        assert len(context.conflicts) == 0
