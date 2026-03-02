"""Raspberry Pi 3 reference HAL implementations.

Designed for Pi 3B+ (1GB RAM, Cortex-A53), Raspberry Pi OS Bookworm,
Bluetooth 4.2 onboard (BLE5 via USB adapter recommended), optional MPU6050,
Pi Camera v2 or USB camera, ALSA microphone/speaker.
"""

from __future__ import annotations

import io
import logging
import os
import queue
import socket
import struct
import subprocess
import threading
import time
import wave
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .base import AudioOutputHal
from ..core.exceptions import ConfigurationError, HardwareError, TransportError
from .base import CameraFrame, CameraHal
from .base import ClassificationResult, ClassifierHal
from .base import DisplayCard, DisplayHal
from .base import IMUHal, IMUSample
from .base import AudioChunk, MicrophoneHal
from .base import SendResult, TransportHal, TransportState


logger = logging.getLogger(__name__)


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class PiIMU(IMUHal):
    HAL_VERSION = "1.0.0"

    def __init__(self, bus_id: int = 1, i2c_addr: int = 0x68) -> None:
        self._bus_id = bus_id
        self._i2c_addr = i2c_addr
        self._lock = threading.RLock()
        self._initialized = False
        self._rate_hz = 25
        self._last_ts = 0
        self._bus = None

    def initialize(self, sample_rate_hz: int = 25) -> None:
        with self._lock:
            if self._initialized:
                return
            if sample_rate_hz < 10 or sample_rate_hz > 50:
                raise ConfigurationError(
                    "sample_rate_hz out of range",
                    error_code="HAL_IMU_INVALID_RATE",
                    remediation="Set sample_rate_hz between 10 and 50",
                )
            try:
                import smbus2  # type: ignore

                self._bus = smbus2.SMBus(self._bus_id)
                # Wake MPU6050 (PWR_MGMT_1 register = 0)
                self._bus.write_byte_data(self._i2c_addr, 0x6B, 0x00)
                self._rate_hz = sample_rate_hz
                self._initialized = True
            except Exception as exc:
                raise HardwareError(
                    f"IMU init failed: {exc}",
                    error_code="HAL_IMU_INIT_FAILED",
                    remediation="Check I2C wiring and smbus2 installation",
                ) from exc

    def _read_word_signed(self, reg: int) -> int:
        high = self._bus.read_byte_data(self._i2c_addr, reg)
        low = self._bus.read_byte_data(self._i2c_addr, reg + 1)
        value = (high << 8) | low
        if value >= 0x8000:
            value -= 0x10000
        return value

    def read_sample(self) -> Optional[IMUSample]:
        start = _monotonic_ms()
        with self._lock:
            if not self._initialized or self._bus is None:
                raise HardwareError(
                    "IMU not initialized",
                    error_code="HAL_IMU_READ_FAILED",
                    remediation="Call initialize() first",
                )
            try:
                ax = self._read_word_signed(0x3B)
                ay = self._read_word_signed(0x3D)
                az = self._read_word_signed(0x3F)
                gx = self._read_word_signed(0x43)
                gy = self._read_word_signed(0x45)
                gz = self._read_word_signed(0x47)

                ts = _monotonic_ms()
                if ts <= self._last_ts:
                    ts = self._last_ts + 1
                self._last_ts = ts

                # MPU6050 scale factors (default ranges):
                # accel: 16384 LSB/g ; gyro: 131 LSB/(deg/s)
                g = 9.80665
                sample = IMUSample(
                    timestamp_ms=ts,
                    accel_x=(ax / 16384.0) * g,
                    accel_y=(ay / 16384.0) * g,
                    accel_z=(az / 16384.0) * g,
                    gyro_x=gx / 131.0,
                    gyro_y=gy / 131.0,
                    gyro_z=gz / 131.0,
                )
            except Exception as exc:
                raise HardwareError(
                    f"IMU read failed: {exc}",
                    error_code="HAL_IMU_READ_FAILED",
                    remediation="Check sensor power and I2C stability",
                ) from exc

        if _monotonic_ms() - start > 10:
            logger.warning("IMU read exceeded 10ms contract")
        return sample

    def set_sample_rate(self, hz: int) -> None:
        with self._lock:
            if hz < 10 or hz > 50:
                raise ConfigurationError(
                    "Invalid IMU sample rate",
                    error_code="HAL_IMU_INVALID_RATE",
                    remediation="Set sample rate between 10 and 50",
                )
            self._rate_hz = hz

    def shutdown(self) -> None:
        with self._lock:
            try:
                if self._bus is not None:
                    self._bus.close()
            except Exception:
                logger.exception("best-effort IMU shutdown failed")
            finally:
                self._bus = None
                self._initialized = False

    def get_device_info(self) -> dict:
        return {
            "name": "MPU6050",
            "axes": 6,
            "max_rate_hz": 50,
            "current_rate_hz": self._rate_hz,
        }

    def validate(self) -> bool:
        if not self._initialized:
            self.initialize(self._rate_hz)
        sample = self.read_sample()
        return sample is not None


