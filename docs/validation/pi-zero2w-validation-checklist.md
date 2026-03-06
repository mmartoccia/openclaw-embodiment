# Raspberry Pi Zero 2W Hardware Validation Checklist

## Prerequisites
- [ ] Raspberry Pi Zero 2W with Camera Module 2 or Pi Camera Module 3
- [ ] Pi OS Lite (Bookworm) installed
- [ ] SSH access: `ssh pi@pizero.local`
- [ ] OpenClaw Embodiment SDK installed: `pip install openclaw-embodiment`
- [ ] Pi Zero on WiFi network

## Environment Setup
- [ ] Camera ribbon cable connected (Pi Zero uses smaller 22-pin FPC)
- [ ] `libcamera-hello --list-cameras` confirms camera detected
- [ ] Overclocking recommended: `arm_freq=1200` in /boot/config.txt
- [ ] GPU memory: 128MB minimum for camera operation

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 5s
  - Note: Pi Zero is slower -- 5s timeout (vs 2s on Pi 5)
  - Expected: JPEG at lower resolution (1280x720 recommended for Zero)
  - Test: `frame = hal.capture_frame(); assert len(frame.data) > 1000`
- [ ] **SystemHealthHal**: `get_health_report()` returns cpu_percent
  - Expected: cpu_percent < 90% during idle capture
- [ ] **TransportHal**: HTTP transport to OpenClaw gateway reachable
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns ≤100 (Pi Zero WiFi 2.4GHz)

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <20s (Pi Zero is slower)
- [ ] Agent receives CameraFrame with valid JPEG data
- [ ] Pi Zero CPU not throttled during pipeline (check `vcgencmd get_throttled`)
- [ ] Memory usage stable over 5 pipeline cycles (Pi Zero has 512MB)

## Performance Benchmarks
- [ ] Camera capture latency: <2s at 720p JPEG
- [ ] HTTP transport latency: <100ms on 2.4GHz WiFi
- [ ] Pipeline cycle time: <15s end-to-end

## Resource Constraints
- [ ] RAM usage peak: <400MB (of 512MB total)
- [ ] CPU temp: <70°C without heatsink, <60°C with heatsink

## Profile Validator
- [ ] `openclaw-embodiment validate pi-zero2w` returns PASS
