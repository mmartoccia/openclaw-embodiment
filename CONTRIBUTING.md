# Contributing

The SDK is a hardware abstraction layer. It captures context from physical devices, packages it, and delivers it to an agent runtime. It has no opinion about what the agent does.

Keep that boundary clean.

---

## What belongs here

- HAL implementations for real hardware
- Device profiles (capability vectors + trigger profiles)
- Context schema definitions (`SensorContext`, `WorldModel`, `ResponseBurst`)
- Transport implementations (BLE, HTTP, stdio)
- Tests that run without hardware

## What doesn't

- Agent behavior, response policy, memory systems
- OpenClaw-specific code or imports
- Hardcoded URLs, API keys, user config
- Anything that only works on one machine

---

## Adding a device profile

1. Implement the relevant HAL ABCs in `openclaw_embodiment/hal/`
2. Add a `DeviceCapabilityVector` constant in `context_builder.py`
3. Add a `TriggerProfile` in `openclaw_embodiment/core/trigger.py`
4. Add the profile to the supported hardware table in `README.md`
5. Validate on actual hardware before opening a PR -- spec-only profiles go in a separate branch

Hardware validation means: `validate()` returns `True` on a real device, not just in simulation.

---

## Code standards

Comments explain why, not what. If you're writing `# increment counter`, delete it.

No generic exception swallowing:
```python
# wrong
except Exception as e:
    logger.error(e)

# right -- handle what you can, let the rest propagate
except subprocess.TimeoutExpired:
    raise RuntimeError(f"arecord timed out on {self.DEVICE}")
```

TODOs must be specific:
```python
# wrong
# TODO: improve this

# right
# TODO: replace edge-density heuristic with MobileNet person detector (needs ~50MB model)
```

---

## Commits

Follow the format already in the log:

```
feat: BLEProximityScanner -- bleak 2.x, ProximityContext, RSSI map
fix: arecord minimum poll is 1s, not configurable poll_duration_ms
spec: v0.2 -- Adaptive Attention Principle
```

One thing per commit. If you're writing "and" in the subject line, split it.

---

## Tests

Tests run without hardware. Use mock HALs for anything that touches a device. If a test requires SSH to a real device, it goes in `tests/hardware/` and is excluded from CI.

---

## Known issues

- Duplicate commits in history (MicrophoneHal.transcribe, iOS companion, LocalMLX) -- artifact from an early merge. History is on GitHub so it stays. Don't add more.
- OV5647 color pipeline broken on Distiller CM5 alpha unit -- `color_reliable=False` is the correct fix, not a tuning file.
- arecord minimum effective poll: 1s regardless of configured duration.