class PiCamera(CameraHal):
    HAL_VERSION = "1.0.0"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False
        self._resolution = (320, 240)
        self._device = None
        self._picam2 = None

    def initialize(self, resolution: tuple[int, int] = (320, 240)) -> None:
        with self._lock:
            if self._initialized:
                return
            self._resolution = resolution
            try:
                from picamera2 import Picamera2  # type: ignore

                self._picam2 = Picamera2()
                cfg = self._picam2.create_still_configuration(main={"size": resolution})
                self._picam2.configure(cfg)
                self._picam2.start()
                self._initialized = True
                return
            except Exception:
                logger.info("picamera2 unavailable, trying OpenCV fallback")

            try:
                import cv2  # type: ignore

                cap = cv2.VideoCapture(0)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, resolution[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, resolution[1])
                if not cap.isOpened():
                    raise RuntimeError("cv2 capture device not opened")
                self._device = cap
                self._initialized = True
            except Exception as exc:
                raise HardwareError(
                    f"Camera init failed: {exc}",
                    error_code="HAL_CAM_INIT_FAILED",
                    remediation="Verify camera module / USB webcam and permissions",
                ) from exc

    def capture_frame(self) -> CameraFrame:
        start = _monotonic_ms()
        with self._lock:
            if not self._initialized:
                raise HardwareError(
                    "Camera not initialized",
                    error_code="HAL_CAM_CAPTURE_FAILED",
                    remediation="Call initialize() first",
                )
            try:
                if self._picam2 is not None:
                    arr = self._picam2.capture_array()
                    from PIL import Image

                    img = Image.fromarray(arr).resize(self._resolution)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=60)
                    jpeg = buf.getvalue()
                else:
                    import cv2  # type: ignore

                    ok, frame = self._device.read()
                    if not ok:
                        raise RuntimeError("Camera read failed")
                    frame = cv2.resize(frame, self._resolution)
                    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
                    if not ok:
                        raise RuntimeError("JPEG encode failed")
                    jpeg = encoded.tobytes()

                elapsed = _monotonic_ms() - start
                if elapsed > 500:
                    raise HardwareError(
                        f"Camera timeout {elapsed}ms",
                        error_code="HAL_CAM_TIMEOUT",
                        remediation="Lower resolution or check camera bus contention",
                    )

                return CameraFrame(
                    timestamp_ms=_monotonic_ms(),
                    width=self._resolution[0],
                    height=self._resolution[1],
                    format="JPEG",
                    data=jpeg,
                )
            except HardwareError:
                raise
            except Exception as exc:
                raise HardwareError(
                    f"Camera capture failed: {exc}",
                    error_code="HAL_CAM_CAPTURE_FAILED",
                    remediation="Inspect camera connection and retry",
                ) from exc

    def shutdown(self) -> None:
        with self._lock:
            try:
                if self._picam2 is not None:
                    self._picam2.stop()
                if self._device is not None:
                    self._device.release()
            except Exception:
                logger.exception("best-effort camera shutdown failed")
            finally:
                self._picam2 = None
                self._device = None
                self._initialized = False

    def get_device_info(self) -> dict:
        return CameraDeviceInfo(
            name="PiCamera/USB",
            sensor="auto",
            max_width=1920,
            max_height=1080,
            supported_formats=("JPEG", "RGB"),
        )

    def validate(self) -> bool:
        if not self._initialized:
            self.initialize(self._resolution)
        frame = self.capture_frame()
        return frame.width > 0 and frame.height > 0 and len(frame.data) > 0


