"""Context Engine v0.3 - ContextBuilder.

Assembles SensorContext from available HAL readings with:
- Graceful degradation when sensors are missing
- Deterministic summary generation (no LLM round-trip)
- awareness_level calculation based on sensor count + conflict detection
- Conflict detection when sensors disagree
"""

from __future__ import annotations

import time
from typing import List, Optional

from .sensor_context import (
    AudioContext,
    DeviceCapabilityVector,
    MotionContext,
    ProximityContext,
    SensorContext,
    VisualContext,
)


class ContextBuilder:
    """Assembles SensorContext from available HAL readings.
    
    Key behaviors:
    - Takes whatever sensors are available, produces consistent SensorContext
    - Generates summary deterministically (no LLM round-trip for v0.3)
    - Calculates awareness_level based on sensor count + conflict detection
    - Detects conflicts when sensors disagree
    
    Usage:
        from openclaw_embodiment.context import ContextBuilder, DISTILLER_CM5
        
        builder = ContextBuilder(device_id="distiller-3aff", capabilities=DISTILLER_CM5)
        context = builder.build(
            trigger="voice_detected",
            audio=AudioContext(...),
            visual=None,   # camera not captured this time
            motion=None,
        )
    """
    
    def __init__(
        self,
        device_id: str,
        capabilities: Optional[DeviceCapabilityVector] = None,
    ) -> None:
        self.device_id = device_id
        self.capabilities = capabilities or DeviceCapabilityVector()
    
    def build(
        self,
        trigger: str,
        audio: Optional[AudioContext] = None,
        visual: Optional[VisualContext] = None,
        motion: Optional[MotionContext] = None,
        proximity: Optional[ProximityContext] = None,
        timestamp_ms: Optional[int] = None,
    ) -> SensorContext:
        """Build a SensorContext from available sensor readings.
        
        Args:
            trigger: What triggered this context capture
            audio: Audio sensor context (None if unavailable)
            visual: Visual sensor context (None if unavailable)
            motion: Motion sensor context (None if unavailable)
            proximity: Proximity sensor context (None if unavailable)
            timestamp_ms: Explicit timestamp; defaults to current time
            
        Returns:
            A fully populated SensorContext
        """
        if timestamp_ms is None:
            timestamp_ms = int(time.time() * 1000)
        
        # Detect conflicts first (affects awareness calculation)
        conflicts = self._detect_conflicts(audio, visual, motion, proximity)
        
        awareness_level = self._calculate_awareness_level(
            audio, visual, motion, proximity, conflicts
        )
        
        summary = self._generate_summary(
            trigger, audio, visual, motion, proximity, awareness_level, conflicts
        )
        
        return SensorContext(
            timestamp_ms=timestamp_ms,
            device_id=self.device_id,
            trigger=trigger,
            audio=audio,
            visual=visual,
            motion=motion,
            proximity=proximity,
            awareness_level=awareness_level,
            conflicts=conflicts,
            summary=summary,
            device_capabilities=self.capabilities,
        )
    
    def _calculate_awareness_level(
        self,
        audio: Optional[AudioContext],
        visual: Optional[VisualContext],
        motion: Optional[MotionContext],
        proximity: Optional[ProximityContext],
        conflicts: List[str],
    ) -> float:
        """Calculate awareness level based on sensor availability and conflicts.
        
        Scoring:
        - Base: 0.25 per sensor channel present
        - Penalty: -0.15 per conflict detected
        - Bonus: +0.1 if multiple sensors corroborate (e.g. audio and visual
          agree on person count)
        
        Returns:
            Float between 0.0 and 1.0
        """
        level = 0.0
        
        # Base: 0.25 per sensor channel present
        if audio is not None:
            level += 0.25
        if visual is not None:
            level += 0.25
        if motion is not None:
            level += 0.25
        if proximity is not None:
            level += 0.25
        
        # Penalty: -0.15 per conflict
        level -= 0.15 * len(conflicts)
        
        # Bonus: +0.1 if sensors corroborate
        if self._sensors_corroborate(audio, visual, motion, proximity):
            level += 0.1
        
        # Clamp to [0.0, 1.0]
        return max(0.0, min(1.0, level))
    
    def _sensors_corroborate(
        self,
        audio: Optional[AudioContext],
        visual: Optional[VisualContext],
        motion: Optional[MotionContext],
        proximity: Optional[ProximityContext],
    ) -> bool:
        """Check if multiple sensors corroborate each other.
        
        Corroboration examples:
        - audio.speaker_count matches visual.person_count
        - motion.state == "walking" and audio.ambient_class != "silence"
        - audio detects speech and visual detects person_count > 0
        """
        corroborations = 0
        
        # Audio speaker count matches visual person count
        if (
            audio is not None
            and visual is not None
            and audio.speaker_count is not None
            and visual.person_count is not None
        ):
            if audio.speaker_count == visual.person_count:
                corroborations += 1
            elif (
                audio.speaker_count > 0
                and visual.person_count > 0
                and abs(audio.speaker_count - visual.person_count) <= 1
            ):
                # Close enough -- one person might be silent
                corroborations += 1
        
        # Motion + audio corroboration
        if audio is not None and motion is not None:
            # Walking/running should correlate with non-silence
            if motion.state in ("walking", "running") and audio.ambient_class != "silence":
                corroborations += 1
            # Stationary + speech is coherent
            if motion.state == "stationary" and audio.ambient_class == "speech":
                corroborations += 1
        
        # Speech detected + person visible
        if (
            audio is not None
            and visual is not None
            and audio.ambient_class == "speech"
            and visual.person_count is not None
            and visual.person_count > 0
        ):
            corroborations += 1
        
        return corroborations > 0
    
    def _detect_conflicts(
        self,
        audio: Optional[AudioContext],
        visual: Optional[VisualContext],
        motion: Optional[MotionContext],
        proximity: Optional[ProximityContext],
    ) -> List[str]:
        """Detect conflicts between sensor readings.
        
        Conflicts detected:
        - audio.speaker_count significantly differs from visual.person_count
        - motion.state is active but audio is silent (suspicious)
        """
        conflicts = []
        
        # Check: audio.speaker_count vs visual.person_count
        if (
            audio is not None
            and visual is not None
            and audio.speaker_count is not None
            and visual.person_count is not None
        ):
            # Significant mismatch: more speakers than visible people
            # or more than 1 person difference
            if audio.speaker_count > visual.person_count + 1:
                conflicts.append(
                    f"audio:{audio.speaker_count}_speakers visual:{visual.person_count}_person"
                )
            elif visual.person_count > audio.speaker_count + 2:
                # Many visible people but few speakers -- less suspicious but notable
                conflicts.append(
                    f"visual:{visual.person_count}_people audio:{audio.speaker_count}_speakers"
                )
        
        # Check: motion.state vs audio.ambient_class
        if audio is not None and motion is not None:
            # Running but complete silence is suspicious
            if motion.state == "running" and audio.ambient_class == "silence":
                conflicts.append(f"motion:{motion.state} audio:{audio.ambient_class}")
        
        return conflicts
    
    def _generate_summary(
        self,
        trigger: str,
        audio: Optional[AudioContext],
        visual: Optional[VisualContext],
        motion: Optional[MotionContext],
        proximity: Optional[ProximityContext],
        awareness_level: float,
        conflicts: List[str],
    ) -> str:
        """Generate a deterministic natural-language summary.
        
        NO LLM call -- this must be fast and offline-capable.
        Uses template-based generation with honest uncertainty expression.
        """
        parts = []
        
        # Trigger context
        trigger_phrases = {
            "voice_detected": "Speech detected",
            "motion": "Motion detected",
            "ble_new_device": "New device in range",
            "scheduled": "Scheduled context snapshot",
            "manual": "Manual capture",
            "threshold_crossed": "Sensor threshold crossed",
        }
        trigger_phrase = trigger_phrases.get(trigger, f"Trigger: {trigger}")
        
        # Audio context
        if audio is not None:
            ambient_phrases = {
                "speech": "speech activity",
                "music": "music playing",
                "silence": "quiet environment",
                "noise": "ambient noise",
            }
            ambient = ambient_phrases.get(audio.ambient_class, audio.ambient_class)
            
            if audio.speaker_count is not None and audio.speaker_count > 0:
                speaker_word = "person" if audio.speaker_count == 1 else "people"
                parts.append(f"{audio.speaker_count} {speaker_word} speaking")
            elif audio.ambient_class == "speech":
                parts.append("Speech heard")
            
            if audio.transcript:
                # Don't include full transcript, just note it exists
                parts.append("transcript captured")
            
            if audio.ambient_class != "speech":
                parts.append(ambient)
        
        # Visual context
        if visual is not None:
            if visual.person_count is not None:
                if visual.person_count == 0:
                    parts.append("No people visible")
                elif visual.person_count == 1:
                    parts.append("One person visible")
                else:
                    parts.append(f"{visual.person_count} people visible")
            
            if visual.activity:
                parts.append(f"activity: {visual.activity}")
            
            lighting_phrases = {
                "bright": "well-lit",
                "dim": "dim lighting",
                "dark": "dark environment",
            }
            if visual.lighting in lighting_phrases:
                parts.append(lighting_phrases[visual.lighting])
        
        # Motion context
        if motion is not None:
            motion_phrases = {
                "stationary": "Device stationary",
                "walking": "Walking detected",
                "running": "Running detected",
                "gesture": "Gesture detected",
            }
            parts.append(motion_phrases.get(motion.state, f"Motion: {motion.state}"))
        
        # Proximity context
        if proximity is not None:
            device_count = len(proximity.known_devices) + proximity.unknown_count
            if device_count > 0:
                known = len(proximity.known_devices)
                if known > 0:
                    parts.append(f"{known} known device{'s' if known > 1 else ''} nearby")
                if proximity.unknown_count > 0:
                    parts.append(f"{proximity.unknown_count} unknown device{'s' if proximity.unknown_count > 1 else ''}")
        
        # Handle case with no sensor data
        if audio is None and visual is None and motion is None and proximity is None:
            return f"{trigger_phrase}. No sensor data available."
        
        # Build summary
        if parts:
            summary = f"{trigger_phrase}. {'. '.join(parts)}."
        else:
            summary = f"{trigger_phrase}."
        
        # Add conflict warnings
        if conflicts:
            summary += f" Warning: sensor conflict detected ({len(conflicts)} issue{'s' if len(conflicts) > 1 else ''})."
        
        # Add low-confidence warning
        if awareness_level < 0.3:
            summary += " Low confidence -- limited sensor coverage."
        
        return summary
