# Watchlist Spec: Meta Neural Band (EMG Wrist Input, 2026 Model)

**Status:** Spec-only design document. No public SDK available as of Q1 2026.
**Target implementation date:** When Meta Neural Band SDK / developer preview drops (est. 2026 H2).
**Profiles to create:** `meta-neural-band` (standalone), `meta-rayban-neural` (combined Ray-Ban + Neural Band)

---

## Hardware Overview

### Meta Neural Band (2026 Generation)
- **Input modality:** EMG (electromyography) for wrist gesture recognition
- **IMU:** 6DOF wrist IMU (accelerometer + gyroscope), 200Hz
- **BLE:** BLE 5.2 to paired device (iPhone, Meta Ray-Ban glasses, or Quest)
- **Battery:** ~8h continuous, wireless charging
- **Form factor:** Slim wristband (~12mm width), similar to fitness tracker
- **Gestures (known from CTRL-labs acquisition + Meta demos):**
  - Pinch (thumb + index): confirm/select
  - Double pinch: back/cancel
  - Wrist flick up/down/left/right: navigation
  - Grip: grab/drag
  - Scroll (index + thumb rub): scroll content
  - Custom gestures: trainable per-user calibration
- **EMG sensors:** 16-channel EMG array around wrist circumference
- **Companion devices:** Ray-Ban Smart Glasses, Meta Quest, iPhone

---

## New HAL: `GestureHal` ABC

This device requires a new HAL ABC not in the current 10. Adding `GestureHal`:

```python
@dataclass(frozen=True)
class GestureEvent:
    """Single gesture recognition event from EMG or IMU input.
    
    Attributes:
        gesture_id: Unique identifier string for the gesture type.
        confidence: Recognition confidence 0.0-1.0.
        timestamp_ms: Event timestamp in milliseconds.
        duration_ms: Gesture duration from onset to completion.
        hand: 'left' or 'right'.
        metadata: Optional extra data (e.g. scroll delta, pinch pressure).
    """
    gesture_id: str
    confidence: float
    timestamp_ms: int
    duration_ms: int
    hand: str
    metadata: dict = field(default_factory=dict)


class GestureHal(HALBase, ABC):
    """Gesture recognition abstraction for wrist/EMG input devices.
    
    New HAL ABC (11th) for EMG + IMU gesture recognition.
    Provides a device-agnostic interface for gesture streams.
    """
    
    @abstractmethod
    def initialize(self, gesture_vocab: Optional[list] = None) -> None:
        """Initialize gesture recognition with optional custom vocabulary."""
        ...
    
    @abstractmethod
    def get_gesture(self, timeout_ms: int = 1000) -> Optional[GestureEvent]:
        """Block until a gesture is recognized or timeout.
        
        Returns:
            GestureEvent or None on timeout.
        """
        ...
    
    @abstractmethod
    def stream_gestures(self) -> Iterator[GestureEvent]:
        """Yield gesture events as they occur (blocking iterator)."""
        ...
    
    @abstractmethod
    def calibrate(self, user_id: str) -> bool:
        """Run per-user calibration for personalized gesture recognition.
        
        Returns:
            True if calibration succeeded.
        """
        ...
    
    @abstractmethod
    def get_supported_gestures(self) -> list:
        """Return list of supported gesture_id strings."""
        ...
    
    @abstractmethod
    def shutdown(self) -> None:
        """Shutdown gesture recognition."""
        ...
```

---

## HAL Design

### `NeuralBandIMUHal` extends `IMUHal`

```python
class NeuralBandIMUHal(IMUHal):
    """6DOF wrist IMU HAL for Meta Neural Band.
    
    Reads wrist orientation (pitch/yaw/roll) and acceleration from
    the Neural Band's 200Hz IMU via BLE GATT.
    
    Useful for:
    - Wrist tilt (phone raise gesture)
    - Motion context (walking vs. stationary)
    - Complementing EMG gestures with motion context
    """
    
    # Expected BLE characteristic: TBD from SDK
    # Likely: standard BLE IMU service UUID or Meta custom GATT
    
    def initialize(self, sample_rate_hz=200):
        # Subscribe to BLE IMU GATT characteristic
        # Start background task: decode IMU packets -> IMUSample
        pass
    
    def read_sample(self) -> Optional[IMUSample]:
        # Latest IMU packet from ring buffer
        pass
```

### `NeuralBandGestureHal` extends `GestureHal`

```python
class NeuralBandGestureHal(GestureHal):
    """EMG gesture HAL for Meta Neural Band.
    
    Receives pre-classified gesture events from Neural Band firmware
    via BLE notification. The band does on-device inference (EMG signal
    -> gesture class) and reports events rather than raw EMG.
    
    In mock_mode, generates synthetic gesture events for testing.
    """
    
    # Expected BLE:
    # - Service UUID: TBD (Meta custom GATT)
    # - EMG characteristic: raw 16-channel EMG (high bandwidth, optional)
    # - Gesture characteristic: pre-classified events (low bandwidth, primary)
    
    SUPPORTED_GESTURES = [
        "pinch", "double_pinch", "flick_up", "flick_down",
        "flick_left", "flick_right", "grip", "scroll",
    ]
    
    def get_gesture(self, timeout_ms=1000) -> Optional[GestureEvent]:
        # Block on BLE gesture notification queue
        pass
    
    def stream_gestures(self) -> Iterator[GestureEvent]:
        # Yield gestures as BLE notifications arrive
        pass
    
    def calibrate(self, user_id: str) -> bool:
        # Trigger calibration mode on band (guided by companion app)
        # Returns True after user completes calibration sequence
        pass
```

