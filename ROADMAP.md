# OpenClaw Embodiment SDK -- Roadmap

---

## ⚡ OpenClaw Beta Impact (2026-03-03)

A major OpenClaw beta shipped today with three capabilities that directly upgrade this SDK's architecture. These are high-priority additions to v1.1 and v1.2.

### 4. On-Device Inference — Qwen 3.5 via MLX (2026-03-03)
**Impact:** HIGH — decouples SDK from cloud gateway dependency.

Qwen 3.5 2B (6-bit, MLX) runs on-device on Apple Silicon with visual understanding and toggleable reasoning. Beats models 4x its size. Enables `LocalMLXTransport` as a first-class HAL: no gateway, no network, works offline. Validated on iPhone 17 Pro; target for .152 MacBook Pro and Pi 5 aarch64.

**Target:** v1.2 — `LocalMLXTransport` + hybrid routing config.

### 1. Bidirectional Agent Loop — `onAgentEvent` + `onSessionTranscriptUpdate`
**Impact:** HIGH — closes the missing half of the device-to-agent architecture.

Current flow is one-way: device fires trigger → SDK posts context → nothing comes back. The new plugin runtime events let the agent push responses back to the device in real-time. This is what makes Reachy actually *react* to the agent — not just send context into the void.

**Target:** v1.1 — `AgentResponseListener` class wrapping `onAgentEvent`, wired into `EmbodimentSDK` response callbacks.

### 2. Native STT via `api.runtime.stt.transcribeAudioFile(...)`
**Impact:** HIGH — eliminates the missing transcription path in MicrophoneHal.

`MicrophoneHal` captures raw `AudioChunk` but has no transcription path. The beta provides a first-class STT bridge through OpenClaw's configured audio providers. `MicrophoneHal.transcribe()` can now delegate to `openclaw stt transcribe` instead of shipping a per-device speech stack.

**Target:** v1.1 — add `transcribe()` abstract method to `MicrophoneHal`, implement via OpenClaw STT in reference HALs.

### 3. Instant Agent Wake — `runtime.system.requestHeartbeatNow(...)`
**Impact:** MEDIUM-HIGH — removes heartbeat latency from event-driven flows.

Current trigger architecture polls at 25Hz but the agent only wakes at the next heartbeat cycle — up to 30s latency. `requestHeartbeatNow()` lets the device kick the agent immediately on trigger. Critical for Reachy real-time response.

**Target:** v1.2 — `TriggerDetector` emits heartbeat wake call on CAPTURE state transition.

---

## Current Status (v1.2.0 -- All Gates Complete)

**Package:** `openclaw-embodiment` v1.2.0 (Python, Apache 2.0)
**Repo:** [github.com/mmartoccia/openclaw-embodiment](https://github.com/mmartoccia/openclaw-embodiment)

| Metric | Value |
|--------|-------|
| HAL ABCs | 9 (+ StatusIndicatorHal) |
| Device profiles | 7 (+ Reachy 2 full humanoid) |
| Test coverage | 270 tests passing |
| Hardware validated | Raspberry Pi CM5 (Reachy Mini compute module) |
| Gate | 4 of 4 complete |

**9 HAL ABCs:** IMUHal, CameraHal, MicrophoneHal, AudioOutputHal, DisplayHal, TransportHal, ActuatorHal, PowerHal, **StatusIndicatorHal**

**7 Device profiles:**
1. `reachy-mini` -- Reachy Mini Lite (Pollen Robotics / HuggingFace), HTTP transport, tested on CM5
2. `reachy2` -- Reachy 2 full humanoid, 7-DOF arms x2, mobile base, stereo cameras
3. `pi5-picam` -- Raspberry Pi 5 + PiCamera Module 3, BLE transport
4. `pi-zero2w` -- Raspberry Pi Zero 2W + PiCamera, BLE, performance-constrained
5. `luxonis-oakd` -- Luxonis OAK-D AI camera, USB + HTTP, visual motion proxy
6. `frame-glasses` -- Brilliant Labs Frame AR glasses, BLE, 640x400 OLED display
7. `even-g2` -- Even Realities G2 smart glasses, BLE dual-arm, LC3 audio, BMP display

---

## v1.1 -- Complete ✅

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

## v1.2 -- Complete ✅

**Theme: Close the bidirectional loop + native OpenClaw integration**

### AgentResponseListener
- Wraps `runtime.events.onAgentEvent` to push agent responses back to device
- Routes text responses to `AudioOutputHal.speak()` and `DisplayHal.show_card()`
- `EmbodimentSDK.on_agent_response(callback)` wired into pipeline

### MicrophoneHal STT Bridge
- Add `transcribe(audio_chunk: AudioChunk) -> str` abstract method to `MicrophoneHal`
- Default implementation calls `openclaw agent --message` with audio file path
- OpenClaw routes to configured STT provider (Whisper, Deepgram, etc.)
- Removes need for per-device speech stack

### Heartbeat-Driven Event Wake
- `TriggerDetector` calls `requestHeartbeatNow()` on CAPTURE state
- Eliminates up to 30s latency between device event and agent response
- Configurable: `TriggerConfig.heartbeat_wake: bool = True`

### sessions_spawn Attachment Transport
- Camera frames and audio clips attachable directly to `sessions_spawn` turns
- Bypasses context query API for rich media — agent gets raw frame, not just embedding
- New `TransportHal` implementation: `AttachmentTransport`

### LocalMLXTransport — On-Device Inference
- New `TransportHal` implementation: `LocalMLXTransport`
- Routes context to a local MLX model (Qwen 3.5 2B 6-bit or equivalent) instead of the OpenClaw gateway
- Enables fully offline operation, sub-100ms inference latency, zero cloud dependency
- **Hybrid routing:** fast local model for immediate responses, escalate to OpenClaw gateway for complex multi-step reasoning
- Config: `transport: local_mlx`, `model: Qwen/Qwen3.5-2B-Instruct-6bit`, `fallback: http`
- First validated on: MacBook Pro M-series (.152) via mlx-lm
- Target device profiles: Pi 5 (via mlx on aarch64), iOS companion (iPhone 17 Pro via mlx-swift)
- Enables **iOS companion profile**: iPhone running MLX + Qwen 3.5 as edge compute layer for glasses (G2, Brilliant Labs Frame). Phone = brain, glasses = sensor/display surface.
- Reduces Phantom confabulation API cost: local passes for initial domain exploration, cloud for synthesis

---

## v2.0 -- Design Complete, Implementation Pending

**Theme: Unified orchestration + platform maturity**

> Specs: `docs/specs/v2.0-hal-orchestrator.md` and `docs/specs/v2.0-cross-embodiment.md`

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
