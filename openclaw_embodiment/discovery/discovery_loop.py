"""DiscoveryLoop -- Autonomous spatial discovery runtime for the Distiller CM5.

The primary operating mode for the Distiller. Replaces the reactive
AudioTriggerDetector with a continuous, scheduled polling loop that:
  - Scans BLE every 30s
  - Samples ambient audio every 60s
  - Captures camera + computes scene diff every 300s
  - Updates WorldModel and SpaceModel after every sensor cycle
  - Fires anomaly detection after each update
  - Produces a DiscoveryReport every 600s (or on anomaly)

Output:
  - If gateway_url is set: POST JSON to <gateway_url>/discovery/report
  - If no gateway: write JSON to output_dir (default: /home/distiller/discovery-output)

CLI usage:
    python -m openclaw_embodiment.discovery.discovery_loop \\
        --duration 300 \\
        --gateway http://192.168.1.183:18799 \\
        --known-ble aa:bb:cc:dd:ee:ff=MikePendant \\
        --output-dir /home/distiller/discovery-output

    --duration 300   Run for 5 minutes then produce final report and exit (0 = run forever)
    --gateway        Optional gateway URL for POST delivery
    --known-ble      MAC=name pairs for recognized BLE devices
    --no-camera      Disable camera (use if cam unavailable)
    --no-ble         Disable BLE scanner
    --no-mic         Disable microphone
    --output-dir     Where to write report JSON (local fallback)
    --device-id      Device identifier string
    --db-path        Path to SpaceModel SQLite DB
    --verbose        Enable debug logging
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Polling intervals (configurable at runtime)
# ------------------------------------------------------------------

DEFAULT_BLE_INTERVAL_S = 30
DEFAULT_AUDIO_INTERVAL_S = 60
DEFAULT_CAMERA_INTERVAL_S = 300
DEFAULT_REPORT_INTERVAL_S = 600
DEFAULT_SLEEP_S = 30

# ------------------------------------------------------------------
# DiscoveryLoop
# ------------------------------------------------------------------

class DiscoveryLoop:
    """Autonomous sensing and discovery runtime.

    Args:
        device_id:          Device identifier.
        gateway_url:        Optional gateway URL (POST target). None = local file only.
        output_dir:         Directory for local JSON output.
        db_path:            SpaceModel SQLite path.
        known_ble_map:      Dict[mac -> name] for recognized BLE devices.
        ble_interval_s:     BLE scan every N seconds.
        audio_interval_s:   Audio sample every N seconds.
        camera_interval_s:  Camera capture every N seconds.
        report_interval_s:  Discovery report every N seconds.
        mic_device:         ALSA device for audio capture.
        no_camera:          Disable camera.
        no_ble:             Disable BLE scanner.
        no_mic:             Disable microphone.
    """

    def __init__(
        self,
        device_id: str = "distiller-cm5",
        gateway_url: Optional[str] = None,
        output_dir: Optional[str] = None,
        db_path: Optional[str] = None,
        known_ble_map: Optional[Dict[str, str]] = None,
        ble_interval_s: int = DEFAULT_BLE_INTERVAL_S,
        audio_interval_s: int = DEFAULT_AUDIO_INTERVAL_S,
        camera_interval_s: int = DEFAULT_CAMERA_INTERVAL_S,
        report_interval_s: int = DEFAULT_REPORT_INTERVAL_S,
        mic_device: str = "hw:0,0",
        no_camera: bool = False,
        no_ble: bool = False,
        no_mic: bool = False,
    ) -> None:
        self.device_id = device_id
        self.gateway_url = gateway_url
        self.output_dir = output_dir or self._default_output_dir()
        self.db_path = db_path

        self.known_ble_map = {k.lower(): v for k, v in (known_ble_map or {}).items()}

        self.ble_interval_s = ble_interval_s
        self.audio_interval_s = audio_interval_s
        self.camera_interval_s = camera_interval_s
        self.report_interval_s = report_interval_s
        self.mic_device = mic_device

        self.no_camera = no_camera
        self.no_ble = no_ble
        self.no_mic = no_mic

        # Session state
        self._running = False
        self._start_ms: int = 0
        self._session_anomalies: List[Any] = []
        self._awareness_history: List[float] = []

        # Timing state
        self._last_ble_s: float = 0
        self._last_audio_s: float = 0
        self._last_camera_s: float = 0
        self._last_report_s: float = 0

        # Scene baseline tracking
        self._baseline_mean_px: Optional[float] = None
        self._baseline_lighting: Optional[str] = None

        # Initialized in start()
        self._mic_hal = None
        self._camera_hal = None
        self._ble_scanner = None
        self._context_builder = None
        self._world_model = None
        self._space_model = None
        self._anomaly_detector = None

        # Sensor health tracking
        self._sensor_health: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, duration_s: int = 0) -> None:
        """Start the discovery loop.

        Args:
            duration_s: Run for this many seconds then stop (0 = run forever).
        """
        self._running = True
        self._start_ms = int(time.time() * 1000)

        logger.info("[DiscoveryLoop] Starting -- device_id=%s", self.device_id)
        logger.info("[DiscoveryLoop] Gateway: %s | Output dir: %s",
                    self.gateway_url or "none (local file)", self.output_dir)
        logger.info("[DiscoveryLoop] Intervals: BLE=%ds Audio=%ds Camera=%ds Report=%ds",
                    self.ble_interval_s, self.audio_interval_s,
                    self.camera_interval_s, self.report_interval_s)

        # Initialize components
        self._init_hals()
        self._init_models()

        os.makedirs(self.output_dir, exist_ok=True)

        # Register signal handlers
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Run the loop
        try:
            self._run_loop(duration_s)
        finally:
            self._finalize()

    def stop(self) -> None:
        """Request the loop to stop after the current cycle."""
        logger.info("[DiscoveryLoop] Stop requested.")
        self._running = False

    def _handle_signal(self, sig, frame) -> None:
        logger.info("[DiscoveryLoop] Signal %d received -- stopping.", sig)
        self._running = False

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _init_hals(self) -> None:
        """Initialize hardware abstraction layers with graceful degradation."""

        # Microphone
        if not self.no_mic:
            try:
                from openclaw_embodiment.hal.distiller_reference import DistillerMicrophoneHAL
                self._mic_hal = DistillerMicrophoneHAL()
                self._mic_hal.initialize()
                self._sensor_health["mic"] = "ok"
                logger.info("[DiscoveryLoop] Microphone initialized.")
            except Exception as e:  # grain: ignore NAKED_EXCEPT -- discovery loop iteration -- one cycle failure must not stop the daemon
                logger.warning("[DiscoveryLoop] Mic init failed: %s", e)
                self._sensor_health["mic"] = "failed"
        else:
            self._sensor_health["mic"] = "disabled"
            logger.info("[DiscoveryLoop] Microphone disabled by flag.")

        # Camera
        if not self.no_camera:
            try:
                from openclaw_embodiment.hal.distiller_reference import DistillerCameraHAL
                self._camera_hal = DistillerCameraHAL()
                self._sensor_health["camera"] = "color_unreliable"  # known OV5647 issue
                logger.info("[DiscoveryLoop] Camera initialized (grayscale mode).")
            except Exception as e:  # grain: ignore NAKED_EXCEPT -- discovery loop iteration -- one cycle failure must not stop the daemon
                logger.warning("[DiscoveryLoop] Camera init failed: %s", e)
                self._sensor_health["camera"] = "failed"
        else:
            self._sensor_health["camera"] = "disabled"
            logger.info("[DiscoveryLoop] Camera disabled by flag.")

        # BLE Scanner
        if not self.no_ble:
            try:
                from openclaw_embodiment.hal.ble_scanner import BLEProximityScanner
                self._ble_scanner = BLEProximityScanner(
                    known_map=self.known_ble_map,
                    scan_duration_s=5.0,
                )
                self._sensor_health["ble"] = "ok"
                logger.info("[DiscoveryLoop] BLE scanner initialized.")
            except Exception as e:  # grain: ignore NAKED_EXCEPT -- discovery loop iteration -- one cycle failure must not stop the daemon
                logger.warning("[DiscoveryLoop] BLE scanner init failed: %s", e)
                self._sensor_health["ble"] = "failed"
        else:
            self._sensor_health["ble"] = "disabled"
            logger.info("[DiscoveryLoop] BLE disabled by flag.")

        # IMU: not present on CM5
        self._sensor_health["imu"] = "not_present"

    def _init_models(self) -> None:
        """Initialize WorldModel, SpaceModel, ContextBuilder, AnomalyDetector."""
        from openclaw_embodiment.core.context_builder import ContextBuilder, DISTILLER_CM5_CAPABILITIES
        from openclaw_embodiment.discovery.space_model import SpaceModel
        from openclaw_embodiment.discovery.world_model import WorldModel
        from openclaw_embodiment.discovery.anomaly_detector import AnomalyDetector

        self._context_builder = ContextBuilder(
            device_id=self.device_id,
            capabilities=DISTILLER_CM5_CAPABILITIES,
            mic_hal=self._mic_hal,
            camera_hal=self._camera_hal,
            ble_scanner=self._ble_scanner,
            capture_audio_ms=5000,
        )

        self._world_model = WorldModel(
            device_id=self.device_id,
            window_seconds=300,
            perspective="room_centric",
        )

        self._space_model = SpaceModel(
            db_path=self.db_path,
            device_id=self.device_id,
        )

        self._anomaly_detector = AnomalyDetector(
            on_anomaly=self._on_anomaly,
            scene_change_threshold=0.3,
            min_baseline_scans=3,
        )

        logger.info("[DiscoveryLoop] Models initialized.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def _run_loop(self, duration_s: int) -> None:
        """Main sensing loop."""
        now = time.time()
        # Schedule first run of everything immediately
        self._last_ble_s = now - self.ble_interval_s
        self._last_audio_s = now - self.audio_interval_s
        self._last_camera_s = now - self.camera_interval_s
        self._last_report_s = now - self.report_interval_s

        while self._running:
            now = time.time()
            elapsed_s = int((now * 1000 - self._start_ms) / 1000)

            # Duration check
            if duration_s > 0 and elapsed_s >= duration_s:
                logger.info("[DiscoveryLoop] Duration %ds reached. Stopping.", duration_s)
                break

            # Determine what's due this cycle
            ble_due = (now - self._last_ble_s) >= self.ble_interval_s
            audio_due = (now - self._last_audio_s) >= self.audio_interval_s
            camera_due = (now - self._last_camera_s) >= self.camera_interval_s
            report_due = (now - self._last_report_s) >= self.report_interval_s

            # --- BLE scan ---
            proximity_ctx = None
            if ble_due and not self.no_ble and self._ble_scanner:
                proximity_ctx = self._scan_ble()
                self._last_ble_s = now

            # --- Audio sample ---
            audio_ctx = None
            if audio_due and not self.no_mic and self._mic_hal:
                audio_ctx = self._sample_audio()
                self._last_audio_s = now

            # --- Camera capture ---
            visual_ctx = None
            if camera_due and not self.no_camera and self._camera_hal:
                visual_ctx, diff_score = self._capture_scene()
                self._last_camera_s = now

            # --- Assemble SensorContext ---
            if audio_ctx is not None or visual_ctx is not None or proximity_ctx is not None:
                sensor_ctx = self._assemble_context(audio_ctx, visual_ctx, proximity_ctx)

                # --- Update WorldModel ---
                self._world_model.update(sensor_ctx)

                # --- Update SpaceModel ---
                if proximity_ctx is not None:
                    self._space_model.update_ble_with_names(proximity_ctx, self.known_ble_map)
                if audio_ctx is not None:
                    self._space_model.update_activity_full(
                        audio_ctx, proximity_ctx, duration_min=1.0
                    )
                if visual_ctx is not None and camera_due:
                    lighting = getattr(visual_ctx, "lighting", "unknown")
                    mean_px = self._last_mean_px or 0.0
                    diff = self._last_diff_score or 0.0
                    is_baseline = self._baseline_mean_px is None
                    self._space_model.update_scene(lighting, mean_px, diff, is_baseline=is_baseline)
                    if is_baseline:
                        self._baseline_mean_px = mean_px
                        self._baseline_lighting = lighting

                # Track awareness
                awareness = getattr(sensor_ctx, "awareness_level", 0.0)
                self._awareness_history.append(awareness)

                # --- Anomaly detection ---
                anomalies = self._anomaly_detector.check(
                    sensor_ctx, self._world_model, self._space_model
                )
                if anomalies:
                    self._session_anomalies.extend(anomalies)
                    self._push_report(triggered_by="anomaly")
                    self._last_report_s = now

                logger.info(
                    "[DiscoveryLoop] Cycle complete: awareness=%.2f entities=%d anomalies=%d",
                    awareness,
                    self._world_model.get_active_entity_count(),
                    len(anomalies),
                )

            # --- Scheduled report ---
            if report_due:
                self._push_report(triggered_by="scheduled")
                self._last_report_s = now

            # Sleep until next cycle
            time.sleep(DEFAULT_SLEEP_S)

    # ------------------------------------------------------------------
    # Sensor helpers
    # ------------------------------------------------------------------

    _last_mean_px: Optional[float] = None
    _last_diff_score: Optional[float] = None

    def _scan_ble(self):
        """Run BLE scan with error handling."""
        try:
            result = self._ble_scanner.scan()
            logger.debug(
                "[DiscoveryLoop] BLE: %d known, %d unknown, conf=%.2f",
                len(result.known_devices), result.unknown_count, result.confidence
            )
            return result
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- discovery loop iteration -- one cycle failure must not stop the daemon
            logger.warning("[DiscoveryLoop] BLE scan error: %s", e)
            self._space_model.log_discovery("SENSOR_ERROR", f"BLE scan failed: {e}", confidence=0.5)
            return None

    def _sample_audio(self):
        """Capture ambient audio and build AudioContext."""
        try:
            from openclaw_embodiment.core.context_builder import AudioContext
            import math, struct

            chunk = self._mic_hal.capture(duration_ms=5000)
            raw = chunk.data
            if len(raw) >= 2:
                num_samples = len(raw) // 2
                samples = struct.unpack(f"<{num_samples}h", raw[:num_samples * 2])
                mean_sq = sum(s * s for s in samples) / num_samples
                rms = math.sqrt(mean_sq)
            else:
                rms = 0.0

            # Classify ambient
            if rms < 200:
                ambient = "silence"
            elif rms < 1500:
                ambient = "noise"
            elif rms < 4000:
                ambient = "speech"
            else:
                ambient = "loud"

            confidence = min(1.0, rms / 3000.0) if rms > 0 else 0.3

            ctx = AudioContext(
                transcript=None,
                speaker_count=None,
                ambient_class=ambient,
                rms_level=rms,
                language=None,
                confidence=round(confidence, 3),
            )
            logger.debug("[DiscoveryLoop] Audio: rms=%.0f ambient=%s", rms, ambient)
            return ctx

        except Exception as e:  # grain: ignore NAKED_EXCEPT -- discovery loop iteration -- one cycle failure must not stop the daemon
            logger.warning("[DiscoveryLoop] Audio sample error: %s", e)
            self._space_model.log_discovery("SENSOR_ERROR", f"Audio capture failed: {e}", confidence=0.5)
            return None

    def _capture_scene(self):
        """Capture grayscale frame, compute lighting and scene diff."""
        try:
            from openclaw_embodiment.core.context_builder import VisualContext

            jpeg_bytes = self._camera_hal.capture_grayscale()
            lighting = self._camera_hal.get_lighting_level()
            person_count = None
            try:
                person_count = self._camera_hal.estimate_person_count()
            except Exception:  # grain: ignore NAKED_EXCEPT -- report build -- partial data must not crash the loop
                pass

            # Compute mean pixel value from JPEG bytes (rough estimate)
            try:
                import io
                from PIL import Image
                img = Image.open(io.BytesIO(jpeg_bytes)).convert("L")
                pixels = list(img.getdata())
                mean_px = sum(pixels) / len(pixels) if pixels else 128.0
            except Exception:  # grain: ignore NAKED_EXCEPT -- report build -- partial data must not crash the loop
                mean_px = 128.0  # default

            # Compute diff from baseline
            diff_score = 0.0
            if self._baseline_mean_px is not None:
                diff_score = abs(mean_px - self._baseline_mean_px) / 255.0

            self._last_mean_px = mean_px
            self._last_diff_score = diff_score

            confidence = 0.7 if lighting != "dark" else 0.3
            ctx = VisualContext(
                description=None,
                person_count=person_count,
                activity=None,
                lighting=lighting,
                frame_path=None,
                confidence=round(confidence, 3),
            )
            logger.debug(
                "[DiscoveryLoop] Camera: lighting=%s mean_px=%.1f diff=%.3f",
                lighting, mean_px, diff_score
            )
            return ctx, diff_score

        except Exception as e:  # grain: ignore NAKED_EXCEPT -- discovery loop iteration -- one cycle failure must not stop the daemon
            logger.warning("[DiscoveryLoop] Camera capture error: %s", e)
            self._space_model.log_discovery("SENSOR_ERROR", f"Camera capture failed: {e}", confidence=0.5)
            return None, 0.0

    def _assemble_context(self, audio_ctx, visual_ctx, proximity_ctx):
        """Assemble SensorContext from individual channel readings."""
        try:
            # Use the context builder's internal assembly logic
            # but with pre-captured data rather than live HAL calls
            from openclaw_embodiment.core.context_builder import (
                SensorContext, DISTILLER_CM5_CAPABILITIES
            )
            import time

            # Build conflicts and awareness directly
            conflicts = []
            channel_scores = []

            if audio_ctx is not None:
                channel_scores.append(audio_ctx.confidence)
            if visual_ctx is not None:
                channel_scores.append(visual_ctx.confidence)
            if proximity_ctx is not None:
                ble_conf = getattr(proximity_ctx, "confidence", 0.5)
                # Wrap proximity_ctx in ContextBuilder's ProximityContext if needed
                from openclaw_embodiment.core.context_builder import ProximityContext as CBProx
                if not isinstance(proximity_ctx, CBProx):
                    proximity_ctx = CBProx(
                        known_devices=getattr(proximity_ctx, "known_devices", []),
                        unknown_count=getattr(proximity_ctx, "unknown_count", 0),
                        rssi_map=getattr(proximity_ctx, "rssi_map", {}),
                        confidence=ble_conf,
                    )
                channel_scores.append(ble_conf)

            awareness = sum(channel_scores) / len(channel_scores) if channel_scores else 0.0
            awareness = max(0.0, min(1.0, awareness - len(conflicts) * 0.15))

            # Build summary
            summary_parts = ["Trigger: scheduled."]
            if audio_ctx:
                summary_parts.append(f"Audio: {audio_ctx.ambient_class} (rms={audio_ctx.rms_level:.0f}).")
            if visual_ctx:
                summary_parts.append(f"Visual: lighting={visual_ctx.lighting}.")
            if proximity_ctx:
                n = getattr(proximity_ctx, "unknown_count", 0)
                known = getattr(proximity_ctx, "known_devices", [])
                summary_parts.append(f"BLE: {len(known)} known, {n} unknown.")
            summary_parts.append(f"Awareness: {awareness:.2f}.")

            return SensorContext(
                timestamp_ms=int(time.time() * 1000),
                device_id=self.device_id,
                trigger="scheduled",
                audio=audio_ctx,
                visual=visual_ctx,
                motion=None,
                proximity=proximity_ctx,
                awareness_level=round(awareness, 3),
                conflicts=conflicts,
                summary=" ".join(summary_parts),
                device_capabilities=DISTILLER_CM5_CAPABILITIES,
            )

        except Exception as e:  # grain: ignore NAKED_EXCEPT -- discovery loop iteration -- one cycle failure must not stop the daemon
            logger.error("[DiscoveryLoop] Context assembly error: %s", e)
            return None

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _on_anomaly(self, anomalies: List) -> None:
        """Callback from AnomalyDetector."""
        for a in anomalies:
            logger.warning("[DiscoveryLoop] ANOMALY: %s -- %s", a.anomaly_type, a.description)

    def _push_report(self, triggered_by: str = "scheduled") -> None:
        """Build and deliver a DiscoveryReport."""
        try:
            from openclaw_embodiment.discovery.discovery_report import build_report

            report = build_report(
                world_model=self._world_model,
                space_model=self._space_model,
                anomalies=self._session_anomalies,
                session_start_ms=self._start_ms,
                sensor_health=self._sensor_health,
                awareness_history=self._awareness_history,
                device_id=self.device_id,
            )

            report_dict = report.to_dict()
            report_dict["triggered_by"] = triggered_by

            logger.info(
                "[DiscoveryLoop] Report: duration=%ds entities=%d anomalies=%d awareness=%.2f",
                report.session_duration_s,
                len(report.current_entities),
                len(report.anomalies),
                report.awareness_level,
            )

            # Try gateway
            if self.gateway_url:
                self._post_to_gateway(report_dict)
            else:
                # Write to local file
                filepath = report.save_to_file(self.output_dir)
                logger.info("[DiscoveryLoop] Report written: %s", filepath)

        except Exception as e:  # grain: ignore NAKED_EXCEPT -- gateway POST -- network errors are heterogeneous
            logger.error("[DiscoveryLoop] Report push failed: %s", e)

    def _post_to_gateway(self, payload: Dict[str, Any]) -> None:
        """POST report to gateway (fire and forget)."""
        def _post():
            try:
                url = f"{self.gateway_url.rstrip('/')}/discovery/report"
                data = json.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    url,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.info("[DiscoveryLoop] Gateway POST ok: %d", resp.status)
            except urllib.error.URLError as e:
                logger.warning("[DiscoveryLoop] Gateway POST failed: %s -- saving locally", e)
                # Fallback to local file
                try:
                    from openclaw_embodiment.discovery.discovery_report import DiscoveryReport
                    import datetime
                    os.makedirs(self.output_dir, exist_ok=True)
                    ts = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
                    filepath = os.path.join(self.output_dir, f"{ts}.json")
                    with open(filepath, "w") as f:
                        json.dump(payload, f, indent=2)
                    logger.info("[DiscoveryLoop] Saved locally: %s", filepath)
                except Exception as fe:  # grain: ignore NAKED_EXCEPT -- local file save fallback -- must not lose the primary error
                    logger.error("[DiscoveryLoop] Local save also failed: %s", fe)
            except Exception as e:  # grain: ignore NAKED_EXCEPT -- local file save fallback -- must not lose the primary error
                logger.error("[DiscoveryLoop] POST error: %s", e)

        # Non-blocking fire and forget
        t = threading.Thread(target=_post, daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def _finalize(self) -> None:
        """Push final report and log session summary."""
        logger.info("[DiscoveryLoop] Finalizing -- pushing final report.")
        try:
            self._push_report(triggered_by="final")
            # Give async POST time to complete
            time.sleep(2)
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- gateway POST -- network errors are heterogeneous
            logger.error("[DiscoveryLoop] Finalize error: %s", e)

        duration_s = int((time.time() * 1000 - self._start_ms) / 1000)
        logger.info(
            "[DiscoveryLoop] Session complete. Duration=%ds Scans=%d Anomalies=%d",
            duration_s,
            getattr(self._space_model, "_scan_count", 0),
            len(self._session_anomalies),
        )

    @staticmethod
    def _default_output_dir() -> str:
        if os.path.exists("/home/distiller"):
            return "/home/distiller/discovery-output"
        return os.path.expanduser("~/.openclaw-embodiment/discovery-output")


# ------------------------------------------------------------------
# CLI entrypoint
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Distiller Autonomous Discovery Loop",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--duration", type=int, default=0,
                        help="Run for N seconds then exit (0 = run forever)")
    parser.add_argument("--gateway", type=str, default=None,
                        help="Gateway URL (e.g. http://192.168.1.183:18799)")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Local output directory for discovery JSON")
    parser.add_argument("--db-path", type=str, default=None,
                        help="SpaceModel SQLite DB path")
    parser.add_argument("--device-id", type=str, default="distiller-cm5",
                        help="Device identifier")
    parser.add_argument("--known-ble", nargs="*", default=[], metavar="MAC=NAME",
                        help="Known BLE device mappings (e.g. aa:bb:cc:dd:ee:ff=Pendant)")
    parser.add_argument("--ble-interval", type=int, default=DEFAULT_BLE_INTERVAL_S)
    parser.add_argument("--audio-interval", type=int, default=DEFAULT_AUDIO_INTERVAL_S)
    parser.add_argument("--camera-interval", type=int, default=DEFAULT_CAMERA_INTERVAL_S)
    parser.add_argument("--report-interval", type=int, default=DEFAULT_REPORT_INTERVAL_S)
    parser.add_argument("--no-camera", action="store_true", help="Disable camera")
    parser.add_argument("--no-ble", action="store_true", help="Disable BLE scanner")
    parser.add_argument("--no-mic", action="store_true", help="Disable microphone")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )

    # Parse known BLE map
    known_ble = {}
    for entry in (args.known_ble or []):
        if "=" in entry:
            mac, name = entry.split("=", 1)
            known_ble[mac.strip().lower()] = name.strip()

    loop = DiscoveryLoop(
        device_id=args.device_id,
        gateway_url=args.gateway,
        output_dir=args.output_dir,
        db_path=args.db_path,
        known_ble_map=known_ble,
        ble_interval_s=args.ble_interval,
        audio_interval_s=args.audio_interval,
        camera_interval_s=args.camera_interval,
        report_interval_s=args.report_interval,
        no_camera=args.no_camera,
        no_ble=args.no_ble,
        no_mic=args.no_mic,
    )

    loop.start(duration_s=args.duration)


if __name__ == "__main__":
    main()
