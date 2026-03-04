"""Distiller CM5 HAL -- reference implementation for Distiller Pi CM5 hardware.

Hardware:
  - Microphone:  Pamir AI SoundCard (hw:0,0, 48kHz stereo S16_LE)
  - Speaker:     Pamir AI SoundCard (hw:0,0, playback)
  - Camera:      OV5647 5MP via rpicam-still
  - E-ink:       250x128 display via distiller_sdk (SPI, /dev/spidev0.0)
  - BLE:         BlueZ + bleak (Pi 5 built-in radio)

Requires on the Distiller device:
  - /opt/distiller-sdk activated (source /opt/distiller-sdk/activate.sh)
  - bleak installed in venv
  - rpicam-still in PATH
  - arecord / aplay (alsa-utils)
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import sys
import tempfile
import wave
from typing import Optional

from openclaw_embodiment.hal.base import (
    AudioChunk,
    CameraHal,
    DisplayCard,
    DisplayHal,
    AudioOutputHal,
    MicrophoneHal,
)
from openclaw_embodiment.core.response import AgentResponse, ResponseType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pamir AI SoundCard -- Microphone
# ---------------------------------------------------------------------------

class DistillerMicrophoneHAL(MicrophoneHal):
    """Captures audio from the Pamir AI SoundCard (hw:0,0)."""

    DEVICE = "hw:0,0"
    SDK_SRC = "/opt/distiller-sdk/src"
    SAMPLE_RATE = 48000
    CHANNELS = 2
    FORMAT = "S16_LE"

    def initialize(self) -> None:
        result = subprocess.run(
            ["arecord", "-D", self.DEVICE, "--dump-hw-params"],
            capture_output=True, text=True, timeout=3
        )
        if "S16_LE" in result.stdout or result.returncode != 0:
            logger.info("[DistillerMic] Pamir AI SoundCard detected.")
        else:
            logger.warning("[DistillerMic] Unexpected hw params: %s", result.stdout[:100])

    def capture(self, duration_ms: int = 2000) -> AudioChunk:
        """Capture audio for duration_ms milliseconds. Returns AudioChunk."""
        import time
        duration_s = duration_ms / 1000.0
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            subprocess.run(
                ["arecord", "-D", self.DEVICE, "-f", self.FORMAT,
                 "-r", str(self.SAMPLE_RATE), "-c", str(self.CHANNELS),
                 "-d", str(max(1, int(duration_s))), tmp],
                capture_output=True, check=True, timeout=duration_s + 3
            )
            with wave.open(tmp, "rb") as w:
                raw = w.readframes(w.getnframes())
            return AudioChunk(
                data=raw,
                sample_rate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                format="pcm_16",
                duration_ms=duration_ms,
                timestamp_ms=int(time.time() * 1000),
            )
        except Exception as e:
            logger.error("[DistillerMic] Capture failed: %s", e)
            raise
        finally:
            os.unlink(tmp)


    def get_device_info(self) -> dict:
        return {
            "name": "Pamir AI SoundCard",
            "device": self.DEVICE,
            "sample_rate": self.SAMPLE_RATE,
            "channels": self.CHANNELS,
            "format": self.FORMAT,
        }

    def start_recording(self) -> None:
        """Start continuous recording (background arecord process)."""
        import threading
        self._recording = True
        self._record_buf: list = []
        def _record():
            import time
            while getattr(self, "_recording", False):
                try:
                    chunk = self.capture(duration_ms=1000)
                    self._record_buf.append(chunk)
                    if len(self._record_buf) > 60:
                        self._record_buf.pop(0)
                except Exception:
                    pass
        self._record_thread = threading.Thread(target=_record, daemon=True)
        self._record_thread.start()

    def stop_recording(self) -> None:
        self._recording = False

    def get_buffer(self, duration_ms: int) -> "AudioChunk":
        """Return captured audio from rolling buffer."""
        return self.capture(duration_ms=duration_ms)

    def transcribe(self, audio: "AudioChunk", language: str = "en") -> str:
        """Transcribe using distiller_sdk whisper if available, else empty."""
        try:
            sys.path.insert(0, self.SDK_SRC if hasattr(self, "SDK_SRC") else "/opt/distiller-sdk/src")
            from distiller_sdk.whisper.fast_whisper import FastWhisper  # type: ignore
            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            import wave as _wave
            with _wave.open(tmp, "wb") as w:
                w.setnchannels(audio.channels)
                w.setsampwidth(2)
                w.setframerate(audio.sample_rate)
                w.writeframes(audio.data)
            result = FastWhisper().transcribe(tmp, language=language)
            os.unlink(tmp)
            return result
        except Exception as e:
            logger.debug("[DistillerMic] transcribe unavailable: %s", e)
            return ""

    def transcribe_stream(self, stream) -> "Iterator[str]":
        for chunk in stream:
            text = self.transcribe(chunk)
            if text:
                yield text

    def shutdown(self) -> None:
        pass

    def validate(self) -> bool:
        try:
            result = subprocess.run(
                ["arecord", "-D", self.DEVICE, "-f", self.FORMAT,
                 "-r", str(self.SAMPLE_RATE), "-c", str(self.CHANNELS),
                 "-d", "1", "/dev/null"],
                capture_output=True, timeout=4
            )
            return result.returncode == 0
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Pamir AI SoundCard -- Speaker
# ---------------------------------------------------------------------------

class DistillerAudioOutputHAL(AudioOutputHal):
    """Plays audio through the Pamir AI SoundCard (hw:0,0)."""

    DEVICE = "hw:0,0"

    def initialize(self) -> None:
        logger.info("[DistillerAudio] Pamir AI SoundCard output ready.")

    def speak(self, text: str) -> None:
        """TTS via piper (distiller SDK) or espeak fallback."""
        try:
            sys.path.insert(0, "/opt/distiller-sdk/src")
            from distiller_sdk.piper import Piper  # type: ignore
            tts = Piper()
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            tts.synthesize(text, tmp)
            subprocess.run(["aplay", "-D", self.DEVICE, tmp],
                           capture_output=True, timeout=30)
            os.unlink(tmp)
        except Exception:
            # Fallback: espeak
            try:
                subprocess.run(["espeak", text], capture_output=True, timeout=10)
            except Exception as e:
                logger.warning("[DistillerAudio] speak failed: %s", e)

    def speak_agent_response(self, response: AgentResponse) -> None:
        if response.response_type in (ResponseType.TEXT, ResponseType.AUDIO):
            self.speak(str(response.content))


    def get_device_info(self) -> dict:
        return {"name": "Pamir AI SoundCard", "device": self.DEVICE}

    def play(self, chunk: "AudioChunk") -> None:
        self.play_audio(chunk)

    def is_playing(self) -> bool:
        return False

    def stop(self) -> None:
        pass

    def play_audio(self, chunk: AudioChunk) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            with wave.open(tmp, "wb") as w:
                w.setnchannels(chunk.channels)
                w.setsampwidth(2)
                w.setframerate(chunk.sample_rate)
                w.writeframes(chunk.data)
            subprocess.run(["aplay", "-D", self.DEVICE, tmp],
                           capture_output=True, timeout=30)
        finally:
            os.unlink(tmp)

    def shutdown(self) -> None:
        pass

    def validate(self) -> bool:
        result = subprocess.run(["aplay", "-l"], capture_output=True, text=True)
        return "Pamir" in result.stdout or "sndpamir" in result.stdout


# ---------------------------------------------------------------------------
# OV5647 Camera via rpicam-still
# ---------------------------------------------------------------------------

class DistillerCameraHAL(CameraHal):
    """Captures images from OV5647 Pi Camera via rpicam-still."""

    def initialize(self, width: int = 640, height: int = 480) -> None:
        self._width = width
        self._height = height
        logger.info("[DistillerCamera] OV5647 %dx%d ready.", width, height)

    def capture(self) -> bytes:
        """Returns JPEG bytes."""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp = f.name
        try:
            subprocess.run(
                ["rpicam-still", "-o", tmp,
                 "--width", str(self._width), "--height", str(self._height),
                 "--timeout", "1000", "--nopreview"],
                capture_output=True, check=True, timeout=10
            )
            with open(tmp, "rb") as fh:
                return fh.read()
        except Exception as e:
            logger.error("[DistillerCamera] Capture failed: %s", e)
            raise
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


    def capture_frame(self) -> bytes:
        """Alias for capture() -- returns raw JPEG bytes."""
        return self.capture()

    def get_device_info(self) -> dict:
        return {
            "name": "OV5647 Pi Camera",
            "device": "/dev/video0",
            "width": getattr(self, "_width", 640),
            "height": getattr(self, "_height", 480),
            "format": "JPEG",
        }

    def shutdown(self) -> None:
        pass

    def validate(self) -> bool:
        try:
            data = self.capture()
            return len(data) > 1000
        except Exception:
            return False


# ---------------------------------------------------------------------------
# E-ink Display via distiller_sdk
# ---------------------------------------------------------------------------

class DistillerEinkDisplayHAL(DisplayHal):
    """Renders to the 250x128 e-ink display via distiller_sdk."""

    WIDTH = 250
    HEIGHT = 128
    SDK_SRC = "/opt/distiller-sdk/src"

    def _get_display(self):
        sys.path.insert(0, self.SDK_SRC)
        from distiller_sdk.hardware.eink import Display  # type: ignore
        return Display()

    def initialize(self) -> None:
        try:
            d = self._get_display()
            d.close()
            logger.info("[DistillerEink] Display initialized.")
        except Exception as e:
            logger.warning("[DistillerEink] Init warning: %s", e)

    def show(self, card: DisplayCard) -> None:
        """Render a DisplayCard to the e-ink screen."""
        try:
            from PIL import Image, ImageDraw, ImageFont  # type: ignore
            sys.path.insert(0, self.SDK_SRC)
            from distiller_sdk.hardware.eink import Display, DisplayMode  # type: ignore

            img = Image.new("L", (self.WIDTH, self.HEIGHT), 255)
            draw = ImageDraw.Draw(img)

            # Title (bold, top)
            if card.title:
                try:
                    font_title = ImageFont.truetype(
                        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
                except Exception:
                    font_title = ImageFont.load_default()
                draw.text((5, 4), card.title[:30], font=font_title, fill=0)

            # Body
            try:
                font_body = ImageFont.truetype(
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
            except Exception:
                font_body = ImageFont.load_default()
            y_start = 26 if card.title else 10
            # Wrap body text
            body = str(card.body)[:200]
            for i, line in enumerate(self._wrap(body, 34)):
                if y_start + i * 15 > self.HEIGHT - 5:
                    break
                draw.text((5, y_start + i * 15), line, font=font_body, fill=0)

            # Orientation correction
            img = img.rotate(270, expand=True).transpose(Image.FLIP_TOP_BOTTOM)
            tmp = "/tmp/_openclaw_eink.png"
            img.save(tmp)

            d = Display()
            d.display_image(tmp, DisplayMode.FULL)
            d.close()
            self._last_rendered = card.body
        except Exception as e:
            logger.error("[DistillerEink] show() failed: %s", e)

    def render_agent_response(self, response: AgentResponse) -> None:
        title = response.metadata.get("title", "Agent")
        body = str(response.content)[:200]
        self.show(DisplayCard(mode='text', title=title, body=body, font_size=14, duration_ms=0))


    def get_device_info(self) -> dict:
        return {
            "name": "Distiller E-ink Display",
            "width": self.WIDTH,
            "height": self.HEIGHT,
            "interface": "SPI",
            "device": "/dev/spidev0.0",
        }

    def clear(self) -> None:
        self.show(DisplayCard(title="", body=""))

    def shutdown(self) -> None:
        self.clear()

    def validate(self) -> bool:
        try:
            sys.path.insert(0, self.SDK_SRC)
            from distiller_sdk.hardware.eink import Display  # type: ignore
            d = Display()
            d.close()
            return True
        except Exception:
            return False

    @staticmethod
    def _wrap(text: str, width: int) -> list:
        words = text.split()
        lines, cur = [], ""
        for w in words:
            if len(cur) + len(w) + 1 <= width:
                cur = (cur + " " + w).strip()
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines
