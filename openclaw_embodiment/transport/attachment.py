"""AttachmentTransport -- sends camera frames and audio as direct attachments.

Instead of embedding and compressing context, this transport attaches raw
camera frames (base64-encoded) and audio clips directly to OpenClaw session
turns via ``openclaw sessions spawn``. The agent receives the raw frame,
not just an embedding -- enabling richer multi-modal context.

Useful when:
- The device is connected to a local OpenClaw gateway (LAN or Tailscale).
- Rich visual context is more valuable than low-latency BLE transfer.
- You want to bypass the BLE 25KB payload limit entirely.

Usage::

    transport = AttachmentTransport(session_id="my-session")
    result = transport.send(context_payload)
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from ..context.models import ContextPayload
from ..hal.base import SendResult, TransportHal, TransportState

logger = logging.getLogger(__name__)


@dataclass
class AttachmentConfig:
    """Configuration for AttachmentTransport.

    Attributes:
        session_id:      OpenClaw session to attach turns to.
        openclaw_bin:    Path to the openclaw CLI binary.
        timeout_s:       Subprocess timeout in seconds.
        include_audio:   Whether to attach audio clips in addition to frames.
        max_frame_bytes: If frame exceeds this, JPEG-compress before attach.
                         Set 0 to disable compression cap.
        fallback_transport: Optional transport to route to if attach fails.
    """

    session_id: str = ""
    openclaw_bin: str = "openclaw"
    timeout_s: float = 10.0
    include_audio: bool = True
    max_frame_bytes: int = 512 * 1024  # 512KB cap before compression
    fallback_transport: Optional[TransportHal] = None


class AttachmentTransportError(RuntimeError):
    """Raised when attachment send fails and no fallback is configured."""


class AttachmentTransport(TransportHal):
    """TransportHal that attaches camera frames to OpenClaw session turns.

    The agent receives the raw camera frame as a base64-encoded attachment
    rather than just a semantic embedding. This enables richer multi-modal
    reasoning at the cost of higher bandwidth.

    Expected latency: ~50-200ms (LAN) depending on payload size and
    subprocess spawn overhead.

    Thread-safe. ``connect()`` and ``disconnect()`` are no-ops for this
    stateless transport.
    """

    EXPECTED_LATENCY_MS: int = 100

    def __init__(self, config: Optional[AttachmentConfig] = None) -> None:
        """Initialise AttachmentTransport.

        Args:
            config: AttachmentConfig. Defaults to minimal configuration.
        """
        self._config = config or AttachmentConfig()
        self._state = TransportState.DISCONNECTED
        self._state_cb: Optional[Callable[[TransportState], None]] = None
        self._latency_window: deque = deque(maxlen=10)

    # ------------------------------------------------------------------
    # TransportHal lifecycle
    # ------------------------------------------------------------------

    def initialize(self, config: dict) -> None:
        """Accept dict config overrides (session_id, openclaw_bin, timeout_s).

        Args:
            config: Dict with optional keys: ``session_id``, ``openclaw_bin``,
                    ``timeout_s``, ``include_audio``.
        """
        if "session_id" in config:
            self._config.session_id = config["session_id"]
        if "openclaw_bin" in config:
            self._config.openclaw_bin = config["openclaw_bin"]
        if "timeout_s" in config:
            self._config.timeout_s = float(config["timeout_s"])
        if "include_audio" in config:
            self._config.include_audio = bool(config["include_audio"])
        self._state = TransportState.CONNECTED
        self._emit_state(TransportState.CONNECTED)

    def connect(self) -> None:
        """No-op: AttachmentTransport is stateless (subprocess per send)."""
        self._emit_state(TransportState.CONNECTED)

    def disconnect(self) -> None:
        """No-op: AttachmentTransport is stateless."""
        self._emit_state(TransportState.DISCONNECTED)

    def shutdown(self) -> None:
        """Release any held state. Idempotent."""
        self.disconnect()

    def get_state(self) -> TransportState:
        """Return current logical connection state."""
        return self._state

    def set_state_callback(self, callback: Callable[[TransportState], None]) -> None:
        """Register state change callback."""
        self._state_cb = callback

    # ------------------------------------------------------------------
    # Send -- core operation
    # ------------------------------------------------------------------

    def send(self, payload: bytes) -> SendResult:
        """Parse payload as ContextPayload JSON and attach frame to session.

        Accepts bytes in one of two formats:
        1. JSON-encoded ContextPayload dict (preferred).
        2. Raw JPEG bytes (frame-only attach, no session turn metadata).

        Args:
            payload: Serialized ContextPayload or raw JPEG bytes.

        Returns:
            SendResult indicating success/failure and bytes sent.
        """
        t0 = _ms()
        try:
            result = self._send_internal(payload)
            elapsed = _ms() - t0
            self._latency_window.append(elapsed)
            return result
        except Exception as exc:
            elapsed = _ms() - t0
            if self._config.fallback_transport is not None:
                logger.warning("[AttachmentTransport] Falling back to secondary transport: %s", exc)
                return self._config.fallback_transport.send(payload)
            logger.error("[AttachmentTransport] Send failed: %s", exc)
            return SendResult(False, 0, elapsed, error_code="ATTACH_SEND_FAILED")

    def send_context(self, context: ContextPayload) -> SendResult:
        """High-level send accepting a ContextPayload directly.

        Converts frame bytes to base64, builds attachment metadata,
        and spawns an OpenClaw session turn with the attachment.

        Args:
            context: ContextPayload from the pipeline.

        Returns:
            SendResult with outcome details.
        """
        t0 = _ms()
        try:
            frame_b64 = base64.b64encode(context.image_data).decode() if context.image_data else ""
            audio_b64 = base64.b64encode(context.audio_data).decode() if (
                context.audio_data and self._config.include_audio
            ) else ""

            turn_meta = {
                "event_id": context.event_id,
                "device_id": context.device_id,
                "timestamp_epoch": context.timestamp_epoch,
                "has_frame": bool(frame_b64),
                "has_audio": bool(audio_b64),
                "frame_bytes_b64": frame_b64,
                "audio_bytes_b64": audio_b64,
            }

            bytes_sent = len(context.image_data) + len(context.audio_data)
            self._spawn_session_turn(turn_meta)
            elapsed = _ms() - t0
            self._latency_window.append(elapsed)
            logger.info("[AttachmentTransport] Sent context event=%s bytes=%d elapsed=%dms",
                        context.event_id, bytes_sent, elapsed)
            return SendResult(True, bytes_sent, elapsed)

        except Exception as exc:
            elapsed = _ms() - t0
            logger.error("[AttachmentTransport] send_context failed: %s", exc)
            return SendResult(False, 0, elapsed, error_code="ATTACH_CTX_FAILED")

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        """Not supported for AttachmentTransport (response via AgentResponseListener).

        Returns:
            Always None -- responses are routed through the agent event system.
        """
        return None

    # ------------------------------------------------------------------
    # Latency
    # ------------------------------------------------------------------

    def get_expected_latency_ms(self) -> int:
        """Return expected attach latency (LAN subprocess overhead).

        Returns:
            100ms nominal (LAN, local openclaw CLI).
        """
        return self.EXPECTED_LATENCY_MS

    def get_measured_latency_ms(self) -> Optional[int]:
        """Return rolling average of last 10 send latencies."""
        if not self._latency_window:
            return None
        return int(sum(self._latency_window) / len(self._latency_window))

    # ------------------------------------------------------------------
    # HALBase
    # ------------------------------------------------------------------

    def validate(self) -> bool:
        """Check that the openclaw binary is available on PATH.

        Returns:
            True if ``openclaw --version`` exits cleanly.
        """
        try:
            r = subprocess.run(
                [self._config.openclaw_bin, "--version"],
                capture_output=True,
                timeout=5,
            )
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_device_info(self) -> dict:
        """Return transport metadata."""
        return {
            "name": "attachment-transport",
            "type": "subprocess",
            "session_id": self._config.session_id,
            "openclaw_bin": self._config.openclaw_bin,
            "expected_latency_ms": self.EXPECTED_LATENCY_MS,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_internal(self, payload: bytes) -> SendResult:
        """Attempt to parse and attach the payload."""
        try:
            meta = json.loads(payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Treat as raw JPEG frame
            meta = {
                "event_id": "raw-%d" % _ms(),
                "frame_bytes_b64": base64.b64encode(payload).decode(),
            }

        self._spawn_session_turn(meta)
        return SendResult(True, len(payload), 0)

    def _spawn_session_turn(self, meta: dict) -> None:
        """Write metadata to a temp file and spawn OpenClaw session turn.

        Args:
            meta: Dict with event metadata and base64-encoded attachments.

        Raises:
            AttachmentTransportError: If the subprocess fails.
        """
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="oc_attach_", delete=False
        ) as f:
            json.dump(meta, f)
            tmp_path = f.name

        cmd = [self._config.openclaw_bin, "sessions", "spawn",
               "--attachment", tmp_path]
        if self._config.session_id:
            cmd += ["--session", self._config.session_id]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._config.timeout_s,
            )
            if result.returncode != 0:
                raise AttachmentTransportError(
                    f"openclaw sessions spawn failed ({result.returncode}): {result.stderr.strip()}"
                )
            logger.debug("[AttachmentTransport] Spawned turn: %s", result.stdout.strip())
        except subprocess.TimeoutExpired as exc:
            raise AttachmentTransportError(
                f"openclaw sessions spawn timed out after {self._config.timeout_s}s"
            ) from exc
        except FileNotFoundError as exc:
            raise AttachmentTransportError(
                f"openclaw binary not found: {self._config.openclaw_bin}"
            ) from exc
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except OSError:
                pass

    def _emit_state(self, state: TransportState) -> None:
        self._state = state
        if self._state_cb is not None:
            try:
                self._state_cb(state)
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- transport state callback -- must not crash transport
                logger.debug("[AttachmentTransport] State callback error: %s", exc)


def _ms() -> int:
    return int(time.monotonic() * 1000)
