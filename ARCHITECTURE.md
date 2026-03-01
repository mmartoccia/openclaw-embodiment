# OpenClaw Wearable OS -- Architecture

This document covers the high-level architecture of OpenClaw Wearable OS. It is intentionally readable, not a full specification. Implementation details live in the SDK.

---

## System Overview

OpenClaw Wearable OS has two physical components and one logical layer:

1. **The Glasses (Edge Node):** A Zephyr-based (or MicroPython) device with a camera, microphone, display, and touch/IMU sensors. Computationally constrained. Responsible only for capture and render.

2. **The OpenClaw Host:** A paired device (phone, Mac, or edge compute node) running the full OpenClaw agent stack. Handles all intelligence, memory, and orchestration.

3. **The Bridge:** A BLE 5.3 + WiFi protocol that connects the two.

```
[ Glasses -- Edge Node ]              [ OpenClaw Host ]
  Camera (5MP)                          STT Engine (local Whisper)
  Microphone     <--- WiFi (audio) ---> TTS Engine (local)
  Display        <--- BLE (commands) -> Layout Manager
  Touch / IMU    --- BLE (events) --->  Context Engine
  Bone conduction <- BLE/A2DP (audio)   Agent Fleet
                                        Memory / Skills / Cron
```

---

## Bridge Protocol

**Transport layers:**
- **BLE 5.3 GATT** -- primary channel for display commands, sensor events, and keep-alives. Low power. Always-on.
- **WiFi 6 (burst)** -- audio upload only. Activated on demand (tap-to-talk), then sleeps.
- **Bluetooth A2DP** -- audio playback from host TTS to glasses bone conduction speakers.

**Message format:** Protobuf over BLE, COBS-encoded for framing. Chosen over JSON because BLE MTU is 244 bytes and JSON is too verbose for constrained packets.

**Service UUID:** `0x0C1A` (OpenClaw) -- advertised by the glasses for host discovery.

**Connection lifecycle:**
1. Glasses advertise on power-on.
2. OpenClaw host connects and negotiates MTU.
3. 1-byte keep-alive ping every 5 seconds. Three missed pings triggers deep sleep + re-advertise.

---

## Display Pipeline

The glasses display (e.g., 640x400 waveguide OLED) is driven by primitive commands, not raw bitmaps. Streaming bitmaps over BLE is too slow.

**Pipeline:**
1. OpenClaw host generates a display command: `DRAW_TEXT(x, y, text, color)`, `DRAW_ICON(x, y, icon_id)`, `CLEAR()`.
2. Command is packed into a Protobuf packet and sent over BLE.
3. The glasses-side firmware executes the primitive using the native display driver.

**Why primitives?** A 640x400 bitmap is roughly 100KB per frame. Even compressed, that is too large for BLE without significant latency. A text command is under 50 bytes.

The layout manager on the host is responsible for fitting content to the screen real estate -- text wrapping, font sizing, and priority-based overlay management.

---

## Audio Pipeline

**Capture (glasses to host):**
1. User taps the glasses frame.
2. Tap event is sent over BLE: `EVENT_TAP`.
3. Glasses wake WiFi and POST a 16kHz, 16-bit PCM audio chunk to the OpenClaw host's local HTTP endpoint.
4. WiFi sleeps after transfer.

**Why WiFi for audio?** BLE bandwidth is too constrained for reliable low-latency audio. WiFi handles 16kHz audio effortlessly and is only active during the burst.

**Processing (host):**
1. OpenClaw host receives the audio chunk.
2. Local STT (Whisper or system STT) transcribes.
3. Agent processes the intent.
4. Response is generated and passed to local TTS.

**Playback (host to glasses):**
1. TTS audio is streamed to glasses bone conduction speakers via standard Bluetooth A2DP.
2. No custom audio streaming protocol -- uses the existing OS audio routing stack.

Simultaneously, a display command is sent over BLE to render a text summary on the glasses display.

---

## Agent Context Sync

There are two modes of context delivery to the glasses:

**On-demand (user-initiated):**
- Trigger: Tap gesture on glasses frame.
- Flow: Tap event -> audio capture -> STT -> agent processing -> display command + audio response.
- Latency target: under 800ms end-to-end.

**Proactive push (agent-initiated):**
- Trigger: OpenClaw background agent detects a relevant event (calendar alert, priority message, anomaly).
- Flow: Agent generates a display command -> sends over BLE to glasses.
- Latency target: under 200ms from agent decision to screen render.
- The glasses do not need to be in an active interaction for proactive push to work.

---

## SDK Build Order (Reference Implementation)

| Phase | Goal | Milestone |
|---|---|---|
| Week 1 | BLE bridge + display | "Hello World" from terminal to glasses display |
| Week 2 | Audio + intent | Tap, speak, get answer on display |
| Week 3 | Proactive context | Glasses surface calendar alerts and priority messages unprompted |
| Week 4 | Polish + packaging | OpenClaw skill, published MicroPython firmware |

---

## Technology Decisions

| Component | Choice | Reason |
|---|---|---|
| Edge OS (MVP) | MicroPython | Fastest path. Avoids Zephyr C toolchain for initial demo. |
| Edge OS (production) | Zephyr C | Power efficiency, security hardening, federal readiness. |
| Control channel | BLE 5.3 GATT | Low power, always-on, sufficient for UI primitives and events. |
| Audio upload | WiFi burst | BLE bandwidth is insufficient for reliable audio upload. |
| Audio playback | Bluetooth A2DP | Standard OS audio routing. No custom protocol needed. |
| Message format | Protobuf | Compact binary. Handles 244-byte BLE MTU efficiently. |
| Host BLE library | Python `bleak` | Cross-platform, async, well-maintained. |
| STT | Local Whisper / macOS STT | No cloud dependency. Runs on the paired device. |
| TTS | Local (macOS or Kokoro) | No cloud dependency. |

---

*Full specifications, Protobuf schemas, and firmware implementation details are in the SDK repository (coming soon).*
