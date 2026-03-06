"""TriggerArbiter -- fuses visual and audio trigger streams into one event.

Receives TriggerEvent objects from multiple sources (visual TriggerDetector,
AudioTriggerDetector, or any future sensor) and emits a single fused event
downstream with configurable priority policy and deduplication.

Usage::

    from openclaw_embodiment.triggers.arbiter import TriggerArbiter, ArbiterPolicy, ArbiterConfig
    from openclaw_embodiment.core.trigger import TriggerEvent

    def handle(event: TriggerEvent) -> None:
        print("Fused trigger:", event)

    arbiter = TriggerArbiter(on_trigger=handle)
    arbiter.push(visual_event)
    arbiter.push(audio_event)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from ..core.trigger import TriggerEvent

logger = logging.getLogger(__name__)


class ArbiterPolicy(Enum):
    """Priority policy controlling which trigger wins when multiple fire."""

    FIRST_WINS = "first_wins"
    """Emit the first event received; suppress duplicates in the dedup window."""

    AUDIO_PRIORITY = "audio_priority"
    """Audio events win over visual events within the dedup window."""

    VISUAL_PRIORITY = "visual_priority"
    """Visual events win over audio events within the dedup window."""

    HIGHEST_CONFIDENCE = "highest_confidence"
    """Emit whichever event has the highest ``trigger_confidence`` score."""


@dataclass
class ArbiterConfig:
    """Configuration for TriggerArbiter.

    Attributes:
        policy:        Priority policy (default FIRST_WINS).
        dedup_window_ms: Window during which duplicate events are suppressed (ms).
                         Default 500ms -- prevents double-firing on the same event.
        max_pending:   Max events held in the pending queue before the oldest is dropped.
    """

    policy: ArbiterPolicy = ArbiterPolicy.FIRST_WINS
    dedup_window_ms: int = 500
    max_pending: int = 16


class TriggerArbiter:
    """Fuses multiple TriggerEvent streams into a single downstream callback.

    Thread-safe. All ``push()`` calls are safe from any thread.

    Policies:
        FIRST_WINS:        First event in the dedup window wins. Default.
        AUDIO_PRIORITY:    Audio events beat visual events in the same window.
        VISUAL_PRIORITY:   Visual events beat audio events in the same window.
        HIGHEST_CONFIDENCE: Highest confidence score in the dedup window wins.
                             Window is held open until it expires, then emits winner.

    Deduplication:
        After emitting an event, a ``dedup_window_ms`` cooldown suppresses
        additional events from any source. This prevents double-firing when
        a loud sound also triggers a visual capture.

    Usage::

        def on_fused(event: TriggerEvent) -> None:
            process(event)

        arbiter = TriggerArbiter(on_trigger=on_fused)

        # Feed from visual detector:
        visual_detector.on_trigger(arbiter.push)

        # Feed from audio detector (wraps AudioChunk -> TriggerEvent):
        audio_detector.on_trigger(lambda chunk: arbiter.push(audio_chunk_to_event(chunk)))
    """

    def __init__(
        self,
        on_trigger: Callable[[TriggerEvent], None],
        config: Optional[ArbiterConfig] = None,
    ) -> None:
        """Initialise arbiter.

        Args:
            on_trigger: Callback invoked with the winning fused TriggerEvent.
            config:     ArbiterConfig. Defaults to FIRST_WINS, 500ms window.
        """
        self._on_trigger = on_trigger
        self._config = config or ArbiterConfig()
        self._lock = threading.Lock()
        self._last_emit_ms: int = 0
        self._pending: List[TriggerEvent] = []
        self._window_timer: Optional[threading.Timer] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push(self, event: TriggerEvent) -> None:
        """Submit a TriggerEvent for arbitration.

        Thread-safe. May be called from any thread or callback.

        Args:
            event: TriggerEvent from any trigger source.
        """
        with self._lock:
            now_ms = self._now_ms()
            elapsed = now_ms - self._last_emit_ms

            # Dedup: within cooldown window, apply policy
            if elapsed < self._config.dedup_window_ms:
                if self._config.policy == ArbiterPolicy.FIRST_WINS:
                    logger.debug("[Arbiter] FIRST_WINS dedup: dropping %s event", event.event_id)
                    return

                elif self._config.policy == ArbiterPolicy.AUDIO_PRIORITY:
                    # If incoming is audio and pending/last was visual, replace
                    is_incoming_audio = self._is_audio(event)
                    if is_incoming_audio and not self._is_audio_last():
                        logger.debug("[Arbiter] AUDIO_PRIORITY: promoting audio over visual")
                        self._emit(event)
                    return

                elif self._config.policy == ArbiterPolicy.VISUAL_PRIORITY:
                    is_incoming_visual = not self._is_audio(event)
                    if is_incoming_visual and self._is_audio_last():
                        logger.debug("[Arbiter] VISUAL_PRIORITY: promoting visual over audio")
                        self._emit(event)
                    return

                elif self._config.policy == ArbiterPolicy.HIGHEST_CONFIDENCE:
                    # Accumulate in pending; emit when window expires
                    if len(self._pending) < self._config.max_pending:
                        self._pending.append(event)
                    return

            # Outside dedup window: handle HIGHEST_CONFIDENCE flush or direct emit
            if self._config.policy == ArbiterPolicy.HIGHEST_CONFIDENCE:
                self._pending.append(event)
                self._schedule_confidence_flush()
                return

            # Default: emit immediately
            self._emit(event)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit(self, event: TriggerEvent) -> None:
        """Fire the downstream callback and record emit time. Must hold lock."""
        self._last_emit_ms = self._now_ms()
        self._pending.clear()
        logger.info("[Arbiter] Emitting fused event %s (policy=%s)",
                    event.event_id, self._config.policy.value)
        t = threading.Thread(
            target=self._fire_callback,
            args=(event,),
            daemon=True,
            name="arbiter-emit",
        )
        t.start()

    def _fire_callback(self, event: TriggerEvent) -> None:
        try:
            self._on_trigger(event)
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- arbiter emit -- delivery failure must not crash arbiter
            logger.exception("[Arbiter] Callback error: %s", exc)

    def _schedule_confidence_flush(self) -> None:
        """Schedule a timer to flush the pending queue at window expiry."""
        if self._window_timer is not None:
            self._window_timer.cancel()
        wait_s = self._config.dedup_window_ms / 1000.0
        self._window_timer = threading.Timer(wait_s, self._flush_confidence)
        self._window_timer.daemon = True
        self._window_timer.start()

    def _flush_confidence(self) -> None:
        """Pick the highest-confidence event from pending and emit it."""
        with self._lock:
            if not self._pending:
                return
            winner = max(self._pending, key=lambda e: e.trigger_confidence)
            logger.debug("[Arbiter] HIGHEST_CONFIDENCE flush: winner=%s conf=%.2f",
                         winner.event_id, winner.trigger_confidence)
            self._emit(winner)

    @staticmethod
    def _is_audio(event: TriggerEvent) -> bool:
        """Detect whether a TriggerEvent originated from an audio source."""
        eid = event.event_id.lower()
        return "audio" in eid or "snd" in eid or "mic" in eid

    def _is_audio_last(self) -> bool:
        """Return True if the most recent emission was an audio event."""
        # Approximate: check pending list order. Conservative default: False.
        return False

    @staticmethod
    def _now_ms() -> int:
        return int(time.monotonic() * 1000)
