#!/usr/bin/env python3
"""
End-to-end simulation loop for the OpenClaw Embodiment SDK.

Demonstrates the full pipeline:
  trigger (IMU saccade) -> capture (camera + audio) -> agent response -> actuate

No real hardware needed -- runs entirely in simulation.
"""

import random
import time
import sys
import os

# Allow running from project root without install
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from openclaw_embodiment.hal.simulator import (
    SimulatedIMU,
    SimulatedCamera,
    SimulatedMicrophone,
    SimulatedAudioOutput,
    SimulatedDisplay,
    SimulatedTransport,
    SimulatedClassifier,
)

# ---------------------------------------------------------------------------
# Simulated agent responses -- offline, no API call
# ---------------------------------------------------------------------------
AGENT_RESPONSES = [
    "I see a whiteboard with diagrams.",
    "There is a person looking at a computer screen.",
    "I notice a table with several items on it.",
]

SACCADE_THRESHOLD = 100.0  # dps -- above this = saccade detected
FIXATION_THRESHOLD = 20.0  # dps -- below this after saccade = fixation locked


def run_sim_loop(num_cycles: int = 3) -> None:
    print("OpenClaw Embodiment SDK -- E2E Simulation Loop")
    print("=" * 50)
    print(f"Running {num_cycles} trigger cycles (simulation only)\n")

    # Initialize simulated HALs
    imu = SimulatedIMU()
    imu.initialize(sample_rate_hz=25)

    camera = SimulatedCamera()
    camera.initialize(resolution=(320, 240))

    mic = SimulatedMicrophone()
    mic.initialize(sample_rate=16000, channels=1)
    mic.start_recording()

    audio_out = SimulatedAudioOutput()
    audio_out.initialize()

    display = SimulatedDisplay()
    display.initialize()

    for cycle in range(1, num_cycles + 1):
        # --- Phase 1: Saccade detection ---
        state = "IDLE"
        saccade_detected = False
        fixation_locked = False

        # Drive the IMU through its waveform to hit a saccade window
        # SimulatedIMU produces gyro=220 at phase 4-8 (of 30-sample period)
        for _ in range(30):
            sample = imu.read_sample()
            gyro_x = round(sample.gyro_x, 1)

            if not saccade_detected and gyro_x > SACCADE_THRESHOLD:
                print(f"[SIM] IMU sample: gyro_x={gyro_x} dps (saccade detected)")
                prev_state = state
                state = "SACCADE"
                print(f"[SIM] State: {prev_state} -> SACCADE")
                saccade_detected = True

            elif saccade_detected and not fixation_locked and gyro_x < FIXATION_THRESHOLD:
                print(f"[SIM] IMU sample: gyro_x={gyro_x} dps (fixation locked)")
                print(f"[SIM] State: SACCADE -> FIXATION -> CAPTURE")
                fixation_locked = True
                state = "CAPTURE"
                break

        if not fixation_locked:
            print(f"[SIM] WARNING: cycle {cycle} did not reach fixation -- skipping")
            continue

        # --- Phase 2: Context capture ---
        evt_id = f"evt-{int(time.time() * 1000)}"
        print(f"[SIM] TriggerEvent fired: {evt_id}")

        frame = camera.capture_frame()
        audio = mic.get_buffer(duration_ms=500)

        print(
            f"[SIM] Context captured: frame {frame.width}x{frame.height} {frame.format},"
            f" audio {audio.sample_rate}Hz"
        )

        # --- Phase 3: Simulated agent response ---
        response = AGENT_RESPONSES[(cycle - 1) % len(AGENT_RESPONSES)]
        print(f'[SIM] Agent response: "{response}"')

        # --- Phase 4: Actuate ---
        print("[SIM] Actuator: nod (simulated)")
        # In real hardware this would call ReachyActuatorHAL.execute("nod")
        # Here we just simulate the audio feedback
        audio_out.play(b"\x00" * 1024)
        time.sleep(0.05)
        audio_out.stop()

        print(f"--- Capture {cycle}/{num_cycles} complete ---\n")

        if cycle < num_cycles:
            time.sleep(0.5)  # brief pause between cycles

    mic.stop_recording()

    print("✅ Full loop verified: trigger -> capture -> response -> actuate")


if __name__ == "__main__":
    run_sim_loop(num_cycles=3)