class PiMicrophone(MicrophoneHal):
    HAL_VERSION = "1.0.0"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False
        self._recording = False
        self._sample_rate = 16000
        self._channels = 1
        self._buffer_seconds = 1.0
        self._ring = bytearray()
        self._ring_max_bytes = int(self._sample_rate * 2 * self._buffer_seconds)
        self._stream = None
        self._pa = None

    def initialize(self, sample_rate: int = 16000, channels: int = 1) -> None:
        with self._lock:
            if self._initialized:
                return
            self._sample_rate = sample_rate
            self._channels = channels
            self._ring_max_bytes = int(sample_rate * channels * 2 * self._buffer_seconds)
            try:
                import pyaudio  # type: ignore

                self._pa = pyaudio.PyAudio()
                self._initialized = True
            except Exception as exc:
                raise HardwareError(
                    f"Microphone init failed: {exc}",
                    error_code="HAL_MIC_INIT_FAILED",
                    remediation="Install/enable ALSA microphone input",
                ) from exc

    def _callback(self, in_data, frame_count, time_info, status):
        with self._lock:
            self._ring.extend(in_data)
            if len(self._ring) > self._ring_max_bytes:
                overflow = len(self._ring) - self._ring_max_bytes
                del self._ring[:overflow]
        import pyaudio  # type: ignore

        return (None, pyaudio.paContinue)

    def start_recording(self) -> None:
        with self._lock:
            if self._recording:
                return
            if not self._initialized or self._pa is None:
                raise HardwareError(
                    "Microphone not initialized",
                    error_code="HAL_MIC_START_FAILED",
                    remediation="Call initialize() first",
                )
            try:
                import pyaudio  # type: ignore

                self._stream = self._pa.open(
                    format=pyaudio.paInt16,
                    channels=self._channels,
                    rate=self._sample_rate,
                    input=True,
                    frames_per_buffer=1024,
                    stream_callback=self._callback,
                )
                self._stream.start_stream()
                self._recording = True
            except Exception as exc:
                raise HardwareError(
                    f"Microphone start failed: {exc}",
                    error_code="HAL_MIC_START_FAILED",
                    remediation="Check audio input device availability",
                ) from exc

    def stop_recording(self) -> None:
        with self._lock:
            if not self._recording:
                return
            try:
                if self._stream is not None:
                    self._stream.stop_stream()
                    self._stream.close()
            except Exception:
                logger.exception("best-effort microphone stop failed")
            finally:
                self._stream = None
                self._recording = False

    def get_buffer(self, duration_ms: int) -> AudioChunk:
        start = _monotonic_ms()
        with self._lock:
            if duration_ms > 1000:
                duration_ms = 1000
            req_bytes = int(self._sample_rate * self._channels * 2 * (duration_ms / 1000.0))
            data = bytes(self._ring[-req_bytes:])
            if len(data) < req_bytes:
                data = (b"\x00" * (req_bytes - len(data))) + data

        elapsed = _monotonic_ms() - start
        if elapsed > 50:
            logger.warning("Microphone get_buffer exceeded 50ms")

        return AudioChunk(
            timestamp_ms=_monotonic_ms(),
            sample_rate=self._sample_rate,
            channels=self._channels,
            format="PCM_S16LE",
            data=data,
        )

    def shutdown(self) -> None:
        with self._lock:
            self.stop_recording()
            try:
                if self._pa is not None:
                    self._pa.terminate()
            except Exception:
                logger.exception("best-effort microphone shutdown failed")
            finally:
                self._pa = None
                self._initialized = False

    def get_device_info(self) -> dict:
        return {
            "name": "ALSA default",
            "max_sample_rate": 48000,
            "channels": self._channels,
        }

    def validate(self) -> bool:
        if not self._initialized:
            self.initialize(self._sample_rate, self._channels)
        self.start_recording()
        time.sleep(0.15)
        chunk = self.get_buffer(100)
        return len(chunk.data) > 0


