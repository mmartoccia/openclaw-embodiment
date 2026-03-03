# OpenClaw Embodiment SDK

> **Context capture for physical AI.**
> A pure-Python hardware abstraction layer (HAL) that connects physical devices -- including Reachy Mini -- to the OpenClaw agent runtime. Robots, wearables, and edge compute in one open source SDK.

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-Gate%203%20complete-green.svg)](ROADMAP.md)
[![Repo](https://img.shields.io/badge/repo-mmartoccia%2Fopenclaw--embodiment-black.svg)](https://github.com/mmartoccia/openclaw-embodiment)

---

## What Is This?

Most embodied AI frameworks are **agent-to-device**: the agent issues commands and the hardware executes them. The OpenClaw Embodiment SDK flips the direction.

**Device-to-agent:** Hardware events capture context and trigger AI responses. The device notices; the agent thinks; the device acts. Actuation is a response path, not the primary control flow.

Compare:

| Framework | Direction | Paradigm |
|-----------|-----------|----------|
| **OM1** (2,668 stars) | Agent → Device | Command-control |
| **OpenClaw Embodiment SDK** | Device → Agent | Context capture |

No ROS dependency. No proprietary lock-in. Six device profiles ship out of the box. One HAL interface covers robots, AR glasses, edge cameras, and single-board computers.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Physical Hardware                      │
│  IMU · Camera · Microphone · Display · Actuator · Power │
└────────────────────────┬────────────────────────────────┘
                         │  HAL ABCs (openclaw_wearable.hal)
┌────────────────────────▼────────────────────────────────┐
│                  TriggerDetector                         │
│         IDLE → SACCADE → FIXATION → CAPTURE             │
└────────────────────────┬────────────────────────────────┘
                         │  ContextPacket (frame + IMU + audio)
┌────────────────────────▼────────────────────────────────┐
│              TransportHal (BLE or HTTP)                  │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│             OpenClaw Agent Runtime                       │
│        Memory · Skills · Multi-agent · Cron             │
└────────────────────────┬────────────────────────────────┘
                         │  Agent response
┌────────────────────────▼────────────────────────────────┐
│        ActuatorHal · AudioOutputHal · DisplayHal        │
└─────────────────────────────────────────────────────────┘
```

---

## Supported Hardware

| Device | Type | Transport | Status | Profile Name |
|--------|------|-----------|--------|--------------|
| Reachy Mini Lite (Pollen Robotics) | Robot | HTTP | Tested on CM5 | `reachy-mini` |
| Raspberry Pi 5 + PiCamera Module 3 | Edge compute | BLE | Implemented | `pi5-picam` |
| Raspberry Pi Zero 2W + PiCamera | Edge compute (constrained) | BLE | Implemented | `pi-zero2w` |
| Luxonis OAK-D | AI camera | USB + HTTP | Implemented | `luxonis-oakd` |
| Brilliant Labs Frame | AR glasses | BLE | Implemented (hw validation needed) | `frame-glasses` |
| Even Realities G2 | Smart glasses | BLE dual-arm | Implemented (hw validation needed) | `even-g2` |

---

## HAL Layer

Eight abstract base classes define the contract between hardware and agents. Each device profile implements the subset that applies.

| HAL | Purpose | Key Methods |
|-----|---------|-------------|
| `IMUHal` | Motion and orientation capture | `get_orientation()`, `get_acceleration()` |
| `CameraHal` | Frame capture | `capture_frame()`, `get_raw_frame()` |
| `MicrophoneHal` | Audio capture + direction of arrival | `capture_audio()`, `get_doa()` |
| `AudioOutputHal` | Speaker / TTS output | `speak()`, `play_audio()` |
| `DisplayHal` | Screen or LED expression | `render()`, `set_expression()` |
| `TransportHal` | Bidirectional data transport | `send()`, `receive()`, `connect()` |
| `ActuatorHal` | Physical actuation | `execute(command)`, `stop_all()`, `get_joint_states()` |
| `PowerHal` | Battery state and charging | `get_battery_level()`, `on_low_battery(cb)` |

`CameraHal.get_raw_frame()` is the escape hatch for device-native frame formats (e.g., OAK-D's `depthai.ImgFrame`).

`MicrophoneHal.get_doa()` returns Direction of Arrival as a stub -- wired to the `AudioTriggerDetector` in v1.1.

---

## Quick Start

```bash
pip install openclaw-embodiment
openclaw-embodiment doctor
```

The `doctor` command checks Python version, BLE adapter availability, USB devices (OAK-D), and OpenClaw runtime connectivity.

**Load a device profile:**

```python
from openclaw_wearable.profiles import load_profile

config = load_profile("reachy-mini")
# Returns a fully configured HAL bundle for Reachy Mini Lite
```

**Run the end-to-end pipeline:**

```python
from openclaw_wearable.profiles import load_profile
from openclaw_wearable.core.pipeline import EmbodimentPipeline

config = load_profile("reachy-mini")
pipeline = EmbodimentPipeline(config)
pipeline.run()  # Blocks; handles trigger→capture→transport→actuate loop
```

---

## Running the Demo

The `e2e_loop.py` example runs a full simulated embodiment loop -- no hardware required.

```bash
# Clone the repo
git clone https://github.com/mmartoccia/openclaw-embodiment
cd openclaw-embodiment

# Install dev dependencies
pip install -e ".[dev]"

# Run the simulation demo (no hardware needed)
openclaw-embodiment demo

# Or run the example directly
python examples/reachy_openclaw/e2e_loop.py --profile simulator
```

The demo uses `SimulatorHal` implementations that emit synthetic IMU, camera frames, and audio events through the full pipeline -- same code path as real hardware.

---

## TriggerProfiles

Each device has a tuned `TriggerProfile` that controls when a context capture fires. The state machine runs `IDLE → SACCADE → FIXATION → CAPTURE`.

| Profile Constant | Device | Saccade Threshold | Fixation Duration | Capture Cooldown |
|-----------------|--------|-------------------|-------------------|-----------------|
| `REACHY_MINI_TRIGGER_PROFILE` | Reachy Mini | Low (head motor latency) | 400 ms | 2.0 s |
| `GLASSES_TRIGGER_PROFILE` | Frame AR glasses | Medium (eye/head) | 300 ms | 1.5 s |
| `G2_TRIGGER_PROFILE` | Even G2 | Medium (IMU-based) | 350 ms | 1.5 s |
| `OAKD_TRIGGER_PROFILE` | Luxonis OAK-D | High (static mount) | 500 ms | 3.0 s |

Profiles are importable and overridable:

```python
from openclaw_wearable.core.trigger import REACHY_MINI_TRIGGER_PROFILE, TriggerDetector

detector = TriggerDetector(profile=REACHY_MINI_TRIGGER_PROFILE)
```

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `openclaw-embodiment init` | Initialize SDK config in current directory |
| `openclaw-embodiment check` | Check system readiness (connectivity, permissions) |
| `openclaw-embodiment demo` | Run simulated end-to-end demo |
| `openclaw-embodiment doctor` | Full dependency and permission checker |

---

## Project Status

| Gate | Description | Status |
|------|-------------|--------|
| Gate 1 | HAL ABC design + package scaffolding | Complete |
| Gate 2 | Reachy Mini HAL implementation + TriggerDetector | Complete |
| Gate 3 | CM5 hardware validation + 6 device profiles + 22 tests | **Complete** |
| Gate 4 | AudioTriggerDetector + TriggerArbiter (v1.1) | In progress |

---

## Roadmap

### v1.1 (Next)

- **AudioTriggerDetector** -- sound energy + DoA → orient → capture; parallel trigger path alongside visual saccade detection
- **TriggerArbiter** -- multi-modal fusion: visual and audio triggers with configurable priority policy
- **Latency-aware actuation** -- `get_expected_latency_ms()` on `TransportHal`; pipeline adjusts actuation timing
- **StatusIndicatorHal** -- LED state HAL: `set_color()`, `blink()`, `pulse()` for device status feedback
- **`openclaw-embodiment demo` auto-detection** -- profile detection from connected hardware at startup
- **Meta Ray-Ban profile** -- pending Meta Ray-Ban SDK public release; priority-1 wearable target
- **Apple Vision Pro profile** -- via VisionProTeleop bridge

### v2.0 (Future)

- **HalOrchestrator** -- explicit `trigger → capture → transport → actuate` loop as a first-class object; replaces manual pipeline wiring
- **SystemHealthHal** -- unified device health: thermals, connectivity, storage, sensor availability
- **Transport abstraction unification** -- BLE and HTTP transports share a single interface with latency metadata
- **Profile auto-discovery** -- detect connected hardware and load matching profile automatically

---

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

- Open issues for bug reports and feature requests
- PRs welcome -- new device profiles especially needed
- Hardware validation PRs (Frame, Even G2) are a high priority

---

## License

Apache 2.0. See [LICENSE](LICENSE).

---

*OpenClaw Embodiment SDK -- the device notices, the agent thinks, the device acts.*
