# Raspberry Pi 5 + Pi Camera Module 3 Hardware Validation Checklist

## Prerequisites
- [ ] Raspberry Pi 5 with Pi Camera Module 3 installed
- [ ] Pi OS (Bookworm) installed with camera enabled
- [ ] SSH access configured: `ssh pi@raspberrypi.local`
- [ ] OpenClaw Embodiment SDK installed on Pi: `pip install openclaw-embodiment`
- [ ] `pip install picamera2` (already on Pi OS Bookworm)

## Environment Setup
- [ ] Camera ribbon cable properly seated (purple side toward camera, blue toward Pi)
- [ ] Run `libcamera-hello --list-cameras` to confirm camera detected
- [ ] Pi on same WiFi as OpenClaw gateway
- [ ] GPU memory set to 128MB in raspi-config

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 2s
  - Expected: JPEG, native resolution (up to 4608x2592)
  - Test: `frame = hal.capture_frame(); assert frame.format == 'JPEG'; assert len(frame.data) > 5000`
- [ ] **CameraHal**: Frame capture at configured FPS (≥5fps at 1080p)
- [ ] **SystemHealthHal**: `get_health_report()` returns cpu_percent and temperature_c
  - Expected: temperature_c < 80°C under load
- [ ] **TransportHal**: HTTP transport to OpenClaw gateway reachable
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns ≤50 (local WiFi)

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <10s
- [ ] Agent receives CameraFrame with valid JPEG data
- [ ] Pi-side CPU usage during pipeline: <80%
- [ ] No memory leak over 10 pipeline cycles

## Performance Benchmarks
- [ ] Camera capture latency: <500ms at 1080p JPEG
- [ ] HTTP transport latency: <50ms on LAN
- [ ] System temperature stable <70°C during continuous capture

## Error Scenarios
- [ ] Camera disconnected: HAL logs error, pipeline pauses gracefully
- [ ] High CPU temp (>80°C): SystemHealthHal reports warning

## Profile Validator
- [ ] `openclaw-embodiment validate pi5-picam` returns PASS
