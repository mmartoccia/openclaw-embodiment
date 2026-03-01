# OpenClaw Wearable OS

> **The open agent OS for AI wearables.**
> Persistent memory, multi-agent orchestration, and ambient intelligence for any open wearable device.

---

## What Is OpenClaw Wearable OS?

OpenClaw Wearable OS is **not a glasses company**. It is an **agent OS layer** -- a persistent intelligence platform that connects to smart glasses and other open wearable hardware as sensor/display nodes.

Every AI glasses product shipping today is a feature, not a platform. They pipe audio or video to a cloud model and render the response. There is no persistent agent layer. No memory. No context between interactions. No autonomy.

OpenClaw already operates as a multi-agent intelligence platform for desktop and server environments. OpenClaw Wearable OS extends that layer to the physical edge, wiring it to the open wearables stack.

**Glasses are dumb nodes. OpenClaw is the intelligence layer.**

The glasses capture (camera, microphone, IMU) and render (display, bone conduction audio). All memory, reasoning, orchestration, and autonomy live in OpenClaw running on the paired device -- a phone, Mac, or edge compute node.

---

## Core Principles

- **Open** -- No proprietary lock-in. Hardware schematics, OS, and SDK are public. Community first.
- **Hardware-agnostic** -- Any Zephyr-based device. Any BLE/WiFi bridge. The reference device is OpenClaw Glasses, but the OS runs anywhere.
- **Privacy-first** -- Inference happens on the paired device or a trusted local edge node. Raw sensor data never leaves your hardware unless you choose it.
- **Persistent context** -- Context is the product. Ambient intelligence requires memory that survives across interactions, sessions, and days.
- **Agent-native** -- Not a voice assistant. A full multi-agent orchestration layer with memory, skills, cron-based autonomy, and proactive context push.

See [PRINCIPLES.md](PRINCIPLES.md) for the full design philosophy.

---

## Architecture Overview

```
[Glasses Hardware]           [Paired Device]              [OpenClaw]
  Camera           <------->  OpenClaw Node   <------->   Agent Fleet
  Microphone       BLE 5.3/  (phone / edge)              Memory
  Display          WiFi 6                                 Skills
  Touch / IMU                Context Engine              Multi-agent
                             Always-on listener          Cron / Autonomy
```

The glasses connect to the OpenClaw host over BLE 5.3 (low-power control and display commands) and WiFi (audio bursts on demand). The host runs the full OpenClaw agent stack -- STT, TTS, context sync, and multi-agent orchestration.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full breakdown of the bridge protocol, display pipeline, audio pipeline, and context sync.

---

## First Reference Device: OpenClaw Glasses

The first device purpose-built for this OS. OpenClaw Glasses is a hardware reference design built on:

- Zephyr RTOS
- BLE 5.3 + WiFi 6 connectivity
- Waveguide display
- 5MP camera module
- Bone conduction audio
- On-device NPU for edge inference

The glasses are the first node. The OS is the point.

---

## OpenClaw Core

OpenClaw Wearable OS builds on the OpenClaw agent platform.

- **OpenClaw shared resources:** [github.com/mmartoccia/shared-resources](https://github.com/mmartoccia/shared-resources)

---

## Get Involved

We are in early development. The best way to get involved right now:

- **Developer waitlist** (SDK access, early builds): [openclaw-wearable-os.vercel.app](https://openclaw-wearable-os.vercel.app)
- **OpenClaw Glasses waitlist** (reference hardware): [openclaw-glasses.vercel.app](https://openclaw-glasses.vercel.app)
- **Discussions:** Open a [GitHub Discussion](https://github.com/mmartoccia/openclaw-wearable/discussions) to ask questions, share ideas, or propose integrations.
- **Contributing:** Read [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

---

## Status

| Component | Status |
|---|---|
| Venture brief | Complete |
| Architecture spec | Complete |
| SDK (BLE bridge) | In design |
| Frame SDK integration | In design |
| Reference hardware | Planned |
| Enterprise pilots | Planned |

---

*OpenClaw Wearable OS -- ambient intelligence for the open edge.*
