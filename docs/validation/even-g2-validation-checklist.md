# Even Realities G2 Smart Glasses Hardware Validation Checklist

## Prerequisites
- [ ] Even G2 glasses powered on and charged
- [ ] Even Realities companion app installed and glasses paired
- [ ] `pip install bleak`
- [ ] BLE adapter available on host machine

## Environment Setup
- [ ] G2 advertising as "Even G2", "EvenG2", or "G2" via BLE
- [ ] Glasses not in sleep mode
- [ ] Micro-LED display working (green indicator on temple)

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 3s
  - Expected: JPEG frame, non-null data
- [ ] **MicrophoneHal**: `get_buffer(100)` returns AudioChunk with data
- [ ] **MicrophoneHal**: `transcribe()` returns text from speech
- [ ] **DisplayHal**: `show(card)` renders text on micro-LED waveguide display
  - Expected: Text visible in right lens
- [ ] **DisplayHal**: `clear()` blanks display
- [ ] **TransportHal**: BLE transport connects to host
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns ≤80 (BLE)

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <15s
- [ ] Agent receives CameraFrame with valid frame data
- [ ] Agent response displayed on waveguide within 3s
- [ ] Tap gesture triggers pipeline correctly

## Performance Benchmarks
- [ ] Camera frame capture: <2s
- [ ] Micro-LED display update: <200ms
- [ ] BLE round-trip: ~80ms
- [ ] Battery life with SDK active: >4h

## Profile Validator
- [ ] `openclaw-embodiment validate even-g2` returns PASS
