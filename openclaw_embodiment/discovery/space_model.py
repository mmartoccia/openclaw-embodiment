"""SpaceModel -- Persistent SQLite-backed knowledge store.

Records what the Distiller has learned about its environment over time.
Persists across restarts. Tables track:
  - BLE devices observed (infrastructure vs transient)
  - Activity windows (occupancy, speech patterns)
  - Scene snapshots (lighting profile, visual change events)
  - Discovery log (significant events with confidence)

Default DB path:
  - On device:  /home/distiller/space-model.db
  - Locally:    ~/.openclaw-embodiment/space-model.db
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

# Infrastructure threshold: device appears in this fraction of scans
INFRASTRUCTURE_THRESHOLD = 0.80

# Default DB paths
_DEVICE_DB_PATH = "/home/distiller/space-model.db"
_LOCAL_DB_PATH = os.path.expanduser("~/.openclaw-embodiment/space-model.db")


def _default_db_path() -> str:
    """Return the appropriate default DB path for this environment."""
    if os.path.exists("/home/distiller"):
        return _DEVICE_DB_PATH
    os.makedirs(os.path.dirname(_LOCAL_DB_PATH), exist_ok=True)
    return _LOCAL_DB_PATH


class SpaceModel:
    """Persistent spatial knowledge store backed by SQLite.

    Args:
        db_path:    Path to the SQLite DB file. Created if it doesn't exist.
        device_id:  Identifier for this device (used in log entries).
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        device_id: str = "distiller-cm5",
    ) -> None:
        self.db_path = db_path or _default_db_path()
        self.device_id = device_id
        self._scan_count = 0  # total BLE scans since startup (for infra classification)
        self._init_db()
        logger.info("[SpaceModel] Initialized DB at %s", self.db_path)

    # ------------------------------------------------------------------
    # DB Setup
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS ble_devices (
                    mac             TEXT PRIMARY KEY,
                    name            TEXT,
                    first_seen      INTEGER NOT NULL,
                    last_seen       INTEGER NOT NULL,
                    last_rssi       INTEGER,
                    total_observations INTEGER DEFAULT 1,
                    scans_present   INTEGER DEFAULT 1,
                    is_infrastructure INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS activity_windows (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       INTEGER NOT NULL,
                    duration_min    REAL,
                    has_speech      INTEGER DEFAULT 0,
                    speaker_count_estimate INTEGER,
                    ambient_class   TEXT,
                    ble_device_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS scene_snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       INTEGER NOT NULL,
                    lighting_level  TEXT,
                    mean_pixel_value REAL,
                    diff_score_from_baseline REAL DEFAULT 0.0,
                    is_baseline     INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS discovery_log (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       INTEGER NOT NULL,
                    event_type      TEXT NOT NULL,
                    description     TEXT,
                    confidence      REAL DEFAULT 0.5
                );

                CREATE INDEX IF NOT EXISTS idx_ble_last_seen
                    ON ble_devices (last_seen);
                CREATE INDEX IF NOT EXISTS idx_activity_timestamp
                    ON activity_windows (timestamp);
                CREATE INDEX IF NOT EXISTS idx_scene_timestamp
                    ON scene_snapshots (timestamp);
            """)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Update methods
    # ------------------------------------------------------------------

    def update_ble(self, proximity_context) -> None:
        """Upsert BLE device records and classify infrastructure vs transient.

        Args:
            proximity_context:  ProximityContext from BLEProximityScanner.
                                Must have rssi_map, known_devices attributes.
        """
        if proximity_context is None:
            return

        self._scan_count += 1
        now_ms = int(time.time() * 1000)

        rssi_map: Dict[str, int] = getattr(proximity_context, "rssi_map", {})
        known_devices: List[str] = getattr(proximity_context, "known_devices", [])

        # Build mac -> name mapping for known devices
        # known_devices is a list of names; we need the reverse from rssi_map
        # We'll need to check which macs correspond to known names
        # The rssi_map has all macs; known_devices has names of recognized ones
        # We track them all by MAC

        with self._conn() as conn:
            for mac, rssi in rssi_map.items():
                # Determine name if this is a known device
                name = None
                # We don't have a direct mac->name here from ProximityContext
                # but known_devices has the names. We store name as None for unknown.
                # If the scanner has a known_map we could use it, but for robustness
                # we just store whatever we can derive.

                existing = conn.execute(
                    "SELECT total_observations, scans_present, name FROM ble_devices WHERE mac = ?",
                    (mac,)
                ).fetchone()

                if existing:
                    new_obs = existing["total_observations"] + 1
                    new_scans = existing["scans_present"] + 1
                    current_name = existing["name"]

                    # Classify infrastructure: appears in >80% of scans
                    infra = 1 if (new_scans / self._scan_count) >= INFRASTRUCTURE_THRESHOLD else 0

                    conn.execute("""
                        UPDATE ble_devices
                        SET last_seen=?, last_rssi=?, total_observations=?,
                            scans_present=?, is_infrastructure=?
                            {name_update}
                        WHERE mac=?
                    """.replace(
                        "{name_update}",
                        ", name=?" if (current_name is None and name is not None) else ""
                    ),
                    (now_ms, rssi, new_obs, new_scans, infra, mac)
                    if (current_name is None and name is not None) else
                    (now_ms, rssi, new_obs, new_scans, infra, mac))

                    # Log if newly classified as infrastructure
                    if infra and not existing:
                        self.log_discovery(
                            "NEW_INFRASTRUCTURE_DEVICE",
                            f"Device {mac} classified as infrastructure (present in {new_scans}/{self._scan_count} scans)",
                            confidence=0.85,
                        )
                else:
                    # First time seen
                    conn.execute("""
                        INSERT INTO ble_devices
                            (mac, name, first_seen, last_seen, last_rssi,
                             total_observations, scans_present, is_infrastructure)
                        VALUES (?, ?, ?, ?, ?, 1, 1, 0)
                    """, (mac, name, now_ms, now_ms, rssi))

        logger.debug(
            "[SpaceModel] BLE update: %d devices, scan #%d",
            len(rssi_map), self._scan_count
        )

    def update_ble_with_names(self, proximity_context, known_map: Dict[str, str]) -> None:
        """Same as update_ble but also records device names from known_map.

        Args:
            proximity_context:  ProximityContext.
            known_map:          Dict[mac_lower -> name] from BLEProximityScanner.
        """
        if proximity_context is None:
            return

        self._scan_count += 1
        now_ms = int(time.time() * 1000)
        rssi_map: Dict[str, int] = getattr(proximity_context, "rssi_map", {})

        with self._conn() as conn:
            for mac, rssi in rssi_map.items():
                name = known_map.get(mac.lower())

                existing = conn.execute(
                    "SELECT total_observations, scans_present, name, is_infrastructure FROM ble_devices WHERE mac = ?",
                    (mac,)
                ).fetchone()

                if existing:
                    new_obs = existing["total_observations"] + 1
                    new_scans = existing["scans_present"] + 1
                    was_infra = existing["is_infrastructure"]
                    infra = 1 if (new_scans / self._scan_count) >= INFRASTRUCTURE_THRESHOLD else 0

                    conn.execute("""
                        UPDATE ble_devices
                        SET last_seen=?, last_rssi=?, total_observations=?,
                            scans_present=?, is_infrastructure=?,
                            name=COALESCE(name, ?)
                        WHERE mac=?
                    """, (now_ms, rssi, new_obs, new_scans, infra, name, mac))

                    if infra and not was_infra:
                        self.log_discovery(
                            "NEW_INFRASTRUCTURE_DEVICE",
                            f"Device {name or mac} now always-present infrastructure "
                            f"({new_scans}/{self._scan_count} scans)",
                            confidence=0.85,
                        )
                else:
                    conn.execute("""
                        INSERT INTO ble_devices
                            (mac, name, first_seen, last_seen, last_rssi,
                             total_observations, scans_present, is_infrastructure)
                        VALUES (?, ?, ?, ?, ?, 1, 1, 0)
                    """, (mac, name, now_ms, now_ms, rssi))

    def update_activity(self, audio_context, timestamp: Optional[int] = None) -> None:
        """Log an activity window from an AudioContext snapshot.

        Args:
            audio_context:  AudioContext (may be None -- logs empty window).
            timestamp:      Unix ms. Defaults to now.
        """
        ts = timestamp or int(time.time() * 1000)
        has_speech = 0
        speaker_count = None
        ambient_class = "unknown"
        ble_count = 0

        if audio_context is not None:
            ambient_class = getattr(audio_context, "ambient_class", "unknown")
            has_speech = 1 if ambient_class == "speech" else 0
            speaker_count = getattr(audio_context, "speaker_count", None)

        with self._conn() as conn:
            conn.execute("""
                INSERT INTO activity_windows
                    (timestamp, duration_min, has_speech, speaker_count_estimate,
                     ambient_class, ble_device_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ts, 1.0, has_speech, speaker_count, ambient_class, ble_count))

    def update_activity_full(
        self,
        audio_context,
        proximity_context,
        timestamp: Optional[int] = None,
        duration_min: float = 1.0,
    ) -> None:
        """Log an activity window with BLE device count included."""
        ts = timestamp or int(time.time() * 1000)
        has_speech = 0
        speaker_count = None
        ambient_class = "unknown"
        ble_count = len(getattr(proximity_context, "rssi_map", {})) if proximity_context else 0

        if audio_context is not None:
            ambient_class = getattr(audio_context, "ambient_class", "unknown")
            has_speech = 1 if ambient_class == "speech" else 0
            speaker_count = getattr(audio_context, "speaker_count", None)

        with self._conn() as conn:
            conn.execute("""
                INSERT INTO activity_windows
                    (timestamp, duration_min, has_speech, speaker_count_estimate,
                     ambient_class, ble_device_count)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ts, duration_min, has_speech, speaker_count, ambient_class, ble_count))

    def update_scene(
        self,
        lighting_level: str,
        mean_px: float,
        diff_score: float,
        is_baseline: bool = False,
    ) -> None:
        """Log a scene snapshot.

        Args:
            lighting_level:  "bright" | "dim" | "dark"
            mean_px:         Mean pixel value from grayscale frame (0-255)
            diff_score:      0.0-1.0 difference from current baseline
            is_baseline:     If True, this snapshot becomes the new baseline
        """
        ts = int(time.time() * 1000)

        with self._conn() as conn:
            conn.execute("""
                INSERT INTO scene_snapshots
                    (timestamp, lighting_level, mean_pixel_value,
                     diff_score_from_baseline, is_baseline)
                VALUES (?, ?, ?, ?, ?)
            """, (ts, lighting_level, mean_px, diff_score, 1 if is_baseline else 0))

        if is_baseline:
            logger.info("[SpaceModel] New scene baseline set: lighting=%s mean_px=%.1f",
                       lighting_level, mean_px)

    def log_discovery(
        self,
        event_type: str,
        description: str,
        confidence: float = 0.5,
    ) -> None:
        """Append an entry to the discovery log.

        Args:
            event_type:   Short event type string (e.g. "NEW_UNKNOWN_DEVICE")
            description:  Human-readable description
            confidence:   0.0-1.0 confidence in this observation
        """
        ts = int(time.time() * 1000)
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO discovery_log (timestamp, event_type, description, confidence)
                VALUES (?, ?, ?, ?)
            """, (ts, event_type, description, confidence))
        logger.debug("[SpaceModel] Discovery logged: %s -- %s", event_type, description)

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_space_summary(self) -> Dict[str, Any]:
        """Synthesize current knowledge into a structured summary dict.

        Returns:
            dict with keys: device_inventory, activity_patterns, scene_baseline,
            discovery_log_count, total_scans
        """
        with self._conn() as conn:
            # BLE device inventory
            devices = conn.execute("""
                SELECT mac, name, first_seen, last_seen, last_rssi,
                       total_observations, scans_present, is_infrastructure
                FROM ble_devices
                ORDER BY total_observations DESC
            """).fetchall()

            # Activity patterns
            total_windows = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_windows"
            ).fetchone()["cnt"]

            speech_windows = conn.execute(
                "SELECT COUNT(*) as cnt FROM activity_windows WHERE has_speech=1"
            ).fetchone()["cnt"]

            # Most recent activity
            recent_activity = conn.execute("""
                SELECT timestamp, ambient_class, ble_device_count
                FROM activity_windows
                ORDER BY timestamp DESC LIMIT 10
            """).fetchall()

            # Hour-of-day occupancy distribution (which hours have activity)
            hourly = conn.execute("""
                SELECT
                    CAST((timestamp / 3600000) % 24 AS INTEGER) as hour,
                    COUNT(*) as count
                FROM activity_windows
                GROUP BY hour
                ORDER BY hour
            """).fetchall()

            # Scene baseline status
            baseline = conn.execute("""
                SELECT timestamp, lighting_level, mean_pixel_value
                FROM scene_snapshots
                WHERE is_baseline=1
                ORDER BY timestamp DESC LIMIT 1
            """).fetchone()

            recent_scenes = conn.execute("""
                SELECT lighting_level, diff_score_from_baseline
                FROM scene_snapshots
                ORDER BY timestamp DESC LIMIT 20
            """).fetchall()

            # Discovery log count
            log_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM discovery_log"
            ).fetchone()["cnt"]

        # Compute occupancy hours
        active_hours = [row["hour"] for row in hourly if row["count"] >= 2]

        # Compute average diff score
        avg_diff = 0.0
        if recent_scenes:
            avg_diff = sum(r["diff_score_from_baseline"] for r in recent_scenes) / len(recent_scenes)

        return {
            "device_inventory": {
                "total_seen": len(devices),
                "infrastructure": sum(1 for d in devices if d["is_infrastructure"]),
                "transient": sum(1 for d in devices if not d["is_infrastructure"]),
                "devices": [
                    {
                        "mac": d["mac"],
                        "name": d["name"],
                        "is_infrastructure": bool(d["is_infrastructure"]),
                        "total_observations": d["total_observations"],
                        "last_rssi": d["last_rssi"],
                        "last_seen_ms": d["last_seen"],
                    }
                    for d in devices
                ],
            },
            "activity_patterns": {
                "total_windows": total_windows,
                "speech_windows": speech_windows,
                "speech_frequency": round(speech_windows / total_windows, 3) if total_windows > 0 else 0.0,
                "active_hours": active_hours,
                "occupancy_hours_estimate": f"{min(active_hours, default=0):02d}:00-{max(active_hours, default=23):02d}:00" if active_hours else "unknown",
            },
            "scene_baseline": {
                "has_baseline": baseline is not None,
                "baseline_lighting": baseline["lighting_level"] if baseline else None,
                "baseline_mean_px": baseline["mean_pixel_value"] if baseline else None,
                "avg_diff_score_recent": round(avg_diff, 3),
            },
            "discovery_log_count": log_count,
            "total_scans": self._scan_count,
        }

    def get_all_ble_devices(self) -> List[Dict[str, Any]]:
        """Return all BLE devices ever seen."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT mac, name, first_seen, last_seen, last_rssi,
                       total_observations, is_infrastructure
                FROM ble_devices
                ORDER BY total_observations DESC
            """).fetchall()
        return [dict(r) for r in rows]

    def get_recent_discoveries(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return most recent discovery log entries."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT timestamp, event_type, description, confidence
                FROM discovery_log
                ORDER BY timestamp DESC, rowid DESC LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_narrative(self) -> str:
        """Generate a natural language description of what the system has learned.

        Returns:
            Human-readable description of the space and its patterns.
        """
        summary = self.get_space_summary()
        parts: List[str] = []

        # Space type inference (rough heuristic)
        inv = summary["device_inventory"]
        infra_count = inv["infrastructure"]
        total_devices = inv["total_seen"]

        if infra_count >= 5:
            space_type = "busy environment (office, public space)"
        elif infra_count >= 2:
            space_type = "home or small office"
        elif infra_count == 1:
            space_type = "private space"
        else:
            space_type = "unknown environment"

        parts.append(f"This appears to be a {space_type}.")

        # Device summary
        if total_devices > 0:
            if infra_count > 0:
                parts.append(
                    f"{infra_count} infrastructure device(s) always present, "
                    f"{inv['transient']} transient device(s) observed."
                )
            else:
                parts.append(f"{total_devices} BLE device(s) observed (none yet classified as infrastructure).")
        else:
            parts.append("No BLE devices observed yet.")

        # Occupancy patterns
        act = summary["activity_patterns"]
        if act["total_windows"] > 0:
            occ = act["occupancy_hours_estimate"]
            if occ != "unknown":
                parts.append(f"Occupied typically {occ}.")
            speech_pct = int(act["speech_frequency"] * 100)
            parts.append(f"Speech activity in {speech_pct}% of samples.")
        else:
            parts.append("No activity history yet.")

        scene = summary["scene_baseline"]
        if scene["has_baseline"]:
            parts.append(
                f"Scene baseline: {scene['baseline_lighting']} lighting "
                f"(mean pixel {scene['baseline_mean_px']:.0f}). "
                f"Recent scene drift: {scene['avg_diff_score_recent']:.2f}."
            )
        else:
            parts.append("No scene baseline established yet.")

        return " ".join(parts)
