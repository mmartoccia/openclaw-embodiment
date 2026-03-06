# Unitree Go2 Quadruped Robot Hardware Validation Checklist

## Prerequisites
- [ ] Unitree Go2 powered on and on same WiFi network
- [ ] Go2 IP address confirmed: 192.168.123.161 (default) or custom
- [ ] `pip install unitree_sdk2py`
- [ ] unitree_sdk2py simulation mode tested first

## Environment Setup
- [ ] Set `simulation.enabled: false` in unitree_go2.yaml
- [ ] Set correct `transport.host` to Go2 IP address
- [ ] Go2 standing and in normal state (not in transport mode)
- [ ] Clear 2m x 2m floor space around robot

## HAL Tests
- [ ] **IMUHal**: `read_sample()` returns IMUSample with non-zero accel_z (~9.8 m/s²)
  - Test: `sample = hal.read_sample(); assert abs(sample.accel_z - 9.8) < 2.0`
- [ ] **IMUHal**: Sample rate matches config (500Hz)
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 2s
  - Expected: JPEG/H264, 1920x1080
  - Test: `frame = hal.capture_frame(); assert len(frame.data) > 10000`
- [ ] **CameraHal**: WebRTC stream maintains 30fps
- [ ] **ActuatorHal**: `stand_up()` command executes without error
  - Expected: ActuatorResult(success=True), robot stands
- [ ] **ActuatorHal**: `wave()` command executes hello gesture
- [ ] **ActuatorHal**: `stop_all()` immediately halts all motion
- [ ] **ActuatorHal**: `move_forward(speed=0.2)` moves robot forward
- [ ] **AudioOutputHal**: `speak_agent_response()` emits audio from onboard speaker
- [ ] **SystemHealthHal**: `get_health_report()` returns battery_percent > 20
- [ ] **TransportHal**: `send()` returns SendResult(success=True) to Go2 onboard PC
- [ ] **TransportHal**: `get_expected_latency_ms()` returns 20

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <10s
- [ ] Agent receives CameraFrame with populated frame data
- [ ] Locomotion command from agent context reaches SportClient
- [ ] IMU readings update at configured rate during locomotion

## Safety Tests
- [ ] `stop_all()` halts robot within 500ms from any motion state
- [ ] Transport failure triggers warning log, robot continues last command safely
- [ ] Battery below 15% triggers SystemHealthHal warning

## Performance Benchmarks
- [ ] IMU sample latency: <5ms at 500Hz
- [ ] Camera frame latency: <100ms (WebRTC)
- [ ] Actuator command latency: <50ms (sport_client)
- [ ] End-to-end pipeline cycle: <3s

## Profile Validator
- [ ] `openclaw-embodiment validate unitree-go2` returns PASS
- [ ] All 7 checks pass in simulation mode (no hardware)
