"""OpenClaw Embodiment SDK -- Autonomous Spatial Discovery System.

This package provides the autonomous discovery runtime for stationary devices
(e.g. Distiller CM5). It learns what's normal about a space over time and
detects anomalies when things change.

Components:
    SpaceModel        -- Persistent SQLite-backed knowledge store
    WorldModel        -- Rolling entity state with confidence decay
    AnomalyDetector   -- Fires callbacks when baseline changes
    DiscoveryReport   -- Synthesized report of space knowledge
    DiscoveryLoop     -- Autonomous sensing runtime (replaces reactive mode)
"""

from .space_model import SpaceModel
from .world_model import WorldModel, EntityState
from .anomaly_detector import AnomalyDetector, AnomalyEvent, AnomalyType
from .discovery_report import DiscoveryReport, build_report

__all__ = [
    "SpaceModel",
    "WorldModel",
    "EntityState",
    "AnomalyDetector",
    "AnomalyEvent",
    "AnomalyType",
    "DiscoveryReport",
    "build_report",
]
