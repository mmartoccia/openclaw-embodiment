# OpenGlass (ESP32-S3 DIY AI Glasses) Hardware Validation Checklist

## Prerequisites
- [ ] OpenGlass hardware assembled (ESP32-S3 + OV2640 camera module)
- [ ] OpenGlass firmware flashed from github.com/BasedHardware/OpenGlass
- [ ] `pip install bleak`
- [ ] BLE adapter available on host machine (built-in or USB dongle)

## Environment Setup
- [ ] ESP32-S3 powered on and advertising as "OpenGlass" via BLE
- [ ] Verify BLE advertisement: `python -c "import asyncio; import bleak; asyncio.run(bleak.BleakScanner.discover())" | grep OpenGlass`
- [ ] Host machine BLE not blocked by system preferences
- [ ] OpenGlass within 5m of host machine

## HAL Tests
- [ ] **CameraHal**: BLE camera characteristic subscribable (UUID 00005678-...)
  - Expected: GATT notification received within 2s
- [ ] **CameraHal**: `capture_frame()` returns reassembled JPEG (non-stub)
  - Test: `frame = hal.capture_frame(); assert frame.data[:2] == b'\xff\xd8'`
- [ ] **CameraHal**: JPEG chunk reassembly correct (no partial frames)
- [ ] **MicrophoneHal**: BLE audio characteristic subscribable (UUID 00005679-...)
- [ ] **MicrophoneHal**: `get_buffer(100)` returns PCM_INT16 at 8000 Hz
  - Test: `chunk = hal.get_buffer(100); assert chunk.sample_rate == 8000`
- [ ] **MicrophoneHal**: `transcribe()` returns non-empty string from speech
- [ ] **StatusIndicatorHal**: `set_color(255, 0, 0)` lights red LED
- [ ] **StatusIndicatorHal**: `blink(500)` starts 500ms blink cycle
- [ ] **StatusIndicatorHal**: `pulse("processing")` starts processing animation
- [ ] **StatusIndicatorHal**: `off()` turns LED off
- [ ] **PowerHal**: `get_battery_percent()` returns value 0-100
  - Expected: BLE Battery Service characteristic (0x2A19) readable
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns 80

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <15s (BLE slower than WiFi)
- [ ] Agent receives CameraFrame with valid JPEG data
- [ ] Audio capture → transcription round-trip < 5s
- [ ] LED status updates reflect pipeline state

## BLE Quality Tests
- [ ] Camera JPEG successfully reassembled from MTU-split chunks
- [ ] Audio buffer not overflowed during 10s continuous capture
- [ ] BLE connection stable for 5+ minutes

## Performance Benchmarks
- [ ] Camera frame interval: ~1s (1fps)
- [ ] Audio chunk latency: <200ms via BLE
- [ ] LED command latency: <100ms GATT write
- [ ] BLE transport latency: ~80ms (expected)

## Error Scenarios
- [ ] BLE disconnection: HAL logs warning, reconnection attempted
- [ ] MTU-split JPEG with corruption: chunk buffer reset, next frame captured cleanly
- [ ] Battery at 0%: PowerHal returns 0, warning logged

## Profile Validator
- [ ] `openclaw-embodiment validate openglass` returns PASS
- [ ] All 7 checks pass in BLE simulator mode (no hardware)
