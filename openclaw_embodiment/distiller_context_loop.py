"""DistillerContextLoop -- wires audio trigger, context engine, and output HALs.

This is the top-level runtime for the Distiller CM5:

1. AudioTriggerDetector polls the mic continuously
2. On TRIGGER: ContextBuilder assembles a SensorContext snapshot
3. SensorContext is POSTed to the OpenClaw gateway
4. Gateway response is rendered to e-ink + spoken via TTS
5. Graceful degradation if camera or BLE are unavailable

CLI::

    python -m openclaw_embodiment.distiller_context_loop \\
        --gateway http://192.168.1.183:18799

    # Or with known BLE devices:
    python -m openclaw_embodiment.distiller_context_loop \\
        --gateway http://192.168.1.183:18799 \\
        --known-ble aa:bb:cc:dd:ee:ff=MikePendant

    # Disable camera (e.g., hardware not connected):
    python -m openclaw_embodiment.distiller_context_loop \\
        --gateway http://192.168.1.183:18799 \\
        --no-camera
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import threading
import time
import urllib.request
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class DistillerContextLoop:
    """Main runtime loop for the Distiller CM5 embodiment agent.

    Args:
        gateway_url:        OpenClaw gateway base URL. POST target will be
                            ``<gateway_url>/context/ingest``.
        enable_camera:      Enable OV5647 camera HAL.
        enable_ble:         Enable BLE proximity scanner.
        known_ble:          Known BLE device map {mac: name}.
        ble_scan_duration:  BLE scan duration (seconds) per context build.
        audio_threshold_rms: RMS threshold for AudioTriggerDetector.
        audio_min_duration_ms: Min sustained audio duration before trigger fires.
        audio_cooldown_ms:  Cooldown after trigger fires.
        device_id:          Device identifier sent with context payloads.
        log_level:          Python logging level (e.g., logging.INFO).
    """

    GATEWAY_ENDPOINT = "/context/ingest"

    def __init__(
        self,
        gateway_url: str = "http://localhost:18799",
        enable_camera: bool = True,
        enable_ble: bool = True,
        known_ble: Optional[Dict[str, str]] = None,
        ble_scan_duration: float = 3.0,
        audio_threshold_rms: float = 800.0,
        audio_min_duration_ms: int = 300,
        audio_cooldown_ms: int = 2000,
        device_id: str = "distiller-cm5",
        log_level: int = logging.INFO,
    ) -> None:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
        self.gateway_url = gateway_url.rstrip("/")
        self.device_id = device_id
        self._stop_event = threading.Event()

        # Initialize HALs with graceful fallback
        self.mic_hal = self._init_mic()
        self.camera_hal = self._init_camera() if enable_camera else None
        self.display_hal = self._init_display()
        self.audio_out_hal = self._init_audio_out()
        self.ble_scanner = self._init_ble(known_ble or {}, ble_scan_duration) if enable_ble else None

        # ContextBuilder
        from openclaw_embodiment.core.context_builder import ContextBuilder, DISTILLER_CM5_CAPABILITIES
        self.context_builder = ContextBuilder(
            device_id=device_id,
            capabilities=DISTILLER_CM5_CAPABILITIES,
            mic_hal=self.mic_hal,
            camera_hal=self.camera_hal,
            ble_scanner=self.ble_scanner,
        )

        # AudioTriggerDetector
        from openclaw_embodiment.triggers.audio_trigger import (
            AudioTriggerDetector, AudioTriggerConfig,
        )
        self.trigger_detector = AudioTriggerDetector(
            on_trigger=self._on_audio_trigger,
            config=AudioTriggerConfig(
                threshold_rms=audio_threshold_rms,
                min_duration_ms=audio_min_duration_ms,
                cooldown_ms=audio_cooldown_ms,
            ),
        )

    # ------------------------------------------------------------------
    # HAL initialization (each returns None on failure -- graceful degradation)
    # ------------------------------------------------------------------

    def _init_mic(self):
        try:
            from openclaw_embodiment.hal.distiller_reference import DistillerMicrophoneHAL
            hal = DistillerMicrophoneHAL()
            hal.initialize()
            logger.info("[ContextLoop] Mic HAL initialized.")
            return hal
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- context loop resilience -- one bad read must not stop the daemon
            logger.warning("[ContextLoop] Mic HAL unavailable: %s", e)
            return None

    def _init_camera(self):
        try:
            from openclaw_embodiment.hal.distiller_reference import DistillerCameraHAL
            hal = DistillerCameraHAL()
            hal.initialize()
            logger.info("[ContextLoop] Camera HAL initialized (color_reliable=%s).",
                        hal.color_reliable)
            return hal
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- context loop resilience -- one bad read must not stop the daemon
            logger.warning("[ContextLoop] Camera HAL unavailable: %s", e)
            return None

    def _init_display(self):
        try:
            from openclaw_embodiment.hal.distiller_reference import DistillerEinkDisplayHAL
            hal = DistillerEinkDisplayHAL()
            hal.initialize()
            logger.info("[ContextLoop] E-ink display HAL initialized.")
            return hal
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- context loop resilience -- one bad read must not stop the daemon
            logger.warning("[ContextLoop] Display HAL unavailable: %s", e)
            return None

    def _init_audio_out(self):
        try:
            from openclaw_embodiment.hal.distiller_reference import DistillerAudioOutputHAL
            hal = DistillerAudioOutputHAL()
            hal.initialize()
            logger.info("[ContextLoop] Audio output HAL initialized.")
            return hal
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- context loop resilience -- one bad read must not stop the daemon
            logger.warning("[ContextLoop] Audio output HAL unavailable: %s", e)
            return None

    def _init_ble(self, known_map: Dict[str, str], duration: float):
        try:
            from openclaw_embodiment.hal.ble_scanner import BLEProximityScanner
            scanner = BLEProximityScanner(known_map=known_map, scan_duration_s=duration)
            logger.info("[ContextLoop] BLE scanner initialized (%d known devices).",
                        len(known_map))
            return scanner
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- context loop resilience -- one bad read must not stop the daemon
            logger.warning("[ContextLoop] BLE scanner unavailable: %s", e)
            return None

    # ------------------------------------------------------------------
    # Trigger handler
    # ------------------------------------------------------------------

    def _on_audio_trigger(self, chunk) -> None:
        """Called by AudioTriggerDetector when voice activity detected."""
        logger.info("[ContextLoop] Audio trigger fired -- building context...")
        try:
            ctx = self.context_builder.build(trigger="voice_detected")
            logger.info("[ContextLoop] SensorContext built: awareness=%.2f conflicts=%d",
                        ctx.awareness_level, len(ctx.conflicts))

            # POST to gateway
            response_text = self._post_to_gateway(ctx)

            if response_text:
                logger.info("[ContextLoop] Gateway response: %s", response_text[:100])
                self._render_response(response_text, ctx)
            else:
                logger.warning("[ContextLoop] No response from gateway.")
                self._render_local_fallback(ctx)

        except Exception as e:  # grain: ignore NAKED_EXCEPT -- context loop resilience -- one bad read must not stop the daemon
            logger.error("[ContextLoop] Trigger handler error: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # Gateway communication
    # ------------------------------------------------------------------

    def _post_to_gateway(self, ctx) -> Optional[str]:
        """POST SensorContext to gateway. Returns response text or None."""
        url = self.gateway_url + self.GATEWAY_ENDPOINT
        try:
            payload = self._context_to_dict(ctx)
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("response") or data.get("text") or str(data)
        except urllib.error.URLError as e:
            logger.warning("[ContextLoop] Gateway unreachable (%s): %s", url, e)
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- context loop resilience -- one bad read must not stop the daemon
            logger.error("[ContextLoop] Gateway POST error: %s", e)
        return None

    @staticmethod
    def _context_to_dict(ctx) -> dict:
        """Serialize SensorContext to JSON-safe dict."""
        d = dataclasses.asdict(ctx)
        # Remove large binary fields if present (frame bytes don't serialize well)
        if d.get("visual") and d["visual"].get("frame_bytes"):
            d["visual"]["frame_bytes"] = None
        return d

    # ------------------------------------------------------------------
    # Output rendering
    # ------------------------------------------------------------------

    def _render_response(self, text: str, ctx) -> None:
        """Render gateway response to e-ink + TTS."""
        threads = []

        if self.display_hal is not None:
            from openclaw_embodiment.hal.base import DisplayCard
            card = DisplayCard(
                mode="text",
                title="Agent",
                body=text[:200],
                font_size=13,
                duration_ms=0,
            )
            t = threading.Thread(target=self.display_hal.show, args=(card,), daemon=True)
            threads.append(t)

        if self.audio_out_hal is not None:
            t = threading.Thread(target=self.audio_out_hal.speak, args=(text[:500],), daemon=True)
            threads.append(t)

        for t in threads:
            t.start()

    def _render_local_fallback(self, ctx) -> None:
        """Render local summary when gateway is unavailable."""
        summary_short = ctx.summary[:200]
        logger.info("[ContextLoop] Local fallback: %s", summary_short)

        if self.display_hal is not None:
            from openclaw_embodiment.hal.base import DisplayCard
            try:
                self.display_hal.show(DisplayCard(
                    mode="text",
                    title="Offline",
                    body=f"Awareness: {ctx.awareness_level:.0%}\n{summary_short[:150]}",
                    font_size=12,
                    duration_ms=0,
                ))
            except Exception as e:  # grain: ignore NAKED_EXCEPT -- context loop resilience -- one bad read must not stop the daemon
                logger.warning("[ContextLoop] Display fallback failed: %s", e)

    # ------------------------------------------------------------------
    # Run / stop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the trigger detector and block until stop() is called."""
        logger.info("[ContextLoop] Starting Distiller Context Loop.")
        logger.info("[ContextLoop] Gateway: %s", self.gateway_url)
        logger.info("[ContextLoop] Mic=%s Camera=%s BLE=%s Display=%s Audio=%s",
                    self.mic_hal is not None,
                    self.camera_hal is not None,
                    self.ble_scanner is not None,
                    self.display_hal is not None,
                    self.audio_out_hal is not None)

        if self.mic_hal is None:
            logger.error("[ContextLoop] Microphone unavailable -- cannot run. Exiting.")
            return

        self.trigger_detector.start()
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("[ContextLoop] KeyboardInterrupt -- stopping.")
        finally:
            self.stop()

    def stop(self) -> None:
        """Gracefully stop the context loop."""
        self._stop_event.set()
        self.trigger_detector.stop()
        logger.info("[ContextLoop] Stopped.")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distiller CM5 Context Loop -- OpenClaw Embodiment SDK"
    )
    parser.add_argument("--gateway", default="http://localhost:18799",
                        help="OpenClaw gateway URL (default: http://localhost:18799)")
    parser.add_argument("--no-camera", action="store_true",
                        help="Disable camera HAL")
    parser.add_argument("--no-ble", action="store_true",
                        help="Disable BLE scanner")
    parser.add_argument("--known-ble", nargs="*", default=[], metavar="MAC=NAME",
                        help="Known BLE devices, e.g. aa:bb:cc:dd:ee:ff=MyPendant")
    parser.add_argument("--threshold-rms", type=float, default=800.0,
                        help="RMS threshold for voice activity (default: 800)")
    parser.add_argument("--min-duration", type=int, default=300,
                        help="Min voice duration ms before trigger fires (default: 300)")
    parser.add_argument("--cooldown", type=int, default=2000,
                        help="Cooldown ms after trigger (default: 2000)")
    parser.add_argument("--device-id", default="distiller-cm5",
                        help="Device identifier (default: distiller-cm5)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging")
    args = parser.parse_args()

    known_ble: Dict[str, str] = {}
    for entry in (args.known_ble or []):
        if "=" in entry:
            mac, name = entry.split("=", 1)
            known_ble[mac.strip()] = name.strip()

    loop = DistillerContextLoop(
        gateway_url=args.gateway,
        enable_camera=not args.no_camera,
        enable_ble=not args.no_ble,
        known_ble=known_ble,
        audio_threshold_rms=args.threshold_rms,
        audio_min_duration_ms=args.min_duration,
        audio_cooldown_ms=args.cooldown,
        device_id=args.device_id,
        log_level=logging.DEBUG if args.verbose else logging.INFO,
    )
    loop.run()


if __name__ == "__main__":
    main()
