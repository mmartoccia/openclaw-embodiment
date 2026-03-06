# Watchlist Spec: Android XR (Samsung Galaxy AI Glasses + Google)

**Status:** Spec-only design document. No SDK available as of Q1 2026.
**Target implementation date:** When Android XR SDK / Galaxy AI Glasses SDK drops (est. 2026 H1).
**Profiles to create:** `android-xr-glasses` (Samsung Galaxy AI Glasses), `android-xr-headset` (HDMI/USB XR headset)

---

## Hardware Overview

### Samsung Galaxy AI Glasses (Code: "Haean")
- **Chipset:** Qualcomm AR1 Gen2 (dedicated AR SoC)
- **AI:** Gemini Nano on-device (1B param model, INT4 quantized)
- **Camera:** Forward-facing 12MP + depth sensor
- **Audio:** Dual mic array (beamforming), bone-conduction speakers
- **Display:** Micro-LED waveguide (right lens), ~45 degree FOV
- **Connectivity:** BLE 5.3, WiFi 6E, USB-C for charging/dev
- **Battery:** ~8h continuous AI use
- **Weight:** ~40g (similar to standard sunglasses)
- **OS:** Android XR 1.0 (AOSP fork with Gemini integration)

### Android XR Platform (Google)
- **SDK:** Android XR SDK (expected public release 2026 H1)
- **Developer program:** Open enrollment announced at Google I/O 2025
- **Primary OEM:** Samsung (Haean glasses + Project Moohan headset)
- **AI runtime:** Gemini Nano via MediaPipe (on-device), Gemini 2.0 Flash (cloud fallback)

---

## HAL Design (implement immediately when SDK drops)

### `AndroidXRCameraHal` extends `CameraHal`

```python
class AndroidXRCameraHal(CameraHal):
    """Camera HAL for Android XR glasses via Android CameraX API.

    Android XR provides CameraX-compatible access to glasses cameras.
    Expected: YUV_420_888 or JPEG via ImageReader, 30fps max.
    """
    
    # SDK API targets (based on Android CameraX patterns):
    # - camera = CameraX.getInstance().bindToLifecycle(...)
    # - imageCapture = ImageCapture.Builder().setTargetResolution(...)
    # - imageCapture.takePicture(executor, callback)
    
    def initialize(self, resolution=(1920, 1080)):
        # await CameraX.init()
        # self._camera_selector = CameraSelector.DEFAULT_BACK_CAMERA
        pass
    
    def capture_frame(self) -> CameraFrame:
        # imageCapture.takePicture(executor, OnImageCapturedCallback)
        # Convert ImageProxy -> JPEG bytes
        pass
```

### `AndroidXRMicrophoneHal` extends `MicrophoneHal`

```python
class AndroidXRMicrophoneHal(MicrophoneHal):
    """Microphone HAL via Android AudioRecord API.

    Android XR exposes the dual-mic array via standard AudioRecord.
    Expected: 16kHz PCM_INT16, channels=1 (post-beamforming), or
              48kHz stereo (raw dual-mic).
    """
    
    # SDK API targets:
    # - AudioRecord(MediaRecorder.AudioSource.MIC, 16000, CHANNEL_IN_MONO, 
    #               ENCODING_PCM_16BIT, buffer_size)
    # - Beamforming: AudioEffect or VoiceCapture API
    
    def initialize(self, sample_rate=16000, channels=1):
        # audio_record = AudioRecord(SOURCE_MIC, sample_rate, ...)
        pass
```

### `AndroidXRDisplayHal` extends `DisplayHal`

```python
class AndroidXRDisplayHal(DisplayHal):
    """Display HAL for Android XR micro-LED waveguide.

    Android XR provides SurfaceView/GLSurfaceView for overlay rendering.
    Spatial positioning via ARCore for Android XR.
    Expected: OpenGL ES 3.2 or Vulkan 1.1.
    """
    
    # SDK API targets:
    # - XrDisplayManager.getXrDisplay()
    # - SpatialPanel API (Android XR 1.0+)
    # - GLSurfaceView with ARCore anchor positioning
    
    def show(self, card: DisplayCard):
        # Create SpatialPanel with card content
        # Anchor to world coordinates or follow gaze
        pass
```