### `NeuralBandTransportHal` extends `TransportHal`

```python
class NeuralBandTransportHal(TransportHal):
    """BLE transport HAL for Meta Neural Band.
    
    Expected latency: 40ms (BLE 5.2, short connection interval).
    Primary path: BLE to host, HTTP relay to OpenClaw gateway.
    """
    
    def get_expected_latency_ms(self) -> int:
        return 40  # BLE 5.2 short connection interval
```

---

## Profile Config

```yaml
profile: meta-neural-band
display_name: Meta Neural Band (EMG Wrist Input)
hardware:
  imu: {type: wrist_imu, rate_hz: 200, dof: 6}
  gesture: {type: emg_neural, channels: 16, gestures: [pinch, double_pinch, flick_up, flick_down, flick_left, flick_right, grip, scroll]}
transport:
  type: ble
  service_uuid: "TBD-meta-neural-band-uuid"
capabilities: [imu, gesture, transport]
emg:
  raw_stream: false  # set true for raw 16-channel EMG data (high bandwidth)
  calibration_required: true
```

---

## Combined Profile: meta-rayban-neural

When both Meta Ray-Ban glasses and Neural Band are connected together:

```yaml
profile: meta-rayban-neural
display_name: Meta Ray-Ban + Neural Band (Combined)
hardware:
  # From Ray-Ban:
  camera: {fps: 1, format: jpeg, resolution: "1080p"}
  microphone: {sample_rate: 16000, channels: 1}
  audio_output: {sample_rate: 24000, channels: 1}
  # From Neural Band:
  imu: {type: wrist_imu, rate_hz: 200}
  gesture: {type: emg_neural}
capabilities: [camera, microphone, audio_output, status_indicator, imu, gesture, transport]
```

**Combined device factory:**

```python
def build_meta_rayban_neural_hals(config: dict) -> dict:
    """Build combined HAL set for Ray-Ban glasses + Neural Band.
    
    Returns HALs from both devices. Gesture events from Neural Band
    can trigger camera captures on Ray-Ban (e.g. pinch = capture).
    Wrist IMU augments head camera context with body motion data.
    """
    rayban_hals = build_meta_rayban_hals(config)
    neural_hals = build_neural_band_hals(config)
    
    return {
        **rayban_hals,
        "imu": neural_hals["imu"],       # Override with wrist IMU
        "gesture": neural_hals["gesture"],  # New: gesture HAL
    }
```

**Interaction pattern:**

```python
# Pinch gesture -> capture frame -> speak response
async def gesture_pipeline(hals):
    gesture_hal = hals["gesture"]
    camera = hals["camera"]
    audio_out = hals["audio_output"]
    
    while True:
        event = gesture_hal.get_gesture(timeout_ms=5000)
        if event and event.gesture_id == "pinch" and event.confidence > 0.85:
            frame = camera.capture_frame()
            response = await agent.process(frame)
            audio_out.speak(response.content)
```

---

## Simulator Strategy (today, before SDK)

Add `SimulatedGestureHal` to `hal/simulator.py`:

```python
class SimulatedGestureHal(GestureHal):
    """Synthetic gesture generator for testing.
    
    Cycles through: pinch, flick_up, flick_left on a configurable cadence.
    """
    
    CYCLE = ["pinch", "flick_up", "flick_left", "double_pinch"]
    
    def get_gesture(self, timeout_ms=1000) -> Optional[GestureEvent]:
        import time
        time.sleep(min(timeout_ms / 1000.0, 0.1))
        gesture_id = self.CYCLE[self._idx % len(self.CYCLE)]
        self._idx += 1
        return GestureEvent(gesture_id, 0.92, _ms(), 150, "right", {})
```

---

## What to Implement Immediately When SDK Drops

1. **Day 0:**
   - `GestureHal` ABC committed to `hal/base.py` (design done today)
   - `NeuralBandGestureHal` -- BLE GATT gesture event subscriber
   - `NeuralBandIMUHal` -- 200Hz wrist IMU subscriber
   - Profile YAML + factory
   - `openclaw-embodiment validate meta-neural-band` passes

2. **Day 1:**
   - `SimulatedGestureHal` in `hal/simulator.py`
   - Combined `meta-rayban-neural` profile
   - Gesture-triggered pipeline example

3. **Day 3:**
   - Calibration workflow integration
   - Raw EMG stream support (optional)
   - Per-gesture confidence threshold configuration

---

## Auto-Discovery Signature

```python
DeviceSignature(
    profile_name="meta-neural-band",
    ble_names=["Neural Band", "Meta Band", "CTRL"],  # TBD
    suggested_config={"emg": {"calibration_required": True}},
    confidence=0.9,
)
```

---

## References

- Meta CTRL-labs acquisition (2019): Meta acquired CTRL-labs for EMG technology
- Meta Connect 2024 Neural Interface demo: neuromotor-based computing previewed
- EMG gesture papers: "Continuous-time gesture recognition" (Meta Reality Labs)
- Qualcomm AR1 Gen2 BLE 5.3: qualcomm.com/ar1-gen2-connectivity
- Meta Ray-Ban 2024 MWDAT SDK: developers.meta.com/ray-ban
