"""WorldModel -- Rolling entity state tracker with confidence decay.

Implements the WorldModel from CONTEXT_ENGINE_SPEC.md:
  - EntityState: typed entity with confidence decay
  - WorldModel: maintained semantic state of the environment
  - Linear TTL decay per entity type
  - Entities below 0.2 confidence move to last_known (not deleted)

Confidence decay formula:
    confidence(t) = initial * max(0, 1 - (elapsed_s / half_life_s))

Half-lives per spec:
    Person (audio confirmed)  120s
    Person (visual only)       60s
    BLE device (connected)    300s
    BLE device (scan only)     30s
    Motion state               10s
    Ambient class              30s
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Confidence decay half-lives (seconds)
# ------------------------------------------------------------------

HALF_LIVES: Dict[str, float] = {
    "person_audio": 120.0,
    "person_visual": 60.0,
    "ble_connected": 300.0,
    "ble_scan": 30.0,
    "motion": 10.0,
    "ambient": 30.0,
    "unknown": 60.0,
}

# Entities with confidence below this threshold move to last_known
CONFIDENCE_FLOOR = 0.2

# Default rolling history window
DEFAULT_WINDOW_SECONDS = 300  # 5 minutes
DEFAULT_HISTORY_DEPTH = 100   # max SensorContext snapshots


# ------------------------------------------------------------------
# EntityState
# ------------------------------------------------------------------

@dataclass
class EntityState:
    """A tracked entity in the physical environment.

    Attributes:
        entity_id:      Unique identifier (e.g. "ble:aa:bb:cc", "person:audio:0")
        entity_type:    "person" | "device" | "ambient" | "motion"
        last_seen:      Unix timestamp ms when last confirmed
        last_context:   Short description of last observation
        confidence:     0.0-1.0, decays over time per half-life
        source_sensors: List of sensor names that contributed
        half_life_key:  Key into HALF_LIVES dict for decay rate
        initial_confidence: Original confidence when first/last confirmed
    """
    entity_id: str
    entity_type: str
    last_seen: int          # ms
    last_context: str = ""
    confidence: float = 1.0
    source_sensors: List[str] = field(default_factory=list)
    half_life_key: str = "unknown"
    initial_confidence: float = 1.0

    def current_confidence(self, now_ms: Optional[int] = None) -> float:
        """Compute decayed confidence at the current moment.

        Args:
            now_ms: Unix ms. Defaults to current time.

        Returns:
            Decayed confidence (0.0 - initial_confidence).
        """
        now = now_ms or int(time.time() * 1000)
        elapsed_s = (now - self.last_seen) / 1000.0
        half_life = HALF_LIVES.get(self.half_life_key, HALF_LIVES["unknown"])
        decayed = self.initial_confidence * max(0.0, 1.0 - (elapsed_s / half_life))
        return round(decayed, 4)

    def refresh(self, confidence: float, context: str, sensors: List[str]) -> None:
        """Refresh this entity with a new observation."""
        self.last_seen = int(time.time() * 1000)
        self.initial_confidence = confidence
        self.confidence = confidence
        self.last_context = context
        self.source_sensors = sensors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "entity_type": self.entity_type,
            "last_seen_ms": self.last_seen,
            "last_context": self.last_context,
            "confidence": self.confidence,
            "source_sensors": self.source_sensors,
            "half_life_key": self.half_life_key,
        }


# ------------------------------------------------------------------
# WorldModel
# ------------------------------------------------------------------

class WorldModel:
    """Maintained semantic state of the physical environment.

    Feeds off a stream of SensorContext snapshots and maintains:
      - Rolling history window
      - Entity states with confidence decay
      - Natural language narrative
      - Staleness tracking

    Args:
        device_id:        Device identifier.
        window_seconds:   How many seconds of SensorContext history to keep.
        history_depth:    Max number of SensorContext snapshots to store.
        perspective:      "room_centric" | "user_centric" | "fused"
    """

    def __init__(
        self,
        device_id: str = "distiller-cm5",
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        history_depth: int = DEFAULT_HISTORY_DEPTH,
        perspective: str = "room_centric",
    ) -> None:
        self.device_id = device_id
        self.window_seconds = window_seconds
        self.history_depth = history_depth
        self.perspective = perspective

        self.last_updated: int = 0
        self.current_context = None  # most recent SensorContext
        self.history: Deque = deque(maxlen=history_depth)

        # Active entities (confidence >= 0.2)
        self.entity_states: Dict[str, EntityState] = {}
        # Demoted entities (confidence < 0.2 at demotion time)
        self.last_known: Dict[str, EntityState] = {}

        self.narrative: str = "No data yet."
        self.staleness: float = 0.0  # seconds since last update

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    def update(self, sensor_context) -> None:
        """Process a new SensorContext snapshot.

        - Adds to rolling history
        - Updates/creates entities from sensor data
        - Decays existing entities
        - Rebuilds narrative
        - Demotes low-confidence entities to last_known

        Args:
            sensor_context: SensorContext from ContextBuilder.
        """
        if sensor_context is None:
            return

        now_ms = int(time.time() * 1000)
        self.last_updated = now_ms
        self.current_context = sensor_context
        self.history.append(sensor_context)

        # Prune history older than window_seconds
        cutoff_ms = now_ms - (self.window_seconds * 1000)
        while self.history and getattr(self.history[0], "timestamp_ms", 0) < cutoff_ms:
            self.history.popleft()

        # Extract entities from this context
        self._extract_entities(sensor_context, now_ms)

        # Decay all active entities
        self._decay_entities(now_ms)

        # Rebuild narrative
        self.narrative = self._build_narrative(now_ms)
        self.staleness = 0.0

        logger.debug(
            "[WorldModel] Updated: %d active entities, %d last_known",
            len(self.entity_states), len(self.last_known)
        )

    # ------------------------------------------------------------------
    # Entity extraction
    # ------------------------------------------------------------------

    def _extract_entities(self, ctx, now_ms: int) -> None:
        """Parse SensorContext and upsert entities."""

        # Audio entities
        audio = getattr(ctx, "audio", None)
        if audio is not None:
            ambient_class = getattr(audio, "ambient_class", "unknown")
            confidence = getattr(audio, "confidence", 0.5)

            # Ambient state entity (always present if audio available)
            self._upsert_entity(
                entity_id="ambient:audio",
                entity_type="ambient",
                confidence=max(0.3, confidence),
                context=f"ambient={ambient_class} rms={getattr(audio, 'rms_level', 0):.0f}",
                sensors=["audio"],
                half_life_key="ambient",
                now_ms=now_ms,
            )

            # Person entity if speech detected
            if ambient_class == "speech":
                speaker_count = getattr(audio, "speaker_count", None) or 1
                self._upsert_entity(
                    entity_id="person:audio:0",
                    entity_type="person",
                    confidence=confidence,
                    context=f"speaking, estimated {speaker_count} speaker(s)",
                    sensors=["audio"],
                    half_life_key="person_audio",
                    now_ms=now_ms,
                )

        # Visual entities
        visual = getattr(ctx, "visual", None)
        if visual is not None:
            lighting = getattr(visual, "lighting", "unknown")
            confidence = getattr(visual, "confidence", 0.5)
            person_count = getattr(visual, "person_count", None)

            # Scene lighting entity
            self._upsert_entity(
                entity_id="ambient:visual",
                entity_type="ambient",
                confidence=confidence,
                context=f"lighting={lighting}",
                sensors=["camera"],
                half_life_key="ambient",
                now_ms=now_ms,
            )

            # Person entity if detected visually
            if person_count is not None and person_count > 0:
                self._upsert_entity(
                    entity_id="person:visual:0",
                    entity_type="person",
                    confidence=confidence * 0.8,  # visual-only is less certain
                    context=f"~{person_count} person(s) detected visually, {lighting} lighting",
                    sensors=["camera"],
                    half_life_key="person_visual",
                    now_ms=now_ms,
                )

        # BLE device entities
        proximity = getattr(ctx, "proximity", None)
        if proximity is not None:
            rssi_map: Dict[str, int] = getattr(proximity, "rssi_map", {})
            known_devices: List[str] = getattr(proximity, "known_devices", [])
            ble_confidence = getattr(proximity, "confidence", 0.5)

            for mac, rssi in rssi_map.items():
                entity_id = f"ble:{mac}"
                name = mac  # fallback
                # Check if this is a known device
                is_known = False
                for kname in known_devices:
                    # We don't have mac->name here, just track as unknown
                    pass

                self._upsert_entity(
                    entity_id=entity_id,
                    entity_type="device",
                    confidence=ble_confidence,
                    context=f"BLE device rssi={rssi}dBm",
                    sensors=["ble"],
                    half_life_key="ble_scan",
                    now_ms=now_ms,
                )

            # Known devices get longer half-life
            for name in known_devices:
                entity_id = f"ble:known:{name}"
                self._upsert_entity(
                    entity_id=entity_id,
                    entity_type="device",
                    confidence=min(1.0, ble_confidence + 0.1),
                    context=f"Known BLE device: {name}",
                    sensors=["ble"],
                    half_life_key="ble_connected",
                    now_ms=now_ms,
                )

    def _upsert_entity(
        self,
        entity_id: str,
        entity_type: str,
        confidence: float,
        context: str,
        sensors: List[str],
        half_life_key: str,
        now_ms: int,
    ) -> None:
        """Update existing entity or create new one."""
        if entity_id in self.entity_states:
            self.entity_states[entity_id].refresh(confidence, context, sensors)
        elif entity_id in self.last_known:
            # Promote back from last_known
            entity = self.last_known.pop(entity_id)
            entity.refresh(confidence, context, sensors)
            self.entity_states[entity_id] = entity
            logger.debug("[WorldModel] Promoted entity from last_known: %s", entity_id)
        else:
            # Create new entity
            self.entity_states[entity_id] = EntityState(
                entity_id=entity_id,
                entity_type=entity_type,
                last_seen=now_ms,
                last_context=context,
                confidence=confidence,
                source_sensors=sensors,
                half_life_key=half_life_key,
                initial_confidence=confidence,
            )
            logger.debug("[WorldModel] New entity: %s (type=%s)", entity_id, entity_type)

    # ------------------------------------------------------------------
    # Decay
    # ------------------------------------------------------------------

    def _decay_entities(self, now_ms: int) -> None:
        """Decay all active entities. Demote those below CONFIDENCE_FLOOR."""
        to_demote = []

        for entity_id, entity in self.entity_states.items():
            decayed = entity.current_confidence(now_ms)
            entity.confidence = decayed

            if decayed < CONFIDENCE_FLOOR:
                to_demote.append(entity_id)

        for entity_id in to_demote:
            entity = self.entity_states.pop(entity_id)
            self.last_known[entity_id] = entity
            logger.debug(
                "[WorldModel] Demoted to last_known: %s (confidence=%.3f)",
                entity_id, entity.confidence
            )

    # ------------------------------------------------------------------
    # Narrative
    # ------------------------------------------------------------------

    def _build_narrative(self, now_ms: int) -> str:
        """Construct a natural language world description."""
        parts: List[str] = []

        # People
        audio_person = self.entity_states.get("person:audio:0")
        visual_person = self.entity_states.get("person:visual:0")

        if audio_person and visual_person:
            parts.append("Person present (confirmed by audio and visual).")
        elif audio_person:
            parts.append(f"Speech detected (confidence {audio_person.confidence:.2f}). {audio_person.last_context}.")
        elif visual_person:
            parts.append(f"Person detected visually (confidence {visual_person.confidence:.2f}).")
        else:
            parts.append("No person detected.")

        # Ambient
        ambient_audio = self.entity_states.get("ambient:audio")
        ambient_visual = self.entity_states.get("ambient:visual")

        if ambient_audio:
            ambient_class = "speech" if "speech" in ambient_audio.last_context else \
                            "silence" if "silence" in ambient_audio.last_context else "noise"
            parts.append(f"Audio: {ambient_class}.")
        if ambient_visual:
            lighting = "unknown"
            if "bright" in ambient_visual.last_context:
                lighting = "bright"
            elif "dim" in ambient_visual.last_context:
                lighting = "dim"
            elif "dark" in ambient_visual.last_context:
                lighting = "dark"
            parts.append(f"Lighting: {lighting}.")

        # BLE devices
        ble_entities = [e for eid, e in self.entity_states.items() if e.entity_type == "device"]
        known_ble = [e for e in ble_entities if e.entity_id.startswith("ble:known:")]
        unknown_ble = [e for e in ble_entities if not e.entity_id.startswith("ble:known:")]

        if known_ble:
            names = [e.entity_id.replace("ble:known:", "") for e in known_ble]
            parts.append(f"Known BLE: {', '.join(names)}.")
        if unknown_ble:
            parts.append(f"{len(unknown_ble)} unknown BLE device(s) in range.")

        # Staleness of world model
        if self.last_updated:
            age_s = (now_ms - self.last_updated) / 1000.0
            if age_s > 60:
                parts.append(f"[World state is {age_s:.0f}s old.]")

        return " ".join(parts) if parts else "No sensor data available."

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_current_state(self) -> Dict[str, Any]:
        """Return a serializable snapshot of current world state."""
        now_ms = int(time.time() * 1000)
        self.staleness = (now_ms - self.last_updated) / 1000.0 if self.last_updated else 0.0

        active_entities = []
        for entity in self.entity_states.values():
            d = entity.to_dict()
            d["confidence"] = entity.current_confidence(now_ms)
            active_entities.append(d)

        last_known_list = []
        for entity in self.last_known.values():
            d = entity.to_dict()
            d["confidence"] = entity.current_confidence(now_ms)
            last_known_list.append(d)

        return {
            "device_id": self.device_id,
            "last_updated_ms": self.last_updated,
            "staleness_s": round(self.staleness, 1),
            "perspective": self.perspective,
            "narrative": self.narrative,
            "active_entities": active_entities,
            "last_known_entities": last_known_list,
            "history_depth": len(self.history),
            "awareness_level": getattr(self.current_context, "awareness_level", 0.0)
                if self.current_context else 0.0,
        }

    def get_narrative(self) -> str:
        """Return current narrative (rebuilt on last update)."""
        if not self.last_updated:
            return "No sensor data received yet."
        now_ms = int(time.time() * 1000)
        age_s = (now_ms - self.last_updated) / 1000.0
        suffix = f" [Last updated {age_s:.0f}s ago.]" if age_s > 10 else ""
        return self.narrative + suffix

    def get_active_entity_count(self, entity_type: Optional[str] = None) -> int:
        """Count active entities, optionally filtered by type."""
        if entity_type is None:
            return len(self.entity_states)
        return sum(1 for e in self.entity_states.values() if e.entity_type == entity_type)

    def get_ble_macs_in_range(self) -> List[str]:
        """Return MACs of BLE devices currently tracked (confidence >= 0.2)."""
        return [
            eid.replace("ble:", "")
            for eid, e in self.entity_states.items()
            if e.entity_type == "device" and eid.startswith("ble:")
            and not eid.startswith("ble:known:")
        ]

    def is_speech_active(self) -> bool:
        """Return True if audio speech entity is currently active."""
        entity = self.entity_states.get("person:audio:0")
        return entity is not None and entity.current_confidence() >= CONFIDENCE_FLOOR

    def get_lighting(self) -> str:
        """Return current lighting from world model."""
        entity = self.entity_states.get("ambient:visual")
        if entity is None:
            return "unknown"
        ctx = entity.last_context
        for level in ("bright", "dim", "dark"):
            if level in ctx:
                return level
        return "unknown"