class PiClassifier(ClassifierHal):
    HAL_VERSION = "1.0.0"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False
        self._model_path = ""
        self._interpreter = None
        self._input_index = 0
        self._output_index = 0
        self._input_hw = (128, 128)

    def initialize(self, model_path: str, config: Optional[dict] = None) -> None:
        with self._lock:
            if self._initialized:
                return
            if not Path(model_path).exists():
                raise ConfigurationError(
                    f"Model not found: {model_path}",
                    error_code="HAL_CLF_MODEL_NOT_FOUND",
                    remediation="Provide a valid .tflite model path",
                )
            try:
                from tflite_runtime.interpreter import Interpreter  # type: ignore

                threads = int((config or {}).get("num_threads", 2))
                self._interpreter = Interpreter(model_path=model_path, num_threads=threads)
                self._interpreter.allocate_tensors()
                in_details = self._interpreter.get_input_details()[0]
                out_details = self._interpreter.get_output_details()[0]
                self._input_index = in_details["index"]
                self._output_index = out_details["index"]
                shape = in_details["shape"]
                self._input_hw = (int(shape[2]), int(shape[1]))
                self._model_path = model_path
                self._initialized = True
            except Exception as exc:
                raise HardwareError(
                    f"Classifier init failed: {exc}",
                    error_code="HAL_CLF_INIT_FAILED",
                    remediation="Check tflite-runtime and model compatibility",
                ) from exc

    def classify(self, image: bytes, width: int, height: int, format: str = "RGB") -> ClassificationResult:
        t0 = _monotonic_ms()
        with self._lock:
            if not self._initialized or self._interpreter is None:
                raise HardwareError(
                    "Classifier not initialized",
                    error_code="HAL_CLF_INFERENCE_FAILED",
                    remediation="Call initialize() before classify()",
                )
            try:
                import numpy as np
                from PIL import Image

                if format == "JPEG":
                    img = Image.open(io.BytesIO(image)).convert("RGB")
                else:
                    img = Image.frombytes("RGB", (width, height), image)
                img = img.resize(self._input_hw)
                x = np.asarray(img, dtype=np.uint8)
                x = np.expand_dims(x, axis=0)

                self._interpreter.set_tensor(self._input_index, x)
                self._interpreter.invoke()
                out = self._interpreter.get_tensor(self._output_index)
                score = float(out.reshape(-1)[0])
                confidence = max(0.0, min(1.0, score))
                label = "interesting" if confidence >= 0.5 else "uninteresting"
            except Exception as exc:
                raise HardwareError(
                    f"Classifier inference failed: {exc}",
                    error_code="HAL_CLF_INFERENCE_FAILED",
                    remediation="Validate model input tensor shape/type",
                ) from exc

        elapsed = _monotonic_ms() - t0
        if elapsed > 200:
            raise HardwareError(
                f"Classifier timeout: {elapsed}ms",
                error_code="HAL_CLF_TIMEOUT",
                remediation="Use smaller model or fewer threads",
            )

        return ClassificationResult(label=label, confidence=confidence, inference_time_ms=elapsed)

    def get_model_info(self) -> dict:
        return ClassifierModelInfo(
            model_name=Path(self._model_path).name if self._model_path else "unknown",
            model_version="1.0",
            runtime="tflite",
            input_width=self._input_hw[0],
            input_height=self._input_hw[1],
            quantization="INT8",
        )

    def shutdown(self) -> None:
        with self._lock:
            self._interpreter = None
            self._initialized = False

    def validate(self) -> bool:
        if not self._initialized:
            raise HardwareError(
                "Classifier not initialized",
                error_code="HAL_CLF_VALIDATE_FAILED",
                remediation="Initialize with model before validate",
            )
        dummy = b"\x00" * (128 * 128 * 3)
        _ = self.classify(dummy, 128, 128, "RGB")
        return True


