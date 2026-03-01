# OpenClaw Wearable OS -- Design Principles

These principles are not aspirations. They are constraints. Every architectural decision, hardware partnership, and feature should be evaluated against them.

---

## 1. Context Is the Product

Ambient intelligence requires persistent memory. A wearable that forgets what happened 10 minutes ago is not ambient -- it is a feature.

OpenClaw Wearable OS maintains a continuous context graph that spans interactions, sessions, and days. The glasses are a window into that context, not the container for it. Memory lives on the OpenClaw host. The glasses render what is relevant, when it is relevant.

**What this means in practice:**
- Every interaction is logged and indexed.
- Context is available across devices and sessions without re-establishing it.
- Proactive push is driven by what OpenClaw knows, not by what the user asks.

---

## 2. Hardware-Agnostic by Design

The OS must not be coupled to any single device. OpenClaw Wearable OS targets the Zephyr RTOS ecosystem because Zephyr is the emerging standard across the open smart glasses space. Any Zephyr-based device with BLE and WiFi connectivity is a valid node.

The reference device (OpenClaw Glasses) demonstrates what the OS can do with purpose-built hardware. It is not a requirement.

**What this means in practice:**
- The BLE bridge protocol is standardized and documented, not device-specific.
- Display rendering uses primitive commands (draw text, draw icon), not bitmaps tied to a specific display.
- The SDK targets MicroPython first for fastest adoption, Zephyr C for production hardening.

---

## 3. Open by Default

No proprietary lock-in. No walled garden. No undocumented protocols.

The bridge protocol, display pipeline, audio pipeline, and context sync format are all documented and open. Community contributions to the SDK and reference firmware are expected and welcomed.

**What this means in practice:**
- The SDK is open source (Apache 2.0 or MIT).
- Hardware reference designs are published with full schematics.
- The agent protocol does not require an OpenClaw account or cloud service to function.

---

## 4. Agent-Native, Not Assistant-Native

OpenClaw Wearable OS is not a voice assistant with glasses bolted on. It is a multi-agent orchestration layer with a wearable display node.

The distinction matters. A voice assistant responds to queries. An agent layer executes tasks, maintains context, runs background processes, and surfaces information proactively -- without waiting to be asked.

**What this means in practice:**
- The OS ships with cron-style background agents that push context without user initiation.
- Multi-agent orchestration is a first-class primitive, not a plugin.
- The glasses can display information the user did not ask for, because the system has enough context to know they need it.

---

## 5. Privacy at the Edge

Raw sensor data -- audio, camera frames, IMU telemetry -- does not leave the paired device unless the user explicitly configures it to. Inference happens on the OpenClaw host or a trusted local edge node.

This is not just a privacy preference. For the federal field worker beachhead, data sovereignty is a hard requirement. The architecture must support fully air-gapped operation.

**What this means in practice:**
- STT (speech-to-text) runs locally on the paired device (e.g., Whisper, macOS STT).
- TTS runs locally.
- No cloud API is required for core functionality.
- Network calls are opt-in, not default.

---

*These principles evolve as the project evolves. Changes require broad consensus from core contributors.*
