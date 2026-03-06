# Reachy Mini Wireless Hardware Validation Checklist

## Prerequisites
- [ ] Reachy Mini Wireless powered on and on WiFi
- [ ] reachy-mini-wireless-daemon running on device
- [ ] Bonjour/mDNS: `reachy-mini-wireless.local` resolvable
- [ ] `pip install reachy_mini_sdk`

## Environment Setup
- [ ] Wireless Reachy Mini on same WiFi network as host
- [ ] Verify: `ping reachy-mini-wireless.local`
- [ ] Verify: `curl http://reachy-mini-wireless.local:50051/api/health`
- [ ] Battery level ≥ 20% before testing

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 3s (WiFi adds ~50ms)
- [ ] **ActuatorHal**: `execute(ActuatorCommand("1", "wave", {}, 0))` triggers wave
  - Expected: ActuatorResult(success=True), wireless command received
- [ ] **StatusIndicatorHal**: `pulse("heartbeat")` blinks front LED
- [ ] **SystemHealthHal**: `get_battery_percent()` returns value 0-100
- [ ] **TransportHal**: WiFi transport to daemon
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns ≤50 (WiFi vs USB)

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <15s (WiFi)
- [ ] Agent receives CameraFrame with valid data
- [ ] Actuator command delivered over WiFi within 200ms
- [ ] Battery monitoring active during pipeline

## Performance vs Wired
- [ ] Wireless latency: ≤2x wired latency
- [ ] Connection stable for 30+ minutes continuous operation

## Profile Validator
- [ ] `openclaw-embodiment validate reachy-mini-wireless` returns PASS