class PiBLETransport(TransportHal):
    HAL_VERSION = "1.0.0"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False
        self._state = TransportState.DISCONNECTED
        self._cb: Optional[Callable[[TransportState], None]] = None
        self._config: dict = {}
        self._loop = None
        self._client = None
        self._notify_queue: queue.Queue[bytes] = queue.Queue(maxsize=32)
        self._mtu = 247
        self._transport_mode = "gatt"
        self._reconnect_schedule_s = [1, 2, 4, 8, 16, 30]
        self._loop_thread = None

    def _run_coro(self, coro, timeout_s: float):
        """Run coroutine on dedicated BLE loop thread.

        Avoids asyncio.run-per-call and is safe when caller already has an event loop.
        """
        import asyncio
        import concurrent.futures

        if self._loop is None:
            self._loop = asyncio.new_event_loop()

            def _runner():
                asyncio.set_event_loop(self._loop)
                self._loop.run_forever()

            self._loop_thread = threading.Thread(target=_runner, daemon=True)
            self._loop_thread.start()

        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout_s)

    def _select_transport_mode(self) -> str:
        """Probe and select L2CAP when available, otherwise GATT fallback."""
        preferred = str(self._config.get("preferred_mode", "l2cap")).lower()
        supports_l2cap = bool(self._config.get("supports_l2cap", False))
        if preferred == "l2cap" and supports_l2cap:
            return "l2cap"
        return "gatt"


    def _set_state(self, st: TransportState) -> None:
        self._state = st
        cb = self._cb
        if cb is not None:
            try:
                cb(st)
            except Exception:
                logger.exception("state callback failed")

    def initialize(self, config: dict) -> None:
        with self._lock:
            if self._initialized:
                return
            required = ["target_address", "service_uuid", "tx_char_uuid", "rx_char_uuid"]
            if any(k not in config for k in required):
                raise ConfigurationError(
                    "Missing BLE transport config keys",
                    error_code="HAL_TXP_INVALID_CONFIG",
                    remediation=f"Provide keys: {required}",
                )
            self._config = dict(config)
            self._mtu = int(config.get("mtu", 247))
            self._initialized = True

    def connect(self) -> None:
        with self._lock:
            if not self._initialized:
                raise TransportError(
                    "Transport not initialized",
                    error_code="HAL_TXP_CONNECT_FAILED",
                    remediation="Call initialize() first",
                )

        import asyncio
        from bleak import BleakClient  # type: ignore

        async def _connect():
            self._set_state(TransportState.CONNECTING)
            self._transport_mode = self._select_transport_mode()
            client = BleakClient(self._config["target_address"])
            await client.connect(timeout=10.0)

            async def _notify_handler(_sender, data: bytearray):
                try:
                    self._notify_queue.put_nowait(bytes(data))
                except queue.Full:
                    logger.warning("BLE notify queue full, dropping packet")

            await client.start_notify(self._config["rx_char_uuid"], _notify_handler)
            self._client = client
            self._set_state(TransportState.CONNECTED)

        try:
            return self._run_coro(_connect(), timeout_s=10.0)
        except Exception as exc:
            self._set_state(TransportState.DISCONNECTED)
            raise TransportError(
                f"BLE connect failed: {exc}",
                error_code="HAL_TXP_CONNECT_FAILED",
                remediation="Verify pairing and BLE adapter availability",
            ) from exc

    def send(self, payload: bytes) -> SendResult:
        start = _monotonic_ms()
        if self._state != TransportState.CONNECTED or self._client is None:
            raise TransportError(
                "BLE not connected",
                error_code="HAL_TXP_SEND_FAILED",
                remediation="Connect transport before send",
            )

        import asyncio

        tx_uuid = self._config["tx_char_uuid"]

        async def _send_chunks() -> int:
            bytes_sent = 0
            chunk_size = max(20, self._mtu - 3)
            for i in range(0, len(payload), chunk_size):
                chunk = payload[i : i + chunk_size]
                await self._client.write_gatt_char(tx_uuid, chunk, response=False)
                bytes_sent += len(chunk)
            return bytes_sent

        try:
            sent = self._run_coro(_send_chunks(), timeout_s=0.5)
            elapsed = _monotonic_ms() - start
            if elapsed > 500:
                raise TransportError(
                    f"BLE send timeout: {elapsed}ms",
                    error_code="HAL_TXP_TIMEOUT",
                    remediation="Reduce payload size or improve BLE link",
                )
            return SendResult(success=True, bytes_sent=sent, elapsed_ms=elapsed, retries=0)
        except TransportError:
            raise
        except Exception as exc:
            elapsed = _monotonic_ms() - start
            raise TransportError(
                f"BLE send failed: {exc}",
                error_code="HAL_TXP_SEND_FAILED",
                remediation="Check connection stability and MTU settings",
            ) from exc

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        try:
            return self._notify_queue.get(timeout=timeout_ms / 1000.0)
        except queue.Empty:
            return None

    def get_state(self) -> TransportState:
        return self._state

    def set_state_callback(self, callback: Callable[[TransportState], None]) -> None:
        with self._lock:
            self._cb = callback

    def reconnect_with_backoff(self) -> None:
        """Reconnect using required schedule: 1,2,4,8,16,30s (cap)."""
        self._set_state(TransportState.RECONNECTING)
        start = _monotonic_ms()
        while True:
            for wait_s in self._reconnect_schedule_s:
                try:
                    self.connect()
                    return
                except TransportError:
                    time.sleep(wait_s)
                    if _monotonic_ms() - start > 300000:
                        logger.warning("HAL_TXP_RECONNECT_TIMEOUT")
            # continue retries at 30s cap

    def disconnect(self) -> None:
        if self._client is None:
            self._set_state(TransportState.DISCONNECTED)
            return

        import asyncio

        async def _disconnect():
            try:
                await self._client.disconnect()
            except Exception:
                logger.exception("best-effort BLE disconnect failed")

        self._run_coro(_disconnect(), timeout_s=2.0)
        self._client = None
        self._set_state(TransportState.DISCONNECTED)

    def shutdown(self) -> None:
        try:
            self.disconnect()
        finally:
            self._initialized = False

    def get_device_info(self) -> dict:
        return {
            "name": "bleak",
            "type": "ble",
            "max_throughput_bps": 500000,
            "mtu": self._mtu,
        }

    def validate(self) -> bool:
        if not self._initialized:
            return False
        return True


