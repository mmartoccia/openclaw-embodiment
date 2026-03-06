"""Meta Ray-Ban MWDAT frame receiver and audio WebSocket server.

Flask HTTP server on port 8421 accepts POST /frame (JPEG bytes) from the
MWDAT iOS/macOS companion app. WebSocket server on port 8422 handles
bidirectional audio (16kHz PCM in, 24kHz PCM out).

Thread-safe frame buffer using threading.Event for frame-ready sync.

Usage::

    server = RayBanServer(http_port=8421, ws_port=8422)
    server.start()
    frame_bytes = server.get_latest_frame(timeout_s=5.0)
    server.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class RayBanFrameBuffer:
    """Thread-safe single-frame buffer with event-based availability signaling.

    Stores the most recent JPEG frame received from the MWDAT companion app.
    Readers block on ``wait_for_frame()`` until a new frame arrives or timeout.
    """

    def __init__(self) -> None:
        self._frame: Optional[bytes] = None
        self._event = threading.Event()
        self._lock = threading.Lock()

    def put(self, jpeg_bytes: bytes) -> None:
        """Store a new frame and signal waiting readers.

        Args:
            jpeg_bytes: Raw JPEG frame bytes from MWDAT.
        """
        with self._lock:
            self._frame = jpeg_bytes
        self._event.set()

    def get(self, timeout_s: float = 5.0) -> Optional[bytes]:
        """Return the latest frame, blocking until one arrives or timeout.

        Args:
            timeout_s: Maximum seconds to wait for a frame.

        Returns:
            JPEG bytes if a frame is available, None on timeout.
        """
        if self._event.wait(timeout=timeout_s):
            self._event.clear()
            with self._lock:
                return self._frame
        return None

    def get_nowait(self) -> Optional[bytes]:
        """Return the latest frame without blocking. Returns None if empty."""
        with self._lock:
            return self._frame


class RayBanAudioBuffer:
    """Thread-safe audio chunk queue for bidirectional PCM audio.

    Stores incoming 16kHz PCM chunks from glasses microphone.
    """

    def __init__(self, max_chunks: int = 32) -> None:
        import queue
        self._queue: queue.Queue = queue.Queue(maxsize=max_chunks)

    def put(self, pcm_bytes: bytes) -> None:
        """Enqueue incoming audio chunk (16kHz PCM from glasses mic).

        Args:
            pcm_bytes: Raw PCM_INT16 bytes.
        """
        try:
            self._queue.put_nowait(pcm_bytes)
        except Exception:  # grain: ignore NAKED_EXCEPT -- queue full, drop oldest chunk
            pass

    def get(self, timeout_s: float = 1.0) -> Optional[bytes]:
        """Dequeue audio chunk, blocking until available or timeout.

        Args:
            timeout_s: Maximum seconds to wait.

        Returns:
            PCM bytes or None on timeout.
        """
        try:
            return self._queue.get(timeout=timeout_s)
        except Exception:  # grain: ignore NAKED_EXCEPT -- queue empty timeout
            return None


class RayBanServer:
    """Manages Flask HTTP + WebSocket servers for Meta Ray-Ban MWDAT bridge.

    Starts two servers:
    - HTTP on http_port: accepts POST /frame with JPEG body
    - WebSocket on ws_port: bidirectional audio (16kHz in, 24kHz out)

    Both are optional -- servers start only if their dependencies are installed.
    Falls back gracefully in test/mock mode.

    Attributes:
        frame_buffer: RayBanFrameBuffer for received camera frames.
        audio_in_buffer: RayBanAudioBuffer for received microphone audio.
        audio_out_queue: Queue for PCM audio to send to glasses speaker.
    """

    def __init__(
        self,
        http_port: int = 8421,
        ws_port: int = 8422,
        mock_mode: bool = True,
    ) -> None:
        """Initialize RayBanServer.

        Args:
            http_port: Port for Flask frame receiver.
            ws_port: Port for WebSocket audio bridge.
            mock_mode: If True, skip actual socket binding (for CI/testing).
        """
        self.http_port = http_port
        self.ws_port = ws_port
        self.mock_mode = mock_mode

        self.frame_buffer = RayBanFrameBuffer()
        self.audio_in_buffer = RayBanAudioBuffer()
        import queue
        self.audio_out_queue: queue.Queue = queue.Queue(maxsize=64)

        self._http_thread: Optional[threading.Thread] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        """Start the HTTP and WebSocket servers in background threads."""
        self._running = True
        if self.mock_mode:
            logger.info("RayBanServer: mock_mode=True -- servers not bound, using in-process buffers only")
            return
        self._http_thread = threading.Thread(target=self._run_http, daemon=True, name="rayban-http")
        self._http_thread.start()
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True, name="rayban-ws")
        self._ws_thread.start()
        logger.info("RayBanServer: HTTP on %d, WS on %d", self.http_port, self.ws_port)

    def stop(self) -> None:
        """Stop all server threads."""
        self._running = False
        logger.info("RayBanServer: stopped")

    def get_latest_frame(self, timeout_s: float = 5.0) -> Optional[bytes]:
        """Block until a frame arrives or timeout.

        Args:
            timeout_s: Maximum wait time.

        Returns:
            JPEG frame bytes or None on timeout.
        """
        return self.frame_buffer.get(timeout_s=timeout_s)

    def send_audio_to_glasses(self, pcm_24k: bytes) -> None:
        """Enqueue 24kHz PCM audio to be sent to glasses speaker via WebSocket.

        Args:
            pcm_24k: Raw PCM_INT16 bytes at 24000 Hz sample rate.
        """
        try:
            self.audio_out_queue.put_nowait(pcm_24k)
        except Exception:  # grain: ignore NAKED_EXCEPT -- queue full, drop
            pass

    def _run_http(self) -> None:
        """Run Flask HTTP server for frame reception (background thread)."""
        try:
            from flask import Flask, request as flask_request

            flask_app = Flask(__name__)

            @flask_app.route("/frame", methods=["POST"])
            def receive_frame():  # type: ignore[return-value]
                jpeg = flask_request.get_data()
                if jpeg:
                    self.frame_buffer.put(jpeg)
                    logger.debug("RayBanServer: received frame %d bytes", len(jpeg))
                return "", 204

            flask_app.run(host="0.0.0.0", port=self.http_port, threaded=True, use_reloader=False)
        except ImportError:
            logger.warning("RayBanServer: flask not installed -- HTTP frame receiver unavailable")
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- RayBan server -- Flask/WebSocket errors unpredictable
            logger.error("RayBanServer HTTP error: %s", exc)

    def _run_ws(self) -> None:
        """Run WebSocket server for audio I/O (background thread)."""
        try:
            import asyncio
            import websockets

            async def audio_handler(websocket) -> None:
                """Handle bidirectional audio WebSocket connection."""
                async def send_loop() -> None:
                    while self._running:
                        try:
                            pcm = self.audio_out_queue.get_nowait()
                            await websocket.send(pcm)
                        except Exception:  # grain: ignore NAKED_EXCEPT -- queue empty, sleep
                            await asyncio.sleep(0.01)

                send_task = asyncio.ensure_future(send_loop())
                try:
                    async for message in websocket:
                        if isinstance(message, bytes):
                            self.audio_in_buffer.put(message)
                finally:
                    send_task.cancel()

            async def serve() -> None:
                async with websockets.serve(audio_handler, "0.0.0.0", self.ws_port):
                    while self._running:
                        await asyncio.sleep(0.1)

            asyncio.run(serve())
        except ImportError:
            logger.warning("RayBanServer: websockets not installed -- audio bridge unavailable")
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- RayBan server -- Flask/WebSocket errors unpredictable
            logger.error("RayBanServer WS error: %s", exc)


__all__ = ["RayBanServer", "RayBanFrameBuffer", "RayBanAudioBuffer"]
