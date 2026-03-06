# Apple Vision Pro Hardware Validation Checklist

## Prerequisites
- [ ] Apple Vision Pro running visionOS 2.0+
- [ ] VisionProTeleop companion app installed and running
- [ ] MacBook/Mac Mini on same WiFi network as Vision Pro
- [ ] `pip install websockets`
- [ ] WebSocket port 8430 open on host machine

## Environment Setup
- [ ] VisionProTeleop app started on Vision Pro (shows connection status)
- [ ] Set `transport.host` to Mac IP address in apple_vision_pro.yaml
- [ ] ReplayKit screen capture enabled in Vision Pro settings
- [ ] Sufficient physical space for head movement testing (2m radius)

## HAL Tests
- [ ] **IMUHal**: `read_sample()` returns IMUSample with head position data
  - Expected: head_position.y ≈ 1.6m (standing height), non-zero quaternion
  - Test: `sample = hal.read_sample(); assert sample is not None`
- [ ] **IMUHal**: `get_orientation()` returns (pitch, yaw, roll) tuple
- [ ] **IMUHal**: `get_acceleration()` returns (x, y, z) head position
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 2s
  - Expected: JPEG, 3680x3504 per eye or passthrough resolution
  - Test: `frame = hal.capture_frame(); assert len(frame.data) > 1000`
- [ ] **CameraHal**: ReplayKit stream maintains 30fps
- [ ] **DisplayHal**: `show_card("Hello Vision Pro")` renders overlay
  - Expected: Text card visible in passthrough view
- [ ] **DisplayHal**: `show_card(text, image=jpeg_bytes)` renders image overlay
- [ ] **DisplayHal**: `clear()` removes overlay without crash
- [ ] **DisplayHal**: `render_agent_response(response)` formats response card
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns 8

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <5s (low latency WiFi)
- [ ] Agent receives CameraFrame with populated spatial frame data
- [ ] Agent response displayed as overlay within 2s of trigger
- [ ] Head pose updates trigger context re-evaluation

## Overlay Quality Tests
- [ ] Text overlay readable at 1m distance
- [ ] Overlay positioned correctly in spatial environment (not floating far)
- [ ] Overlay dismisses after duration_ms elapsed

## Performance Benchmarks
- [ ] Head pose sample latency: <20ms via WebSocket
- [ ] Camera frame latency: <100ms (ReplayKit)
- [ ] Display overlay latency: <100ms (WebSocket command)
- [ ] End-to-end pipeline cycle: <3s

## Profile Validator
- [ ] `openclaw-embodiment validate apple-vision-pro` returns PASS
- [ ] All 7 checks pass with mock WebSocket (no hardware)
