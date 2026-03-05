"""DiscoveryReport -- Synthesizes WorldModel + SpaceModel into a structured report.

The report is sent to the gateway or written to a local file. It provides
a complete snapshot of what the Distiller has learned about its space,
including current world state, anomalies, and self-assessment.

Can be serialized to JSON for gateway ingestion or local file storage.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Output directory for local file storage (on device)
_DEVICE_OUTPUT_DIR = "/home/distiller/discovery-output"
_LOCAL_OUTPUT_DIR = os.path.expanduser("~/.openclaw-embodiment/discovery-output")


def _default_output_dir() -> str:
    if os.path.exists("/home/distiller"):
        return _DEVICE_OUTPUT_DIR
    return _LOCAL_OUTPUT_DIR


@dataclass
class DiscoveryReport:
    """Structured synthesis of spatial discovery session.

    Attributes:
        timestamp_ms:       Unix ms when report was generated
        device_id:          Device identifier
        session_duration_s: How long the discovery loop ran (seconds)

        ble_inventory:      All BLE devices seen, classified
        activity_summary:   Occupancy windows, speech frequency, active hours
        scene_summary:      Lighting profile, change events

        current_entities:   Active world model entities with confidence
        narrative:          Full natural language description of the space

        anomalies:          Anomalies detected during this session

        sensor_health:      Per-sensor health status dict
        awareness_level:    Average awareness across the session
        confidence:         Overall confidence in the space model
    """
    timestamp_ms: int
    device_id: str
    session_duration_s: int

    # Space knowledge
    ble_inventory: List[Dict[str, Any]] = field(default_factory=list)
    activity_summary: Dict[str, Any] = field(default_factory=dict)
    scene_summary: Dict[str, Any] = field(default_factory=dict)

    # World state
    current_entities: List[Dict[str, Any]] = field(default_factory=list)
    narrative: str = ""

    # Anomalies
    anomalies: List[Dict[str, Any]] = field(default_factory=list)

    # Self-assessment
    sensor_health: Dict[str, str] = field(default_factory=dict)
    awareness_level: float = 0.0
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a flat dict."""
        return {
            "timestamp_ms": self.timestamp_ms,
            "timestamp_iso": datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "device_id": self.device_id,
            "session_duration_s": self.session_duration_s,
            "ble_inventory": self.ble_inventory,
            "activity_summary": self.activity_summary,
            "scene_summary": self.scene_summary,
            "current_entities": self.current_entities,
            "narrative": self.narrative,
            "anomalies": self.anomalies,
            "sensor_health": self.sensor_health,
            "awareness_level": round(self.awareness_level, 4),
            "confidence": round(self.confidence, 4),
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    def save_to_file(self, output_dir: Optional[str] = None) -> str:
        """Write report JSON to a timestamped file.

        Args:
            output_dir: Directory to write to. Defaults to device or local path.

        Returns:
            Absolute path of the written file.
        """
        out_dir = output_dir or _default_output_dir()
        os.makedirs(out_dir, exist_ok=True)

        ts = datetime.fromtimestamp(self.timestamp_ms / 1000, tz=timezone.utc)
        filename = ts.strftime("%Y-%m-%d_%H-%M-%S") + ".json"
        filepath = os.path.join(out_dir, filename)

        with open(filepath, "w") as f:
            f.write(self.to_json())

        logger.info("[DiscoveryReport] Written to %s", filepath)
        return filepath


# ------------------------------------------------------------------
# Factory function
# ------------------------------------------------------------------

def build_report(
    world_model,
    space_model,
    anomalies: Optional[List] = None,
    session_start_ms: Optional[int] = None,
    sensor_health: Optional[Dict[str, str]] = None,
    awareness_history: Optional[List[float]] = None,
    device_id: Optional[str] = None,
) -> DiscoveryReport:
    """Build a DiscoveryReport from WorldModel and SpaceModel.

    Args:
        world_model:        Current WorldModel instance.
        space_model:        Current SpaceModel instance.
        anomalies:          List of AnomalyEvent objects from this session.
        session_start_ms:   Unix ms when the session started.
        sensor_health:      Dict of sensor name -> "ok" | "degraded" | "failed".
        awareness_history:  List of awareness_level floats from each cycle.
        device_id:          Override device ID (else use world_model's).

    Returns:
        DiscoveryReport ready for serialization.
    """
    now_ms = int(time.time() * 1000)
    start_ms = session_start_ms or now_ms
    duration_s = int((now_ms - start_ms) / 1000)

    dev_id = device_id or getattr(world_model, "device_id", "unknown")

    # --- World state ---
    world_state = world_model.get_current_state()
    current_entities = world_state.get("active_entities", [])
    narrative_parts: List[str] = []

    # World narrative
    narrative_parts.append(world_model.get_narrative())

    # Space narrative
    space_narrative = space_model.get_narrative()
    narrative_parts.append(space_narrative)

    full_narrative = " | ".join(p for p in narrative_parts if p)

    # --- Space knowledge ---
    space_summary = space_model.get_space_summary()

    ble_inventory = space_summary.get("device_inventory", {}).get("devices", [])
    activity_summary = space_summary.get("activity_patterns", {})
    scene_summary = space_summary.get("scene_baseline", {})

    # Add recent discovery log to scene summary
    recent_discoveries = space_model.get_recent_discoveries(limit=10)
    scene_summary["recent_discoveries"] = recent_discoveries

    # --- Anomalies ---
    anomaly_list = []
    for a in (anomalies or []):
        if hasattr(a, "to_dict"):
            anomaly_list.append(a.to_dict())
        elif isinstance(a, dict):
            anomaly_list.append(a)

    health = sensor_health or {}
    if not health:
        # Infer from world_state
        caps = None
        if world_model.current_context:
            caps = getattr(world_model.current_context, "device_capabilities", None)
        if caps:
            health["mic"] = "ok" if getattr(caps, "has_microphone", False) else "not_present"
            health["camera"] = "color_unreliable"  # Distiller OV5647 known issue
            health["ble"] = "ok" if getattr(caps, "has_ble", False) else "not_present"
            health["imu"] = "not_present"  # CM5 has no IMU
        else:
            health = {"mic": "unknown", "camera": "unknown", "ble": "unknown"}

    # --- Awareness ---
    avg_awareness = 0.0
    if awareness_history:
        avg_awareness = sum(awareness_history) / len(awareness_history)
    else:
        avg_awareness = world_state.get("awareness_level", 0.0)

    # --- Overall confidence ---
    # Based on: how many sensors active, how many scans done, history depth
    total_scans = getattr(space_model, "_scan_count", 0)
    history_depth = world_state.get("history_depth", 0)
    sensor_count = sum(
        1 for v in health.values()
        if v in ("ok", "color_unreliable")
    )
    confidence = min(1.0, (
        (min(total_scans, 10) / 10) * 0.4 +
        (min(history_depth, 20) / 20) * 0.3 +
        (sensor_count / 3) * 0.3
    ))

    return DiscoveryReport(
        timestamp_ms=now_ms,
        device_id=dev_id,
        session_duration_s=duration_s,
        ble_inventory=ble_inventory,
        activity_summary=activity_summary,
        scene_summary=scene_summary,
        current_entities=current_entities,
        narrative=full_narrative,
        anomalies=anomaly_list,
        sensor_health=health,
        awareness_level=round(avg_awareness, 4),
        confidence=round(confidence, 4),
    )
