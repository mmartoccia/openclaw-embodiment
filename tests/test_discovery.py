"""Tests for the spatial discovery system.

Tests:
    TestWorldModel          -- entity creation, decay, last_known demotion
    TestAnomalyDetector     -- anomaly firing on correct conditions
    TestSpaceModel          -- SQLite round-trip, upsert, get_space_summary
    TestDiscoveryReport     -- serialization, build_report factory

Run:
    cd /path/to/openclaw-wearable-sdk
    python -m pytest tests/test_discovery.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

# Ensure we can import from the SDK root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openclaw_embodiment.core.context_builder import (
    AudioContext, VisualContext, ProximityContext,
    SensorContext, DISTILLER_CM5_CAPABILITIES,
)
from openclaw_embodiment.discovery.world_model import (
    WorldModel, EntityState, HALF_LIVES, CONFIDENCE_FLOOR,
)
from openclaw_embodiment.discovery.space_model import SpaceModel
from openclaw_embodiment.discovery.anomaly_detector import (
    AnomalyDetector, AnomalyEvent, AnomalyType,
)
from openclaw_embodiment.discovery.discovery_report import (
    DiscoveryReport, build_report,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def make_sensor_context(
    audio_class: str = "silence",
    audio_rms: float = 100.0,
    audio_conf: float = 0.5,
    person_count: int = 0,
    lighting: str = "bright",
    visual_conf: float = 0.7,
    rssi_map: dict = None,
    known_devices: list = None,
    unknown_count: int = 0,
    awareness: float = 0.5,
    trigger: str = "scheduled",
) -> SensorContext:
    audio = AudioContext(
        ambient_class=audio_class,
        rms_level=audio_rms,
        confidence=audio_conf,
    )
    visual = VisualContext(
        person_count=person_count,
        lighting=lighting,
        confidence=visual_conf,
    )
    proximity = ProximityContext(
        known_devices=known_devices or [],
        unknown_count=unknown_count,
        rssi_map=rssi_map or {},
        confidence=0.8 if rssi_map else 0.0,
    )
    return SensorContext(
        timestamp_ms=int(time.time() * 1000),
        device_id="test-device",
        trigger=trigger,
        audio=audio,
        visual=visual,
        motion=None,
        proximity=proximity,
        awareness_level=awareness,
        conflicts=[],
        summary="Test context.",
        device_capabilities=DISTILLER_CM5_CAPABILITIES,
    )


def make_proximity(macs: list = None, known_names: list = None) -> object:
    """Create a BLE ProximityContext-like object with rssi_map."""
    from openclaw_embodiment.hal.ble_scanner import ProximityContext as BLECtx
    rssi_map = {mac: -70 for mac in (macs or [])}
    return BLECtx(
        known_devices=known_names or [],
        unknown_count=len(macs or []) - len(known_names or []),
        rssi_map=rssi_map,
        scan_duration_s=5.0,
        confidence=0.9,
        timestamp_ms=int(time.time() * 1000),
    )


# ------------------------------------------------------------------
# TestWorldModel
# ------------------------------------------------------------------

class TestWorldModel(unittest.TestCase):

    def setUp(self):
        self.wm = WorldModel(device_id="test")

    def test_initial_state(self):
        state = self.wm.get_current_state()
        self.assertEqual(state["active_entities"], [])
        self.assertEqual(state["last_known_entities"], [])
        self.assertEqual(state["history_depth"], 0)

    def test_entity_created_on_update(self):
        ctx = make_sensor_context(audio_class="speech", audio_rms=2000, audio_conf=0.8)
        self.wm.update(ctx)
        self.assertIn("person:audio:0", self.wm.entity_states)
        self.assertIn("ambient:audio", self.wm.entity_states)

    def test_ble_entity_created(self):
        ctx = make_sensor_context(
            rssi_map={"aa:bb:cc:dd:ee:ff": -70},
            known_devices=[],
            unknown_count=1,
        )
        self.wm.update(ctx)
        ble_ids = [e for e in self.wm.entity_states if e.startswith("ble:")]
        self.assertGreater(len(ble_ids), 0)

    def test_entity_confidence_decays(self):
        entity = EntityState(
            entity_id="test:entity",
            entity_type="person",
            last_seen=int(time.time() * 1000) - 60_000,  # 60s ago
            last_context="test",
            confidence=1.0,
            source_sensors=["audio"],
            half_life_key="person_audio",
            initial_confidence=1.0,
        )
        # After 60s, person_audio half_life=120s, should be ~0.5
        decayed = entity.current_confidence()
        self.assertAlmostEqual(decayed, 0.5, delta=0.05)

    def test_entity_fully_decays_to_zero(self):
        entity = EntityState(
            entity_id="test:entity",
            entity_type="person",
            last_seen=int(time.time() * 1000) - 200_000,  # 200s ago
            last_context="test",
            confidence=1.0,
            source_sensors=["audio"],
            half_life_key="person_audio",  # half_life=120s
            initial_confidence=1.0,
        )
        # 200s > 120s half_life → should be 0.0
        decayed = entity.current_confidence()
        self.assertEqual(decayed, 0.0)

    def test_entity_demoted_to_last_known(self):
        # Insert an entity that's about to decay below floor
        entity = EntityState(
            entity_id="ble:olddevice",
            entity_type="device",
            last_seen=int(time.time() * 1000) - 200_000,
            last_context="old scan",
            confidence=0.05,
            source_sensors=["ble"],
            half_life_key="ble_scan",  # half_life=30s -- totally decayed by 200s
            initial_confidence=0.9,
        )
        self.wm.entity_states["ble:olddevice"] = entity

        # Trigger decay by updating with fresh data
        ctx = make_sensor_context()
        self.wm.update(ctx)

        self.assertNotIn("ble:olddevice", self.wm.entity_states)
        self.assertIn("ble:olddevice", self.wm.last_known)

    def test_entity_promoted_from_last_known(self):
        # Manually place entity in last_known
        entity = EntityState(
            entity_id="person:audio:0",
            entity_type="person",
            last_seen=int(time.time() * 1000) - 200_000,
            last_context="old",
            confidence=0.05,
            source_sensors=["audio"],
            half_life_key="person_audio",
            initial_confidence=0.05,
        )
        self.wm.last_known["person:audio:0"] = entity

        # Update with speech -- should promote
        ctx = make_sensor_context(audio_class="speech", audio_rms=2000, audio_conf=0.8)
        self.wm.update(ctx)

        self.assertIn("person:audio:0", self.wm.entity_states)
        self.assertNotIn("person:audio:0", self.wm.last_known)

    def test_narrative_with_speech(self):
        ctx = make_sensor_context(audio_class="speech", audio_rms=2000, audio_conf=0.8)
        self.wm.update(ctx)
        narrative = self.wm.get_narrative()
        self.assertIn("Speech detected", narrative)

    def test_narrative_no_person(self):
        ctx = make_sensor_context(audio_class="silence")
        self.wm.update(ctx)
        narrative = self.wm.get_narrative()
        self.assertIn("No person detected", narrative)

    def test_get_current_state_serializable(self):
        ctx = make_sensor_context()
        self.wm.update(ctx)
        import json
        state = self.wm.get_current_state()
        # Should be serializable without error
        serialized = json.dumps(state)
        self.assertIsInstance(serialized, str)

    def test_history_bounded(self):
        for i in range(10):
            self.wm.update(make_sensor_context())
        self.assertLessEqual(len(self.wm.history), self.wm.history_depth)

    def test_is_speech_active(self):
        ctx = make_sensor_context(audio_class="speech", audio_conf=0.9)
        self.wm.update(ctx)
        # Only true if the person entity has high confidence
        # Right after update it should be active
        entity = self.wm.entity_states.get("person:audio:0")
        if entity:
            self.assertGreaterEqual(entity.confidence, CONFIDENCE_FLOOR)


# ------------------------------------------------------------------
# TestAnomalyDetector
# ------------------------------------------------------------------

class TestAnomalyDetector(unittest.TestCase):

    def setUp(self):
        self.detected: list = []
        self.detector = AnomalyDetector(
            on_anomaly=self.detected.extend,
            scene_change_threshold=0.3,
            min_baseline_scans=0,  # disable baseline gate for tests
        )
        # Create a temp SpaceModel
        self.tmpdb = tempfile.mktemp(suffix=".db")
        self.space = SpaceModel(db_path=self.tmpdb, device_id="test")
        self.world = WorldModel(device_id="test")

    def tearDown(self):
        if os.path.exists(self.tmpdb):
            os.unlink(self.tmpdb)

    def test_new_unknown_device_fires(self):
        prox = make_proximity(macs=["aa:bb:cc:dd:ee:01"])
        ctx = make_sensor_context(rssi_map={"aa:bb:cc:dd:ee:01": -70})
        self.world.update(ctx)

        anomalies = self.detector.check(ctx, self.world, self.space)

        types = [a.anomaly_type for a in anomalies]
        self.assertIn(AnomalyType.NEW_UNKNOWN_DEVICE, types)

    def test_known_device_appeared_fires(self):
        mac = "aa:bb:cc:dd:ee:02"
        # Pre-populate SpaceModel so device is "known"
        self.space._scan_count = 5
        with self.space._conn() as conn:
            conn.execute("""
                INSERT INTO ble_devices (mac, name, first_seen, last_seen, last_rssi,
                    total_observations, scans_present, is_infrastructure)
                VALUES (?, ?, ?, ?, ?, 3, 3, 0)
            """, (mac, "TestDevice", int(time.time()*1000)-10000,
                  int(time.time()*1000), -70))

        self.detector._sync_known_macs(self.space)
        # Previous scan had no devices
        self.detector._previous_macs = set()

        ctx = make_sensor_context(rssi_map={mac: -70})
        self.world.update(ctx)
        anomalies = self.detector.check(ctx, self.world, self.space)

        types = [a.anomaly_type for a in anomalies]
        self.assertIn(AnomalyType.KNOWN_DEVICE_APPEARED, types)

    def test_known_device_departed_fires(self):
        mac = "aa:bb:cc:dd:ee:03"
        # Pre-populate SpaceModel
        with self.space._conn() as conn:
            conn.execute("""
                INSERT INTO ble_devices (mac, name, first_seen, last_seen, last_rssi,
                    total_observations, scans_present, is_infrastructure)
                VALUES (?, ?, ?, ?, ?, 5, 5, 0)
            """, (mac, "DepartedDevice", int(time.time()*1000)-20000,
                  int(time.time()*1000)-5000, -72))

        self.detector._sync_known_macs(self.space)
        # Previous scan had this device
        self.detector._previous_macs = {mac}

        # Current scan does NOT have this device
        ctx = make_sensor_context(rssi_map={})
        self.world.update(ctx)
        anomalies = self.detector.check(ctx, self.world, self.space)

        types = [a.anomaly_type for a in anomalies]
        self.assertIn(AnomalyType.KNOWN_DEVICE_DEPARTED, types)

    def test_speech_in_empty_space_fires(self):
        # No non-infra BLE devices, but speech detected
        self.detector._infra_macs = set()
        self.detector._known_macs = set()

        ctx = make_sensor_context(
            audio_class="speech",
            audio_rms=3000,
            audio_conf=0.85,
            rssi_map={},  # no BLE devices
        )
        self.world.update(ctx)
        anomalies = self.detector.check(ctx, self.world, self.space)

        types = [a.anomaly_type for a in anomalies]
        self.assertIn(AnomalyType.SPEECH_IN_EMPTY_SPACE, types)

    def test_speech_not_anomalous_with_devices(self):
        # Speech with non-infra BLE devices present -- not anomalous
        mac = "bb:cc:dd:ee:ff:01"
        self.detector._infra_macs = set()
        self.detector._known_macs = set()

        ctx = make_sensor_context(
            audio_class="speech",
            audio_rms=3000,
            audio_conf=0.85,
            rssi_map={mac: -70},
            unknown_count=1,
        )
        self.world.update(ctx)
        anomalies = self.detector.check(ctx, self.world, self.space)

        speech_anomalies = [a for a in anomalies if a.anomaly_type == AnomalyType.SPEECH_IN_EMPTY_SPACE]
        self.assertEqual(len(speech_anomalies), 0)

    def test_scene_change_fires(self):
        # Insert a scene snapshot with high diff score
        with self.space._conn() as conn:
            conn.execute("""
                INSERT INTO scene_snapshots
                    (timestamp, lighting_level, mean_pixel_value,
                     diff_score_from_baseline, is_baseline)
                VALUES (?, ?, ?, ?, ?)
            """, (int(time.time()*1000), "bright", 180.0, 0.45, 0))

        ctx = make_sensor_context(lighting="bright", visual_conf=0.7)
        self.world.update(ctx)
        anomalies = self.detector.check(ctx, self.world, self.space)

        types = [a.anomaly_type for a in anomalies]
        self.assertIn(AnomalyType.SCENE_CHANGE, types)

    def test_scene_change_below_threshold_no_fire(self):
        with self.space._conn() as conn:
            conn.execute("""
                INSERT INTO scene_snapshots
                    (timestamp, lighting_level, mean_pixel_value,
                     diff_score_from_baseline, is_baseline)
                VALUES (?, ?, ?, ?, ?)
            """, (int(time.time()*1000), "bright", 128.0, 0.10, 0))

        ctx = make_sensor_context()
        self.world.update(ctx)
        anomalies = self.detector.check(ctx, self.world, self.space)

        types = [a.anomaly_type for a in anomalies]
        self.assertNotIn(AnomalyType.SCENE_CHANGE, types)

    def test_callback_fired(self):
        ctx = make_sensor_context(rssi_map={"aa:bb:cc:dd:ee:99": -70})
        self.world.update(ctx)
        self.detector.check(ctx, self.world, self.space)
        # Callback should have received the anomaly events
        self.assertGreater(len(self.detected), 0)

    def test_anomaly_event_serializable(self):
        import json
        event = AnomalyEvent(
            anomaly_type=AnomalyType.NEW_UNKNOWN_DEVICE,
            description="Test device appeared",
            confidence=0.85,
            sensor_data={"mac": "aa:bb:cc:dd:ee:ff", "rssi": -70},
        )
        serialized = json.dumps(event.to_dict())
        self.assertIn("NEW_UNKNOWN_DEVICE", serialized)


# ------------------------------------------------------------------
# TestSpaceModel
# ------------------------------------------------------------------

class TestSpaceModel(unittest.TestCase):

    def setUp(self):
        self.tmpdb = tempfile.mktemp(suffix=".db")
        self.space = SpaceModel(db_path=self.tmpdb, device_id="test")

    def tearDown(self):
        if os.path.exists(self.tmpdb):
            os.unlink(self.tmpdb)

    def test_db_created(self):
        self.assertTrue(os.path.exists(self.tmpdb))

    def test_ble_update_inserts_device(self):
        prox = make_proximity(macs=["aa:bb:cc:dd:ee:ff"])
        self.space.update_ble(prox)
        devices = self.space.get_all_ble_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["mac"], "aa:bb:cc:dd:ee:ff")

    def test_ble_update_upserts(self):
        prox = make_proximity(macs=["aa:bb:cc:dd:ee:ff"])
        self.space.update_ble(prox)
        self.space.update_ble(prox)
        devices = self.space.get_all_ble_devices()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["total_observations"], 2)

    def test_infrastructure_classification(self):
        prox = make_proximity(macs=["aa:bb:cc:dd:ee:ff"])
        # Run 10 scans with the same device
        for _ in range(10):
            self.space.update_ble(prox)
        devices = self.space.get_all_ble_devices()
        infra_devices = [d for d in devices if d["is_infrastructure"]]
        self.assertEqual(len(infra_devices), 1)

    def test_transient_device_not_infra(self):
        # First scan has device, subsequent scans don't
        prox_with = make_proximity(macs=["aa:bb:cc:dd:ee:ff"])
        prox_without = make_proximity(macs=[])
        self.space.update_ble(prox_with)
        for _ in range(9):
            self.space.update_ble(prox_without)
        devices = self.space.get_all_ble_devices()
        # Device appeared in 1/10 scans -- not infrastructure
        infra = [d for d in devices if d["is_infrastructure"]]
        self.assertEqual(len(infra), 0)

    def test_update_activity(self):
        audio = AudioContext(ambient_class="speech", rms_level=2000, confidence=0.8)
        self.space.update_activity(audio)
        summary = self.space.get_space_summary()
        self.assertEqual(summary["activity_patterns"]["total_windows"], 1)
        self.assertEqual(summary["activity_patterns"]["speech_windows"], 1)

    def test_update_scene(self):
        self.space.update_scene("bright", 180.0, 0.0, is_baseline=True)
        summary = self.space.get_space_summary()
        self.assertTrue(summary["scene_baseline"]["has_baseline"])
        self.assertEqual(summary["scene_baseline"]["baseline_lighting"], "bright")

    def test_log_discovery(self):
        self.space.log_discovery("TEST_EVENT", "Something happened", confidence=0.9)
        summary = self.space.get_space_summary()
        self.assertEqual(summary["discovery_log_count"], 1)

    def test_get_recent_discoveries(self):
        self.space.log_discovery("EVENT_A", "First event", 0.7)
        self.space.log_discovery("EVENT_B", "Second event", 0.8)
        recent = self.space.get_recent_discoveries(limit=5)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["event_type"], "EVENT_B")  # most recent first

    def test_narrative_no_data(self):
        narrative = self.space.get_narrative()
        self.assertIsInstance(narrative, str)
        self.assertGreater(len(narrative), 0)

    def test_narrative_with_data(self):
        # Add some devices and activity
        prox = make_proximity(macs=[f"aa:bb:cc:dd:ee:{i:02x}" for i in range(3)])
        for _ in range(5):
            self.space.update_ble(prox)
        audio = AudioContext(ambient_class="speech", rms_level=2000, confidence=0.8)
        for _ in range(10):
            self.space.update_activity(audio)
        narrative = self.space.get_narrative()
        self.assertIn("Speech activity", narrative)

    def test_space_summary_structure(self):
        summary = self.space.get_space_summary()
        self.assertIn("device_inventory", summary)
        self.assertIn("activity_patterns", summary)
        self.assertIn("scene_baseline", summary)
        self.assertIn("discovery_log_count", summary)


# ------------------------------------------------------------------
# TestDiscoveryReport
# ------------------------------------------------------------------

class TestDiscoveryReport(unittest.TestCase):

    def setUp(self):
        self.tmpdb = tempfile.mktemp(suffix=".db")
        self.space = SpaceModel(db_path=self.tmpdb, device_id="test")
        self.world = WorldModel(device_id="test")

        # Populate with some data
        ctx = make_sensor_context(audio_class="speech", audio_rms=2000, audio_conf=0.8)
        self.world.update(ctx)
        audio = AudioContext(ambient_class="speech", rms_level=2000, confidence=0.8)
        self.space.update_activity(audio)
        self.space.update_scene("bright", 150.0, 0.05, is_baseline=True)

    def tearDown(self):
        if os.path.exists(self.tmpdb):
            os.unlink(self.tmpdb)

    def test_build_report_returns_discovery_report(self):
        report = build_report(self.world, self.space)
        self.assertIsInstance(report, DiscoveryReport)

    def test_report_has_device_id(self):
        report = build_report(self.world, self.space)
        self.assertEqual(report.device_id, "test")

    def test_report_has_narrative(self):
        report = build_report(self.world, self.space)
        self.assertIsInstance(report.narrative, str)
        self.assertGreater(len(report.narrative), 0)

    def test_report_serializes_to_json(self):
        import json
        report = build_report(self.world, self.space)
        json_str = report.to_json()
        parsed = json.loads(json_str)
        self.assertIn("timestamp_ms", parsed)
        self.assertIn("device_id", parsed)
        self.assertIn("narrative", parsed)
        self.assertIn("sensor_health", parsed)
        self.assertIn("awareness_level", parsed)
        self.assertIn("confidence", parsed)

    def test_report_includes_anomalies(self):
        anomaly = AnomalyEvent(
            anomaly_type=AnomalyType.NEW_UNKNOWN_DEVICE,
            description="Test anomaly",
            confidence=0.9,
            sensor_data={"mac": "aa:bb:cc:dd:ee:ff"},
        )
        report = build_report(self.world, self.space, anomalies=[anomaly])
        self.assertEqual(len(report.anomalies), 1)
        self.assertEqual(report.anomalies[0]["anomaly_type"], "NEW_UNKNOWN_DEVICE")

    def test_report_session_duration(self):
        start_ms = int(time.time() * 1000) - 60_000  # 60s ago
        report = build_report(self.world, self.space, session_start_ms=start_ms)
        self.assertGreaterEqual(report.session_duration_s, 59)

    def test_report_save_to_file(self):
        tmpdir = tempfile.mkdtemp()
        report = build_report(self.world, self.space)
        filepath = report.save_to_file(output_dir=tmpdir)
        self.assertTrue(os.path.exists(filepath))
        self.assertTrue(filepath.endswith(".json"))
        # Verify it's valid JSON
        import json
        with open(filepath) as f:
            data = json.load(f)
        self.assertIn("timestamp_ms", data)

    def test_report_sensor_health_fields(self):
        report = build_report(
            self.world, self.space,
            sensor_health={"mic": "ok", "camera": "color_unreliable", "ble": "ok", "imu": "not_present"},
        )
        self.assertIn("mic", report.sensor_health)
        self.assertEqual(report.sensor_health["mic"], "ok")
        self.assertEqual(report.sensor_health["camera"], "color_unreliable")

    def test_report_awareness_from_history(self):
        history = [0.3, 0.5, 0.7, 0.6]
        report = build_report(self.world, self.space, awareness_history=history)
        expected = sum(history) / len(history)
        self.assertAlmostEqual(report.awareness_level, expected, places=3)

    def test_report_confidence_bounded(self):
        report = build_report(self.world, self.space)
        self.assertGreaterEqual(report.confidence, 0.0)
        self.assertLessEqual(report.confidence, 1.0)

    def test_report_to_dict_has_iso_timestamp(self):
        report = build_report(self.world, self.space)
        d = report.to_dict()
        self.assertIn("timestamp_iso", d)
        self.assertIn("Z", d["timestamp_iso"])


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
