"""Main SDK orchestration pipeline."""

import threading
import time
from typing import Callable, List, Optional, Tuple

from ..context.models import AgentResponse, ContextPayload
from ..hal.base import AudioOutputHal, CameraHal, ClassifierHal, DisplayCard, DisplayHal, IMUHal, MicrophoneHal, SendResult, TransportHal
from ..transport.ble import PacketSerializer
from .exceptions import ConfigurationError, IncompatibleHALError
from .response import AgentResponseListener, DeviceResponseRouter
from .trigger import TriggerConfig, TriggerDetector, TriggerEvent


class HALRegistry:
    """Registry and validator for required and optional HAL instances."""

    def __init__(self) -> None:
        self.imu = None  # type: Optional[IMUHal]
        self.camera = None  # type: Optional[CameraHal]
        self.microphone = None  # type: Optional[MicrophoneHal]
        self.classifier = None  # type: Optional[ClassifierHal]
        self.transports = []  # type: List[Tuple[int, TransportHal]]
        self.display = None  # type: Optional[DisplayHal]
        self.audio_output = None  # type: Optional[AudioOutputHal]

    def _check_hal_version(self, hal: object) -> None:
        major = str(getattr(hal, "HAL_VERSION", "0.0.0")).split(".")[0]
        if major != "1":
            raise IncompatibleHALError("incompatible HAL", "HAL_VERSION_MISMATCH", "Use HAL version 1.x")

    def register_imu(self, hal: IMUHal) -> None:
        self._check_hal_version(hal)
        self.imu = hal

    def register_camera(self, hal: CameraHal) -> None:
        self._check_hal_version(hal)
        self.camera = hal

    def register_microphone(self, hal: MicrophoneHal) -> None:
        self._check_hal_version(hal)
        self.microphone = hal

    def register_classifier(self, hal: ClassifierHal) -> None:
        self._check_hal_version(hal)
        self.classifier = hal

    def register_transport(self, hal: TransportHal, priority: int = 0) -> None:
        self._check_hal_version(hal)
        self.transports.append((priority, hal))
        self.transports.sort(key=lambda x: x[0])

    def register_display(self, hal: DisplayHal) -> None:
        self._check_hal_version(hal)
        self.display = hal

    def register_audio_output(self, hal: AudioOutputHal) -> None:
        self._check_hal_version(hal)
        self.audio_output = hal

    def validate_required(self) -> None:
        if self.imu is None or self.camera is None or not self.transports:
            raise ConfigurationError("Missing required HALs", "HAL_REQUIRED_MISSING", "Register IMU, camera, and >=1 transport")

    def discover(self) -> dict:
        return {"imu": [], "camera": [], "transport": []}


class EmbodimentSDK:
    """Runtime orchestrator for trigger, capture, classify, and transport."""

    def __init__(
        self,
        registry: HALRegistry,
        config_path: str = "config.yaml",
        trigger_config: Optional[TriggerConfig] = None,
        response_listener: Optional[AgentResponseListener] = None,
    ) -> None:
        self.registry = registry
        self.config_path = config_path
        self._run = False
        self._thread = None  # type: Optional[threading.Thread]
        self._trigger_cb = []  # type: List[Callable[[TriggerEvent], None]]
        self._response_cb = []  # type: List[Callable[[AgentResponse], None]]
        self.detector = TriggerDetector(trigger_config if trigger_config is not None else TriggerConfig())
        # Bidirectional response listener -- if not provided, auto-build from registered HALs
        self.response_listener = response_listener  # type: Optional[AgentResponseListener]

    def start(self) -> None:
        """Initialize HALs, register AgentResponseListener, and start trigger loop thread."""
        self.registry.validate_required()
        self.registry.imu.initialize(25)
        self.registry.camera.initialize((320, 240))
        if self.registry.microphone:
            self.registry.microphone.initialize()
            self.registry.microphone.start_recording()
        if self.registry.display:
            self.registry.display.initialize((80, 24))
        for _, t in self.registry.transports:
            t.initialize({})
            t.connect()
        # Auto-build response listener from registered HALs if not provided externally
        if self.response_listener is None:
            router = DeviceResponseRouter(
                display_hal=self.registry.display,
                audio_output_hal=self.registry.audio_output,
            )
            self.response_listener = AgentResponseListener(router=router)
        self.response_listener.register()
        self._run = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop runtime, deregister AgentResponseListener, and teardown HALs best-effort."""
        self._run = False
        if self._thread:
            self._thread.join(timeout=1.0)
        # Deregister response listener before HAL teardown
        if self.response_listener is not None:
            self.response_listener.deregister()
        if self.registry.microphone:
            self.registry.microphone.stop_recording()
            self.registry.microphone.shutdown()
        self.registry.camera.shutdown()
        self.registry.imu.shutdown()
        for _, t in self.registry.transports:
            t.shutdown()
        if self.registry.display:
            self.registry.display.shutdown()

    def on_trigger(self, callback: Callable[[TriggerEvent], None]) -> None:
        """Register callback invoked after accepted trigger."""
        self._trigger_cb.append(callback)

    def on_response(self, callback: Callable[[AgentResponse], None]) -> None:
        """Register callback invoked for response rendering."""
        self._response_cb.append(callback)

    def _run_loop(self) -> None:
        while self._run:
            sample = self.registry.imu.read_sample()
            if sample is None:
                time.sleep(0.01)
                continue
            evt = self.detector.update(sample)
            if evt:
                self._handle_trigger(evt)
            time.sleep(0.01)

    def _handle_trigger(self, event: TriggerEvent) -> None:
        frame = self.registry.camera.capture_frame()
        audio = self.registry.microphone.get_buffer(120).data if self.registry.microphone else b""
        if self.registry.classifier:
            result = self.registry.classifier.classify(frame.data, frame.width, frame.height, frame.format)
            if result.label != "interesting":
                return
            conf = int(max(0.0, min(1.0, result.confidence)) * 32767)
        else:
            conf = int(0.5 * 32767)
        payload = ContextPayload(event_id=event.event_id, device_id="wearable-1", timestamp_epoch=event.timestamp_epoch, flags=0b00000111, image_data=frame.data, audio_data=audio, imu_pitch=int(event.head_pitch * 100), imu_yaw=int(event.head_yaw * 100), imu_roll=int(event.head_roll * 100), imu_trigger_confidence=int(event.trigger_confidence * 65535), scene_gate_confidence=conf)
        packet = PacketSerializer.serialize(payload)
        send_result = None  # type: Optional[SendResult]
        for _, tx in self.registry.transports:
            send_result = tx.send(packet)
            if send_result.success:
                break
        for cb in self._trigger_cb:
            cb(event)
        response = AgentResponse(response_id="resp-%s" % event.event_id, event_id=event.event_id, trigger_timestamp_ms=event.timestamp_ms, title="Captured", body="Context sent (%d bytes)" % (send_result.bytes_sent if send_result else 0))
        if self.registry.display:
            self.registry.display.show(DisplayCard("card", response.title, response.body, 12, 3000))
        for cb in self._response_cb:
            cb(response)

# Backward-compatibility alias
WearableSDK = EmbodimentSDK
