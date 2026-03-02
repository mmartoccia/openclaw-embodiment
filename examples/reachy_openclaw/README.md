# Reachy Mini Lite + OpenClaw

This example connects a Reachy Mini Lite robot to an OpenClaw agent via the Wearable SDK.

Reachy watches the room. When it looks at something and holds its gaze (saccade → fixation),
OpenClaw captures context (camera frame + audio) and responds. Reachy reacts via head animation.

## Hardware Required

- [Reachy Mini Lite](https://www.hf.co/reachy-mini/) ($299, assembled kit)
- Mac or Linux host connected via USB-C
- OpenClaw gateway running on the same host (or Tailscale-accessible)

## Quick Start

```bash
# 1. Install dependencies
pip install reachy-mini opencv-python numpy scipy
pip install -e "../../"

# 2. Start Reachy Mini daemon (robot must be connected via USB)
reachy-mini-daemon

# 3. Run
python app.py
```

## How It Works

The `ReachyHeadEncoderIMU` HAL reads head servo encoder positions at 10Hz and
derives angular velocity (degrees/s). This substitutes for a physical IMU:

- **Saccade** = head moving fast to new position (>30 dps)
- **Fixation** = head position stable for >600ms (<5 dps)
- **Trigger** = saccade followed by fixation → capture frame + audio

The captured context is sent to OpenClaw via HTTP transport (localhost or Tailscale).
The agent processes the context and returns a response, which the robot acknowledges
with a subtle head nod and antenna raise.

## Trigger Tuning

Robot head movements are much slower than human eye saccades. The `REACHY_TRIGGER_CONFIG`
in `reachy_reference.py` is pre-tuned for robot pacing:

```python
TriggerConfig(
    saccade_threshold_dps=30,    # vs 180 for human eyes
    fixation_duration_ms=600,    # vs 400 for humans
    refractory_period_ms=2000,   # 2s between captures
)
```

Adjust these values based on how you want Reachy to behave.
