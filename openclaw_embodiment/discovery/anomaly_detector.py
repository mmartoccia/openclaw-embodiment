"""AnomalyDetector -- Detects significant changes from established baseline.

Fires callbacks when anomaly conditions are met. Designed to be called after
each WorldModel update with the fresh sensor context.

Anomaly types:
    NEW_UNKNOWN_DEVICE       -- BLE device never seen before appeared
    KNOWN_DEVICE_APPEARED    -- recognized device came into range (person arrived)
    KNOWN_DEVICE_DEPARTED    -- recognized device left range (person left)
    SPEECH_IN_EMPTY_SPACE    -- audio detected speech when space was empty
    SCENE_CHANGE             -- camera diff score > 0.3 vs baseline
    UNUSUAL_HOUR_ACTIVITY    -- activity outside normal occupancy windows
    NEW_INFRASTRUCTURE_DEVICE -- device newly classified as always-present

Each anomaly is an AnomalyEvent dataclass.
Callbacks receive a list of AnomalyEvent objects.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Anomaly type enum
# ------------------------------------------------------------------

class AnomalyType(str, Enum):
    NEW_UNKNOWN_DEVICE = "NEW_UNKNOWN_DEVICE"
    KNOWN_DEVICE_APPEARED = "KNOWN_DEVICE_APPEARED"
    KNOWN_DEVICE_DEPARTED = "KNOWN_DEVICE_DEPARTED"
    SPEECH_IN_EMPTY_SPACE = "SPEECH_IN_EMPTY_SPACE"
    SCENE_CHANGE = "SCENE_CHANGE"
    UNUSUAL_HOUR_ACTIVITY = "UNUSUAL_HOUR_ACTIVITY"
    NEW_INFRASTRUCTURE_DEVICE = "NEW_INFRASTRUCTURE_DEVICE"


# ------------------------------------------------------------------
# AnomalyEvent
# ------------------------------------------------------------------

@dataclass
class AnomalyEvent:
    """A detected anomaly.

    Attributes:
        anomaly_type:  AnomalyType enum value
        description:   Human-readable description of what changed
        confidence:    0.0-1.0 confidence that this is a real anomaly
        timestamp_ms:  Unix ms when detected
        sensor_data:   Dict of relevant sensor readings at detection time
    """
    anomaly_type: AnomalyType
    description: str
    confidence: float
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    sensor_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anomaly_type": self.anomaly_type.value,
            "description": self.description,
            "confidence": self.confidence,
            "timestamp_ms": self.timestamp_ms,
            "sensor_data": self.sensor_data,
        }


# ------------------------------------------------------------------
# AnomalyDetector
# ------------------------------------------------------------------

class AnomalyDetector:
    """Detects anomalies by comparing current sensor state to established baseline.

    Args:
        on_anomaly:         Callback fired with list[AnomalyEvent] when anomalies detected.
                            Called synchronously in the detection thread.
        scene_change_threshold: Diff score above which a scene change is anomalous (default 0.3)
        unusual_hour_window:    Hours considered "normal" for occupancy. If None, learn from SpaceModel.
        min_baseline_scans:     Min BLE scans before anomaly detection is active (avoids false positives)
    """

    def __init__(
        self,
        on_anomaly: Optional[Callable[[List[AnomalyEvent]], None]] = None,
        scene_change_threshold: float = 0.3,
        unusual_hour_window: Optional[tuple] = None,  # e.g. (8, 22) for 8am-10pm
        min_baseline_scans: int = 3,
    ) -> None:
        self.on_anomaly = on_anomaly
        self.scene_change_threshold = scene_change_threshold
        self.unusual_hour_window = unusual_hour_window
        self.min_baseline_scans = min_baseline_scans

        # State tracking between checks
        self._known_macs: Set[str] = set()         # MACs ever seen in SpaceModel
        self._previous_macs: Set[str] = set()      # MACs seen in previous BLE scan
        self._infra_macs: Set[str] = set()         # MACs classified as infrastructure
        self._previous_scene_diff: float = 0.0
        self._initialized: bool = False
        self._check_count: int = 0

    def check(
        self,
        sensor_context,
        world_model,
        space_model,
    ) -> List[AnomalyEvent]:
        """Run anomaly checks against current state.

        Args:
            sensor_context: Most recent SensorContext from ContextBuilder.
            world_model:    Current WorldModel instance.
            space_model:    Current SpaceModel instance.

        Returns:
            List of AnomalyEvent objects detected this cycle (may be empty).
        """
        self._check_count += 1
        anomalies: List[AnomalyEvent] = []
        now_ms = int(time.time() * 1000)

        # Sync known MACs from SpaceModel
        self._sync_known_macs(space_model)

        # Gate: require minimum baseline before anomaly detection
        total_scans = getattr(space_model, "_scan_count", 0)
        if total_scans < self.min_baseline_scans:
            logger.debug(
                "[AnomalyDetector] Baseline not ready (%d/%d scans)",
                total_scans, self.min_baseline_scans
            )
            self._update_state(sensor_context, space_model)
            return []

        # Run individual checks
        if sensor_context is not None:
            anomalies.extend(self._check_ble(sensor_context, world_model, space_model, now_ms))
            anomalies.extend(self._check_speech(sensor_context, world_model, space_model, now_ms))
            anomalies.extend(self._check_scene(sensor_context, space_model, now_ms))
            anomalies.extend(self._check_unusual_hour(sensor_context, space_model, now_ms))

        # Update state for next cycle
        self._update_state(sensor_context, space_model)

        # Fire callback
        if anomalies and self.on_anomaly:
            try:
                self.on_anomaly(anomalies)
            except Exception as e:  # grain: ignore NAKED_EXCEPT -- anomaly detection heuristic -- bad sensor data must not crash detection
                logger.error("[AnomalyDetector] Callback error: %s", e)

        if anomalies:
            logger.info(
                "[AnomalyDetector] %d anomaly(ies) detected: %s",
                len(anomalies),
                [a.anomaly_type.value for a in anomalies],
            )

        return anomalies

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_ble(self, ctx, world_model, space_model, now_ms: int) -> List[AnomalyEvent]:
        """Detect BLE device appearance/departure anomalies."""
        anomalies: List[AnomalyEvent] = []

        proximity = getattr(ctx, "proximity", None)
        if proximity is None:
            return anomalies

        current_macs: Set[str] = set(getattr(proximity, "rssi_map", {}).keys())
        rssi_map: Dict[str, int] = getattr(proximity, "rssi_map", {})
        known_devices: List[str] = getattr(proximity, "known_devices", [])

        # NEW devices appeared (not in SpaceModel history at all)
        new_macs = current_macs - self._known_macs
        for mac in new_macs:
            rssi = rssi_map.get(mac, -100)
            anomalies.append(AnomalyEvent(
                anomaly_type=AnomalyType.NEW_UNKNOWN_DEVICE,
                description=f"New BLE device never seen before: {mac} (rssi={rssi}dBm)",
                confidence=0.85,
                timestamp_ms=now_ms,
                sensor_data={"mac": mac, "rssi": rssi},
            ))

        # KNOWN devices appeared (were in SpaceModel but not in previous scan)
        appeared = current_macs & self._known_macs - self._previous_macs
        for mac in appeared:
            rssi = rssi_map.get(mac, -100)
            is_infra = mac in self._infra_macs
            if not is_infra:  # Infrastructure reappearing is normal
                anomalies.append(AnomalyEvent(
                    anomaly_type=AnomalyType.KNOWN_DEVICE_APPEARED,
                    description=f"Known BLE device came into range: {mac} (rssi={rssi}dBm)",
                    confidence=0.75,
                    timestamp_ms=now_ms,
                    sensor_data={"mac": mac, "rssi": rssi},
                ))

        # KNOWN devices departed (were in previous scan but not current)
        departed = (self._previous_macs & self._known_macs) - current_macs
        for mac in departed:
            is_infra = mac in self._infra_macs
            if not is_infra:  # Infrastructure leaving is anomalous, transient is normal
                anomalies.append(AnomalyEvent(
                    anomaly_type=AnomalyType.KNOWN_DEVICE_DEPARTED,
                    description=f"Known BLE device left range: {mac}",
                    confidence=0.70,
                    timestamp_ms=now_ms,
                    sensor_data={"mac": mac},
                ))

        # Check for newly classified infrastructure devices
        all_devices = space_model.get_all_ble_devices()
        new_infra = {
            d["mac"] for d in all_devices
            if d["is_infrastructure"] and d["mac"] not in self._infra_macs
        }
        for mac in new_infra:
            name = next((d["name"] for d in all_devices if d["mac"] == mac), None)
            anomalies.append(AnomalyEvent(
                anomaly_type=AnomalyType.NEW_INFRASTRUCTURE_DEVICE,
                description=f"Device newly classified as always-present infrastructure: {name or mac}",
                confidence=0.90,
                timestamp_ms=now_ms,
                sensor_data={"mac": mac, "name": name},
            ))

        return anomalies

    def _check_speech(self, ctx, world_model, space_model, now_ms: int) -> List[AnomalyEvent]:
        """Detect speech when space was previously empty."""
        anomalies: List[AnomalyEvent] = []

        audio = getattr(ctx, "audio", None)
        if audio is None:
            return anomalies

        ambient_class = getattr(audio, "ambient_class", "unknown")
        if ambient_class != "speech":
            return anomalies

        # Check if space was recently empty (no BLE devices, no prior speech)
        proximity = getattr(ctx, "proximity", None)
        ble_count = len(getattr(proximity, "rssi_map", {})) if proximity else 0
        non_infra = ble_count - len(self._infra_macs)

        # Space was "empty" if only infrastructure BLE present
        if non_infra <= 0:
            confidence = getattr(audio, "confidence", 0.5)
            anomalies.append(AnomalyEvent(
                anomaly_type=AnomalyType.SPEECH_IN_EMPTY_SPACE,
                description=(
                    f"Speech detected but space appears empty "
                    f"(only {ble_count} BLE devices, {len(self._infra_macs)} infrastructure). "
                    f"ambient_class=speech, confidence={confidence:.2f}"
                ),
                confidence=confidence * 0.9,
                timestamp_ms=now_ms,
                sensor_data={
                    "ambient_class": ambient_class,
                    "audio_confidence": confidence,
                    "ble_count": ble_count,
                    "infra_count": len(self._infra_macs),
                },
            ))

        return anomalies

    def _check_scene(self, ctx, space_model, now_ms: int) -> List[AnomalyEvent]:
        """Detect significant visual scene changes."""
        anomalies: List[AnomalyEvent] = []

        visual = getattr(ctx, "visual", None)
        if visual is None:
            return anomalies

        # We need the diff score from space_model's most recent snapshot
        # The DiscoveryLoop will compute diff and store it; we just check the threshold
        # by reading the latest scene snapshot from space_model
        try:
            with space_model._conn() as conn:
                latest_scene = conn.execute("""
                    SELECT diff_score_from_baseline, lighting_level, mean_pixel_value
                    FROM scene_snapshots
                    ORDER BY timestamp DESC LIMIT 1
                """).fetchone()
        except Exception:  # grain: ignore NAKED_EXCEPT -- anomaly detection heuristic -- bad sensor data must not crash detection
            return anomalies

        if latest_scene is None:
            return anomalies

        diff_score = latest_scene["diff_score_from_baseline"]
        if diff_score > self.scene_change_threshold:
            lighting = latest_scene["lighting_level"]
            anomalies.append(AnomalyEvent(
                anomaly_type=AnomalyType.SCENE_CHANGE,
                description=(
                    f"Significant scene change detected: diff={diff_score:.2f} "
                    f"(threshold={self.scene_change_threshold}). "
                    f"Lighting: {lighting}."
                ),
                confidence=min(1.0, diff_score / 0.5),  # scales with severity
                timestamp_ms=now_ms,
                sensor_data={
                    "diff_score": diff_score,
                    "lighting": lighting,
                    "mean_pixel_value": latest_scene["mean_pixel_value"],
                },
            ))

        return anomalies

    def _check_unusual_hour(self, ctx, space_model, now_ms: int) -> List[AnomalyEvent]:
        """Detect activity outside normal occupancy hours."""
        anomalies: List[AnomalyEvent] = []

        # Determine current hour
        current_hour = int((now_ms / 3600000) % 24)

        # Get normal hours from SpaceModel or configured window
        if self.unusual_hour_window:
            start_hour, end_hour = self.unusual_hour_window
            is_unusual = not (start_hour <= current_hour <= end_hour)
        else:
            # Learn from SpaceModel: get hours with activity
            try:
                summary = space_model.get_space_summary()
                active_hours = summary["activity_patterns"]["active_hours"]
                if len(active_hours) < 5:
                    # Not enough data to establish normal pattern
                    return anomalies
                is_unusual = current_hour not in active_hours
            except Exception:  # grain: ignore NAKED_EXCEPT -- anomaly detection heuristic -- bad sensor data must not crash detection
                return anomalies

        if not is_unusual:
            return anomalies

        # Only flag if there's actual activity (audio or BLE non-infra)
        audio = getattr(ctx, "audio", None)
        proximity = getattr(ctx, "proximity", None)
        ambient_class = getattr(audio, "ambient_class", "silence") if audio else "silence"
        ble_count = len(getattr(proximity, "rssi_map", {})) if proximity else 0
        non_infra_ble = ble_count - len(self._infra_macs)

        has_activity = (ambient_class == "speech") or (non_infra_ble > 0)
        if has_activity:
            anomalies.append(AnomalyEvent(
                anomaly_type=AnomalyType.UNUSUAL_HOUR_ACTIVITY,
                description=(
                    f"Activity detected at unusual hour {current_hour:02d}:00. "
                    f"Audio: {ambient_class}. Non-infra BLE: {non_infra_ble}."
                ),
                confidence=0.75,
                timestamp_ms=now_ms,
                sensor_data={
                    "hour": current_hour,
                    "ambient_class": ambient_class,
                    "non_infra_ble_count": non_infra_ble,
                },
            ))

        return anomalies

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def _sync_known_macs(self, space_model) -> None:
        """Refresh known MACs and infrastructure MACs from SpaceModel."""
        try:
            devices = space_model.get_all_ble_devices()
            self._known_macs = {d["mac"] for d in devices}
            self._infra_macs = {d["mac"] for d in devices if d["is_infrastructure"]}
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- anomaly detection heuristic -- bad sensor data must not crash detection
            logger.warning("[AnomalyDetector] Failed to sync MACs: %s", e)

    def _update_state(self, sensor_context, space_model) -> None:
        """Update previous-scan state for next cycle's departure detection."""
        if sensor_context is None:
            return

        proximity = getattr(sensor_context, "proximity", None)
        if proximity is not None:
            self._previous_macs = set(getattr(proximity, "rssi_map", {}).keys())
