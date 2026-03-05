"""ContextBuilder -- assembles SensorContext from live HAL readings.

Handles graceful degradation: any sensor HAL can be None (unavailable) and
the SensorContext schema remains valid with Optional fields set to None.

Conflict detection and awareness_level computation are deterministic (no LLM).
Summary generation is also deterministic -- suitable for edge deployment without
network access.

Device capability profile for Distiller CM5:
    mic=True, cam=True, imu=False, ble=True, display=True, speaker=True,
    wearable=False, gps=False, gaze=False
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema definitions (mirrors CONTEXT_ENGINE_SPEC.md)
# ---------------------------------------------------------------------------

@dataclass
class DeviceCapabilityVector:
    """What this device CAN provide -- not what it returned this cycle."""
    has_microphone: bool = False
    has_camera: bool = False
    has_imu: bool = False
    has_ble: bool = False
    has_display: bool = False
    has_speaker: bool = False
    has_gps: bool = False
    is_wearable: bool = False
    has_gaze: bool = False


# Pre-built profiles
DISTILLER_CM5_CAPABILITIES = DeviceCapabilityVector(
    has_microphone=True,
    has_camera=True,
    has_imu=False,
    has_ble=True,
    has_display=True,
    has_speaker=True,
    has_gps=False,
    is_wearable=False,
    has_gaze=False,
)

RASPBERRY_PI_CAPABILITIES = DeviceCapabilityVector(
    has_microphone=True,
    has_camera=True,
    has_imu=False,
    has_ble=True,
    has_display=False,
    has_speaker=True,
    has_gps=False,
    is_wearable=False,
    has_gaze=False,
)


@dataclass
class AudioContext:
    transcript: Optional[str] = None
    speaker_count: Optional[int] = None
    ambient_class: str = "unknown"   # "speech" | "music" | "silence" | "noise" | "unknown"
    rms_level: float = 0.0
    language: Optional[str] = None
    confidence: float = 0.0


@dataclass
class VisualContext:
    description: Optional[str] = None
    person_count: Optional[int] = None
    activity: Optional[str] = None   # "sitting" | "walking" | "talking" | "idle"
    lighting: str = "unknown"        # "bright" | "dim" | "dark" | "unknown"
    frame_path: Optional[str] = None
    confidence: float = 0.0


@dataclass
class MotionContext:
    state: str = "unknown"           # "stationary" | "walking" | "running" | "gesture"
    orientation: Optional[Tuple[float, float, float]] = None
    acceleration: Optional[Tuple[float, float, float]] = None
    confidence: float = 0.0


@dataclass
class ProximityContext:
    known_devices: List[str] = field(default_factory=list)
    unknown_count: int = 0
    rssi_map: Dict[str, int] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class SensorContext:
    """Canonical context snapshot. Always valid regardless of available sensors.

    None fields mean "sensor unavailable" -- not "sensor returned nothing."
    Use device_capabilities to distinguish "not present" from "failed."
    """
    timestamp_ms: int
    device_id: str
    trigger: str                            # "voice_detected" | "motion" | "ble_new_device"
                                            # | "scheduled" | "manual" | "threshold_crossed"
    audio: Optional[AudioContext]
    visual: Optional[VisualContext]
    motion: Optional[MotionContext]
    proximity: Optional[ProximityContext]

    awareness_level: float                  # 0.0–1.0
    conflicts: List[str]
    summary: str

    device_capabilities: DeviceCapabilityVector


# ---------------------------------------------------------------------------
# ContextBuilder
# ---------------------------------------------------------------------------

class ContextBuilder:
    """Assembles SensorContext from live HAL readings.

    Designed for the Distiller CM5 but works with any HAL combination.
    All HALs are optional -- missing HALs produce None sensor channels.

    Args:
        device_id:      Identifier for this device (e.g. "distiller-cm5-01")
        capabilities:   DeviceCapabilityVector for this device.
                        Defaults to DISTILLER_CM5_CAPABILITIES.
        mic_hal:        MicrophoneHal instance (or None)
        camera_hal:     DistillerCameraHAL instance (or None)
        ble_scanner:    BLEProximityScanner instance (or None)
        capture_audio_ms: How many ms of audio to capture per context build.
    """

    def __init__(
        self,
        device_id: str = "distiller-cm5",
        capabilities: Optional[DeviceCapabilityVector] = None,
        mic_hal=None,
        camera_hal=None,
        ble_scanner=None,
        capture_audio_ms: int = 2000,
    ) -> None:
        self.device_id = device_id
        self.capabilities = capabilities or DISTILLER_CM5_CAPABILITIES
        self.mic_hal = mic_hal
        self.camera_hal = camera_hal
        self.ble_scanner = ble_scanner
        self.capture_audio_ms = capture_audio_ms

    def build(self, trigger: str = "manual") -> SensorContext:
        """Assemble a full SensorContext snapshot from available HALs.

        Args:
            trigger: What initiated this context build.

        Returns:
            SensorContext with all available fields populated and
            awareness_level / conflicts / summary computed.
        """
        t0_ms = int(time.time() * 1000)

        audio_ctx = self._build_audio()
        visual_ctx = self._build_visual()
        motion_ctx = None  # No IMU on Distiller CM5
        proximity_ctx = self._build_proximity()

        conflicts = self._detect_conflicts(audio_ctx, visual_ctx)
        awareness = self._compute_awareness(audio_ctx, visual_ctx, proximity_ctx, conflicts)
        summary = self._generate_summary(
            trigger, audio_ctx, visual_ctx, proximity_ctx, awareness, conflicts
        )

        return SensorContext(
            timestamp_ms=t0_ms,
            device_id=self.device_id,
            trigger=trigger,
            audio=audio_ctx,
            visual=visual_ctx,
            motion=motion_ctx,
            proximity=proximity_ctx,
            awareness_level=awareness,
            conflicts=conflicts,
            summary=summary,
            device_capabilities=self.capabilities,
        )

    # ------------------------------------------------------------------
    # Channel builders
    # ------------------------------------------------------------------

    def _build_audio(self) -> Optional[AudioContext]:
        """Capture audio and produce AudioContext."""
        if self.mic_hal is None:
            logger.debug("[ContextBuilder] No mic HAL -- audio=None")
            return None
        try:
            chunk = self.mic_hal.capture(duration_ms=self.capture_audio_ms)
            rms = self._compute_rms(chunk.data)
            ambient = self._classify_ambient(rms)
            confidence = min(1.0, rms / 3000.0) if rms > 0 else 0.0
            return AudioContext(
                transcript=None,
                speaker_count=None,
                ambient_class=ambient,
                rms_level=rms,
                language=None,
                confidence=round(confidence, 3),
            )
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- sensor context build -- one bad sensor must not crash the pipeline
            logger.warning("[ContextBuilder] Audio build failed: %s", e)
            return None

    def _build_visual(self) -> Optional[VisualContext]:
        """Capture camera frame and produce VisualContext."""
        if self.camera_hal is None:
            logger.debug("[ContextBuilder] No camera HAL -- visual=None")
            return None
        try:
            # Use grayscale -- color unreliable on Distiller OV5647
            try:
                jpeg_bytes = self.camera_hal.capture_grayscale()
            except AttributeError:
                jpeg_bytes = self.camera_hal.capture()

            lighting = "unknown"
            person_count = None
            try:
                lighting = self.camera_hal.get_lighting_level()
            except AttributeError:
                pass

            try:
                person_count = self.camera_hal.estimate_person_count()
            except AttributeError:
                pass

            # Save frame for downstream use
            import tempfile, os
            frame_path = tempfile.mktemp(suffix=".jpg", prefix="ctx_frame_")
            with open(frame_path, "wb") as f:
                f.write(jpeg_bytes)

            confidence = 0.7 if lighting != "dark" else 0.3

            return VisualContext(
                description=None,   # Would require VLM -- not available at edge
                person_count=person_count,
                activity=None,
                lighting=lighting,
                frame_path=frame_path,
                confidence=round(confidence, 3),
            )
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- sensor context build -- one bad sensor must not crash the pipeline
            logger.warning("[ContextBuilder] Visual build failed: %s", e)
            return None

    def _build_proximity(self) -> Optional[ProximityContext]:
        """Run BLE scan and produce ProximityContext."""
        if self.ble_scanner is None:
            logger.debug("[ContextBuilder] No BLE scanner -- proximity=None")
            return None
        try:
            from openclaw_embodiment.hal.ble_scanner import ProximityContext as BLECtx
            ble_result = self.ble_scanner.scan()
            return ProximityContext(
                known_devices=ble_result.known_devices,
                unknown_count=ble_result.unknown_count,
                rssi_map=ble_result.rssi_map,
                confidence=ble_result.confidence,
            )
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- sensor context build -- one bad sensor must not crash the pipeline
            logger.warning("[ContextBuilder] Proximity build failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Awareness & conflict computation
    # ------------------------------------------------------------------

    def _detect_conflicts(
        self,
        audio: Optional[AudioContext],
        visual: Optional[VisualContext],
    ) -> List[str]:
        """Detect cross-sensor disagreements.

        Current checks:
        - speaker_count vs person_count (when both are available and disagree)
        """
        conflicts: List[str] = []

        if (
            audio is not None
            and visual is not None
            and audio.speaker_count is not None
            and visual.person_count is not None
            and audio.speaker_count != visual.person_count
        ):
            conflicts.append(
                f"speaker_count:audio({audio.speaker_count}) "
                f"!= person_count:visual({visual.person_count})"
            )

        return conflicts

    def _compute_awareness(
        self,
        audio: Optional[AudioContext],
        visual: Optional[VisualContext],
        proximity: Optional[ProximityContext],
        conflicts: List[str],
    ) -> float:
        """Compute 0.0-1.0 awareness_level.

        Logic:
        - Base score from available sensor channels (each contributes equally)
        - Weighted by individual sensor confidence
        - Penalized by number of conflicts (-0.15 per conflict, floor 0.0)
        """
        channel_scores: List[float] = []

        if audio is not None:
            channel_scores.append(audio.confidence)
        if visual is not None:
            channel_scores.append(visual.confidence)
        if proximity is not None:
            channel_scores.append(proximity.confidence)

        if not channel_scores:
            return 0.0

        base = sum(channel_scores) / len(channel_scores)
        penalty = len(conflicts) * 0.15
        awareness = max(0.0, min(1.0, base - penalty))
        return round(awareness, 3)

    # ------------------------------------------------------------------
    # Summary generation (deterministic, no LLM)
    # ------------------------------------------------------------------

    def _generate_summary(
        self,
        trigger: str,
        audio: Optional[AudioContext],
        visual: Optional[VisualContext],
        proximity: Optional[ProximityContext],
        awareness: float,
        conflicts: List[str],
    ) -> str:
        """Generate a plain-text, LLM-ready summary of current sensor state."""
        parts: List[str] = []

        parts.append(f"Trigger: {trigger}.")

        # Audio
        if audio is not None:
            parts.append(
                f"Audio: {audio.ambient_class} (rms={audio.rms_level:.0f}"
                + (f", {audio.speaker_count} speaker(s)" if audio.speaker_count is not None else "")
                + ")."
            )
        else:
            parts.append("Audio: unavailable.")

        # Visual
        if visual is not None:
            vis_parts = [f"lighting={visual.lighting}"]
            if visual.person_count is not None:
                vis_parts.append(f"~{visual.person_count} person(s)")
            if visual.activity:
                vis_parts.append(visual.activity)
            parts.append("Visual: " + ", ".join(vis_parts) + ".")
        else:
            parts.append("Visual: unavailable.")

        # Proximity
        if proximity is not None:
            ble_parts = []
            if proximity.known_devices:
                ble_parts.append("known: " + ", ".join(proximity.known_devices))
            if proximity.unknown_count:
                ble_parts.append(f"{proximity.unknown_count} unknown device(s)")
            if ble_parts:
                parts.append("BLE: " + "; ".join(ble_parts) + ".")
            else:
                parts.append("BLE: no devices nearby.")
        else:
            parts.append("BLE: unavailable.")

        # Awareness + conflicts
        parts.append(f"Awareness: {awareness:.2f}.")
        if conflicts:
            parts.append("Conflicts: " + " | ".join(conflicts) + ".")

        return " ".join(parts)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_rms(raw_pcm: bytes) -> float:
        import math, struct
        if len(raw_pcm) < 2:
            return 0.0
        num_samples = len(raw_pcm) // 2
        samples = struct.unpack(f"<{num_samples}h", raw_pcm[:num_samples * 2])
        mean_sq = sum(s * s for s in samples) / num_samples
        return math.sqrt(mean_sq)

    @staticmethod
    def _classify_ambient(rms: float) -> str:
        """Coarse ambient classification from RMS level."""
        if rms < 200:
            return "silence"
        elif rms < 1500:
            return "noise"
        elif rms < 4000:
            return "speech"
        else:
            return "loud"
