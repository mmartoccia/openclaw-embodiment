# Reachy Mini (Wired) Hardware Validation Checklist

## Prerequisites
- [ ] Reachy Mini connected via USB or local network
- [ ] reachy-mini-daemon running: `reachy-mini-daemon --port 50055`
- [ ] `pip install reachy_mini_sdk`
- [ ] OpenClaw Embodiment SDK installed

## Environment Setup
- [ ] Reachy Mini powered on (green LED on front panel)
- [ ] USB connection or WiFi pairing confirmed
- [ ] Verify connectivity: `curl http://localhost:50055/api/health`

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 3s
  - Expected: JPEG frame from head-mounted camera
  - Test: `frame = hal.capture_frame(); assert len(frame.data) > 1000`
- [ ] **ActuatorHal**: `execute(ActuatorCommand("1", "wave", {}, 0))` triggers wave gesture
  - Expected: ActuatorResult(success=True), head/arm movement
- [ ] **StatusIndicatorHal**: `set_color(0, 255, 0)` lights front LED green
- [ ] **TransportHal**: HTTP/gRPC transport to reachy-mini-daemon
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns ≤20

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <10s
- [ ] Agent receives CameraFrame with populated data
- [ ] Actuator command from agent context executes correctly
- [ ] LED status reflects pipeline state (processing → idle)

## Performance Benchmarks
- [ ] Camera capture: <1s
- [ ] Actuator command: <200ms
- [ ] End-to-end pipeline: <5s

## Profile Validator
- [ ] `openclaw-embodiment validate reachy-mini` returns PASS