### `AndroidXRIMUHal` extends `IMUHal`

```python
class AndroidXRIMUHal(IMUHal):
    """Head pose IMU via Android XR SensorManager / ARCore.

    Android XR exposes 6DOF head tracking via standard SensorManager
    or higher-level ARCore XR session.
    Expected: 100-500Hz head pose updates.
    """
    
    # SDK API targets:
    # - SensorManager.getDefaultSensor(TYPE_ROTATION_VECTOR) for glasses orientation
    # - ARCore XrSession.getTrackingState() for 6DOF head pose
    
    def read_sample(self) -> IMUSample:
        # sensor_event.values -> quaternion -> Euler -> IMUSample
        pass
```

### `AndroidXRTransportHal` extends `TransportHal`

```python
class AndroidXRTransportHal(TransportHal):
    """Transport HAL for Android XR via BLE or local USB bridge.

    Two modes:
    1. BLE: Android XR glasses act as BLE central, host as peripheral
       - Expected latency: ~50ms
    2. USB bridge: ADB-based local transport during development
       - Expected latency: ~5ms
    3. WiFi WebSocket: For production with gateway
       - Expected latency: ~15ms
    """
    
    def get_expected_latency_ms(self) -> int:
        return 50  # BLE default; override for WiFi/USB
```

---

## Expected BLE Profile

Based on Android Bluetooth LE advertising patterns and AR1 Gen2 specs:

```yaml
profile: android-xr-glasses
transport:
  type: ble
  advertised_name: "Galaxy AI Glasses"  # TBD by Samsung
  service_uuid: "TBD-android-xr-service-uuid"  # Will be assigned at SDK launch
  camera_characteristic: "TBD"
  audio_characteristic: "TBD"
  control_characteristic: "TBD"
capabilities: [camera, microphone, display, imu, status_indicator]
```

**Note:** Samsung typically uses 16-bit UUIDs for first-party BLE services.
When SDK drops, extract UUIDs from `com.samsung.android.xr.ble.service` package.

---

## Gemini Nano On-Device Integration

Android XR ships Gemini Nano via `com.google.android.apps.tipsandtricks` (MediaPipe).
Expected API:

```python
# Python bridge via ADB or Kivy/Chaquopy Android Python bridge:
class GeminiNanoHal:
    """On-device Gemini Nano inference via Android AI Core."""
    
    def infer(self, prompt: str, context_image: Optional[bytes] = None) -> str:
        # POST to local Android AI Core HTTP server (localhost:XXXX on device)
        # Or via ADB: adb shell am startservice com.google.android.aicore/.InferenceService
        pass
```

---

## What to Implement Immediately When SDK Drops

1. **Day 0 (SDK release day):**
   - `AndroidXRCameraHal` -- bind to CameraX, `capture_frame()` returns JPEG
   - `AndroidXRTransportHal` -- USB ADB bridge for dev, WiFi for prod
   - Basic profile YAML + `build_android_xr_hals()`
   - `openclaw-embodiment validate android-xr-glasses` passes

2. **Day 1:**
   - `AndroidXRMicrophoneHal` -- AudioRecord bridge
   - `AndroidXRDisplayHal` -- SpatialPanel overlay
   - `AndroidXRIMUHal` -- SensorManager rotation vector

3. **Day 3:**
   - Gemini Nano on-device inference bridge
   - Full pipeline integration test
   - Auto-discovery BLE signature registration

---

## Simulator Strategy (today, before SDK)

Mock the Android XR bridge via HTTP localhost server that accepts frame + pose messages:

```python
# android_xr_mock_server.py -- runs on dev machine
# Accepts: POST /frame (JPEG), POST /pose (JSON), POST /audio (PCM)
# Same interface as real Android XR bridge
# Enables all integration tests to pass today
```

---

## References

- Android XR developer preview: developer.android.com/xr
- Samsung Galaxy AI Glasses: samsung.com/galaxy-ai-glasses (announced CES 2025)
- Qualcomm AR1 Gen2: qualcomm.com/products/mobile/snapdragon/ar-vr/snapdragon-ar1-gen-2-platform
- MediaPipe Gemini Nano: ai.google.dev/mediapipe
- ARCore XR: developers.google.com/ar/develop/android-xr
