# Brilliant Labs Frame AR Glasses Hardware Validation Checklist

## Prerequisites
- [ ] Brilliant Labs Frame glasses charged and powered on
- [ ] `pip install frame-sdk`
- [ ] BLE adapter available on host machine
- [ ] Frame app companion app installed on iOS/macOS

## Environment Setup
- [ ] Frame glasses advertising "Frame" or "BrilliantFrame" via BLE
- [ ] frame-sdk authenticated and paired to device
- [ ] OLED display not in sleep mode (tap temple to wake)

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 3s
  - Expected: JPEG, 1080p, non-null data
  - Test: `frame = hal.capture_frame(); assert len(frame.data) > 1000`
- [ ] **MicrophoneHal**: `get_buffer(100)` returns AudioChunk with PCM data
- [ ] **MicrophoneHal**: `transcribe()` returns non-empty transcript from speech
- [ ] **DisplayHal**: `show(card)` renders text on 640x400 OLED
  - Expected: Text visible, no display glitch
- [ ] **DisplayHal**: `clear()` blanks display
- [ ] **TransportHal**: BLE transport connects to host
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns ≤80 (BLE)

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <15s
- [ ] Agent receives CameraFrame with valid JPEG
- [ ] Agent response displayed on OLED within 3s
- [ ] Microphone capture triggers correctly on motion/tap

## Performance Benchmarks
- [ ] Camera frame capture: <2s
- [ ] OLED display update: <200ms
- [ ] BLE round-trip: ~80ms
- [ ] End-to-end: <10s

## Profile Validator
- [ ] `openclaw-embodiment validate frame-glasses` returns PASS
