# OpenClaw Embodiment SDK -- Roadmap

---

## Current Status (Gate 3 Complete)

**Package:** `openclaw-embodiment` (Python, Apache 2.0)
**Repo:** [github.com/mmartoccia/openclaw-embodiment](https://github.com/mmartoccia/openclaw-embodiment)

| Metric | Value |
|--------|-------|
| HAL ABCs | 8 |
| Device profiles | 6 |
| Test coverage | 22 tests passing |
| Hardware validated | Raspberry Pi CM5 (Reachy Mini compute module) |
| Gate | 3 of 4 complete |

**8 HAL ABCs:** IMUHal, CameraHal, MicrophoneHal, AudioOutputHal, DisplayHal, TransportHal, ActuatorHal, PowerHal

**6 Device profiles:**
1. `reachy-mini` -- Reachy Mini Lite (Pollen Robotics / HuggingFace), HTTP transport, tested on CM5
2. `pi5-picam` -- Raspberry Pi 5 + PiCamera Module 3, BLE transport
3. `pi-zero2w` -- Raspberry Pi Zero 2W + PiCamera, BLE, performance-constrained
4. `luxonis-oakd` -- Luxonis OAK-D AI camera, USB + HTTP, visual motion proxy
5. `frame-glasses` -- Brilliant Labs Frame AR glasses, BLE, 640x400 OLED display
6. `even-g2` -- Even Realities G2 smart glasses, BLE dual-arm, LC3 audio, BMP display

---

## v1.1 -- Next

**Theme: Multi-modal trigger detection + latency awareness**

### AudioTriggerDetector
- Sound energy threshold → Direction of Arrival (`get_doa()`) → orient actuator toward source → visual capture
- Runs in parallel with visual `TriggerDetector`
- First implementation: Reachy Mini (head motor orient + camera capture)

### TriggerArbiter
- Fuses visual and audio trigger signals with configurable priority policy
- Policies: `FIRST_WINS`, `AUDIO_PRIORITY`, `VISUAL_PRIORITY`, `HIGHEST_CONFIDENCE`
- Required for devices with both camera and microphone (Reachy, Pi 5, Frame, Even G2)

### TransportHal latency awareness
- Add `get_expected_latency_ms()` to `TransportHal` ABC
- Pipeline adjusts actuation timing based on transport lag
- BLE and HTTP transports return measured rolling average

### StatusIndicatorHal
- New HAL ABC for LED and visual status feedback
- Methods: `set_color(r, g, b)`, `blink(interval_ms)`, `pulse(pattern)`, `off()`
- Implementations: Reachy Mini front LED, Even G2 indicator, Pi GPIO LED strip

### CLI improvements
- `openclaw-embodiment demo` -- hardware auto-detection on startup; falls back to simulator if no device found
- Profile hints shown when `doctor` detects partial hardware match

### New device profiles
- **Meta Ray-Ban** -- pending Meta Ray-Ban SDK public release; prioritized as soon as SDK ships
- **Apple Vision Pro** -- via VisionProTeleop bridge; macOS-native profile

---

## v2.0 -- Future

**Theme: Unified orchestration + platform maturity**

### HalOrchestrator
- Explicit `trigger → capture → transport → actuate` loop as a first-class Python object
- Replaces manual pipeline wiring in `EmbodimentPipeline`
- Per-stage hooks for logging, metrics, and custom middleware
- Async-native; handles concurrent trigger streams without blocking

### SystemHealthHal
- Unified device health: thermals, connectivity, storage, sensor availability
- Methods: `get_health_report()`, `is_operational()`, `on_degraded(cb)`
- Required for production deployments where hardware reliability matters

### Transport abstraction unification
- BLE and HTTP transports unified under a single `TransportHal` with identical interface
- Latency metadata on every message: `sent_at`, `received_at`, `transport_type`
- Automatic fallback: BLE → HTTP when BLE drops; HTTP → BLE when network is unavailable

### Profile auto-discovery
- `load_profile()` with no argument scans connected hardware and returns the best match
- USB device IDs, BLE advertisement names, and network service discovery
- Conflict resolution when multiple devices match

---

## Hardware Availability Notes

| Device | Availability | Lead Time | Notes |
|--------|-------------|-----------|-------|
| Reachy Mini Lite | Available (consumer) | ~90 days | Tested on CM5; partner outreach active via HF Space |
| Brilliant Labs Frame | Available | Ships ~1 week | $349; BLE profile written; hardware validation needed |
| Even Realities G2 | Available | Ships ~1 week | $269; BLE dual-arm profile written; hardware validation needed |
| Luxonis OAK-D | Available | Ships ~1 week | USB/HTTP profile implemented; OAK-D is stationary mount |
| Raspberry Pi 5 | Available | Ships ~1 week | Pi5 + PiCamera Module 3; BLE profile implemented |
| Raspberry Pi Zero 2W | Available | Ships ~1 week | Performance-constrained; BLE profile implemented |

**Validation priority:** Frame and Even G2 profiles are the highest-priority hardware gap. Both BLE HALs are written against published protocol documentation; live hardware validation is the remaining step.

---

## Platform Watchlist

Platforms tracked for future profile development. No implementation until SDK or protocol is publicly available.

| Platform | Status | Priority | Notes |
|----------|--------|----------|-------|
| Meta Ray-Ban (w/ Meta AI) | SDK pending public release | Priority 1 | Largest installed base in consumer AI glasses |
| Apple Vision Pro | VisionProTeleop bridge available | Priority 2 | VisionProTeleop provides robot control API; could adapt to HAL |
| Android XR (Samsung/Google) | In development | Priority 3 | Announced 2025; hardware not yet shipping broadly |

---

## Contributing

Device profile PRs are the fastest way to expand the SDK's reach. If you have Frame, Even G2, or Ray-Ban hardware -- we want to hear from you.

Open an issue: [github.com/mmartoccia/openclaw-embodiment/issues](https://github.com/mmartoccia/openclaw-embodiment/issues)

---

*Last updated: 2026-03-02*
