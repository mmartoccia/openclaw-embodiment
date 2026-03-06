# Meta Ray-Ban Smart Glasses Hardware Validation Checklist

## Prerequisites
- [ ] Meta Ray-Ban glasses paired to iPhone via Meta View app
- [ ] MWDAT SDK companion app running on iOS/macOS (developer mode enabled)
- [ ] pip install flask websockets
- [ ] OpenClaw gateway running (openclaw gateway status)
- [ ] USB-C developer cable connected or WiFi on same network

## Environment Setup
- [ ] Set `mwdat.mock_mode: false` in meta_rayban.yaml
- [ ] Verify HTTP port 8421 is open (not firewalled)
- [ ] Verify WebSocket port 8422 is open
- [ ] Start RayBanServer: `from openclaw_embodiment.hal.rayban_server import RayBanServer; s = RayBanServer(mock_mode=False); s.start()`

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns non-None within 5s
  - Expected: JPEG bytes, ~50KB for 1080p
  - Test: `frame = hal.capture_frame(); assert len(frame.data) > 1000`
- [ ] **CameraHal**: Frame arrives at 1fps (±0.2fps tolerance)
- [ ] **MicrophoneHal**: `capture_audio()` returns AudioChunk with data
  - Expected: PCM_INT16, 16000 Hz, non-zero bytes
  - Test: `chunk = hal.get_buffer(100); assert len(chunk.data) > 0`
- [ ] **AudioOutputHal**: `speak("hello")` sends 24kHz PCM to glasses
  - Expected: No exception, audible output from glasses speaker
- [ ] **StatusIndicatorHal**: `set_color(0, 255, 0)` sets green LED
  - Expected: LED visible on glasses frame
- [ ] **StatusIndicatorHal**: `pulse("heartbeat")` starts heartbeat animation
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns 15
- [ ] **TransportHal**: `get_measured_latency_ms()` returns ≤50ms on local WiFi

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <10s
- [ ] Agent receives CameraFrame with populated JPEG data
- [ ] Audio round-trip: mic capture → transcribe → speak response < 3s
- [ ] LED pulse on trigger, off after response

## Performance Benchmarks
- [ ] Camera frame latency: HTTP POST received within 500ms of capture
- [ ] Audio latency: WebSocket chunk received within 200ms
- [ ] End-to-end (trigger to audio response): <5s

## Error Scenarios
- [ ] MWDAT app killed: server gracefully buffers empty, no crash
- [ ] WebSocket disconnect: audio output HAL logs warning, no crash
- [ ] Frame timeout: capture_frame() returns stub JPEG, no exception

## Profile Validator
- [ ] `openclaw-embodiment validate meta-rayban` returns PASS
- [ ] All 7 checks pass in simulator mode (no hardware)
