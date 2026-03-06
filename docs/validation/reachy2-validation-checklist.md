# Reachy 2 Full-Body Robot Hardware Validation Checklist

## Prerequisites
- [ ] Reachy 2 powered on and booted (takes 60-90s)
- [ ] Reachy 2 connected via Ethernet or WiFi (Ethernet preferred)
- [ ] SDK: `pip install reachy2-sdk`
- [ ] gRPC port 50051 accessible: `nc -z reachy.local 50051`

## Environment Setup
- [ ] Robot in starting position (arms resting, head forward)
- [ ] Clear 1.5m radius around robot for arm movement tests
- [ ] Check fan status: fans should spin on power-on
- [ ] Verify cameras: 2x head cameras (left/right stereo) + optional wrist cameras

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 2s
  - Expected: JPEG from head stereo camera, 1080p
  - Test: `frame = hal.capture_frame(); assert len(frame.data) > 10000`
- [ ] **IMUHal**: `read_sample()` returns IMUSample with torso orientation
  - Expected: accel_z ≈ 9.8 m/s² (upright), gyro near zero (stationary)
- [ ] **ActuatorHal**: `execute(ActuatorCommand("1", "wave_right_arm", {}, 0))` waves arm
  - Expected: ActuatorResult(success=True), arm moves to wave position
- [ ] **ActuatorHal**: `stop_all()` immediately stops all joints
- [ ] **ActuatorHal**: `get_joint_states()` returns dict of all joint telemetry
- [ ] **DisplayHal**: LED matrix on chest displays status card
- [ ] **StatusIndicatorHal**: `set_color(0, 255, 0)` sets chest LED green
- [ ] **TransportHal**: gRPC transport to Reachy 2 SDK
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns ≤10 (local gRPC)

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <10s
- [ ] Agent receives CameraFrame with stereo head camera data
- [ ] Natural language → arm gesture round-trip < 5s
- [ ] Joint states update at 100Hz during motion

## Safety Tests
- [ ] `stop_all()` halts all joints within 200ms
- [ ] Torque limit respected (no joint overload)
- [ ] Emergency stop (physical button) disables all joints immediately

## Performance Benchmarks
- [ ] Stereo camera capture: <200ms
- [ ] Joint command latency: <20ms via gRPC
- [ ] End-to-end pipeline: <5s

## Profile Validator
- [ ] `openclaw-embodiment validate reachy2` returns PASS