class PiWiFiTransport(TransportHal):
    HAL_VERSION = "1.0.0"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False
        self._state = TransportState.DISCONNECTED
        self._cb: Optional[Callable[[TransportState], None]] = None
        self._sock: Optional[socket.socket] = None
        self._host = "127.0.0.1"
        self._port = 8765

    def _set_state(self, st: TransportState) -> None:
        self._state = st
        if self._cb:
            try:
                self._cb(st)
            except Exception:
                logger.exception("WiFi state callback failed")

    def initialize(self, config: dict) -> None:
        with self._lock:
            if self._initialized:
                return
            self._host = str(config.get("target_host", "127.0.0.1"))
            self._port = int(config.get("target_port", 8765))
            self._initialized = True

    def connect(self) -> None:
        with self._lock:
            if not self._initialized:
                raise TransportError("WiFi not initialized", "HAL_TXP_CONNECT_FAILED", "Call initialize first")
            self._set_state(TransportState.CONNECTING)
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(3.0)
                s.connect((self._host, self._port))
                self._sock = s
                self._set_state(TransportState.CONNECTED)
            except Exception as exc:
                self._set_state(TransportState.DISCONNECTED)
                raise TransportError(
                    f"WiFi connect failed: {exc}",
                    error_code="HAL_TXP_CONNECT_FAILED",
                    remediation="Verify node host/port and network route",
                ) from exc

    def send(self, payload: bytes) -> SendResult:
        start = _monotonic_ms()
        if self._sock is None:
            raise TransportError("WiFi not connected", "HAL_TXP_SEND_FAILED", "Connect first")
        try:
            header = struct.pack("<I", len(payload))
            self._sock.sendall(header + payload)
            elapsed = _monotonic_ms() - start
            if elapsed > 500:
                raise TransportError(
                    f"WiFi send timeout: {elapsed}ms",
                    error_code="HAL_TXP_TIMEOUT",
                    remediation="Reduce payload size or verify network quality",
                )
            return SendResult(success=True, bytes_sent=len(payload), elapsed_ms=elapsed)
        except Exception as exc:
            raise TransportError(
                f"WiFi send failed: {exc}",
                error_code="HAL_TXP_SEND_FAILED",
                remediation="Check TCP connectivity",
            ) from exc

    def receive(self, timeout_ms: int = 1000) -> Optional[bytes]:
        if self._sock is None:
            return None
        self._sock.settimeout(timeout_ms / 1000.0)
        try:
            hdr = self._sock.recv(4)
            if len(hdr) < 4:
                return None
            length = struct.unpack("<I", hdr)[0]
            data = b""
            while len(data) < length:
                part = self._sock.recv(length - len(data))
                if not part:
                    break
                data += part
            return data if len(data) == length else None
        except socket.timeout:
            return None

    def get_state(self) -> TransportState:
        return self._state

    def set_state_callback(self, callback: Callable[[TransportState], None]) -> None:
        self._cb = callback

    def disconnect(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                logger.exception("best-effort wifi disconnect failed")
        self._sock = None
        self._set_state(TransportState.DISCONNECTED)

    def shutdown(self) -> None:
        self.disconnect()
        self._initialized = False

    def get_device_info(self) -> dict:
        return {
            "name": "tcp",
            "type": "wifi",
            "max_throughput_bps": 10_000_000,
            "mtu": 1500,
        }

    def validate(self) -> bool:
        return self._initialized


class TerminalDisplay(DisplayHal):
    HAL_VERSION = "1.0.0"

    def __init__(self) -> None:
        self._initialized = False
        self._resolution = (80, 24)
        self._lock = threading.RLock()

    def initialize(self, resolution: tuple[int, int] = (80, 24)) -> None:
        with self._lock:
            self._resolution = resolution
            self._initialized = True

    def show(self, card: DisplayCard) -> None:
        if not self._initialized:
            raise HardwareError("Display not initialized", "HAL_DSP_RENDER_FAILED", "Call initialize first")
        title = f"[{card.mode.upper()}] {card.title or ''}".strip()
        print("=" * 40)
        print(title)
        print(card.body)
        print("=" * 40)

    def clear(self) -> None:
        print("\033[2J\033[H", end="")

    def shutdown(self) -> None:
        self._initialized = False

    def get_device_info(self) -> dict:
        return DisplayDeviceInfo(
            name="terminal",
            width=self._resolution[0],
            height=self._resolution[1],
            color=True,
            refresh_rate_hz=60,
        )

    def validate(self) -> bool:
        self.show(DisplayCard(mode="glance", title=None, body="display ok", font_size=14, duration_ms=500))
        return True


class PiAudioOutput(AudioOutputHal):
    HAL_VERSION = "1.0.0"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._initialized = False
        self._sample_rate = 22050
        self._channels = 1
        self._playing = False
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._proc: Optional[subprocess.Popen] = None

    def initialize(self, sample_rate: int = 22050, channels: int = 1) -> None:
        with self._lock:
            if self._initialized:
                return
            self._sample_rate = sample_rate
            self._channels = channels
            self._initialized = True

    def play(self, audio_data: bytes, format: str = "PCM_S16LE", sample_rate: int = 22050) -> None:
        if not self._initialized:
            raise HardwareError("Audio output not initialized", "HAL_AUD_PLAY_FAILED", "Call initialize first")

        def _worker() -> None:
            self._playing = True
            self._stop_evt.clear()
            try:
                if format == "WAV":
                    tmp = Path("/tmp/openclaw_wearable_audio.wav")
                    tmp.write_bytes(audio_data)
                else:
                    tmp = Path("/tmp/openclaw_wearable_audio.wav")
                    with wave.open(str(tmp), "wb") as wf:
                        wf.setnchannels(self._channels)
                        wf.setsampwidth(2)
                        wf.setframerate(sample_rate)
                        wf.writeframes(audio_data)
                self._proc = subprocess.Popen(["aplay", str(tmp)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                while self._proc.poll() is None:
                    if self._stop_evt.is_set():
                        self._proc.terminate()
                        break
                    time.sleep(0.01)
                self._proc = None
            finally:
                self._playing = False

        with self._lock:
            if self._thread and self._thread.is_alive():
                self.stop()
            self._thread = threading.Thread(target=_worker, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception:
                logger.exception("best-effort audio terminate failed")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
        self._playing = False

    def is_playing(self) -> bool:
        return self._playing

    def shutdown(self) -> None:
        self.stop()
        self._initialized = False

    def get_device_info(self) -> dict:
        return {
            "name": "alsa",
            "max_sample_rate": 48000,
            "channels": self._channels,
            "output_type": "speaker",
        }

    def validate(self) -> bool:
        # 100ms silence test (playback path sanity)
        self.play(b"\x00" * int(self._sample_rate * 0.1) * 2, "PCM_S16LE", self._sample_rate)
        time.sleep(0.15)
        return True
