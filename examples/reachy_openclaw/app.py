"""Reachy Mini Lite + OpenClaw integration demo.

Reachy watches the room. When it looks at something and holds its gaze,
OpenClaw captures context and responds. The robot reacts via head animation.

Requirements:
  pip install reachy-mini opencv-python numpy scipy
  pip install -e "../../"  # openclaw-wearable SDK

Usage:
  # 1. Start Reachy Mini daemon (USB connected):
  #    reachy-mini-daemon
  # 2. Run this app:
  #    python app.py
"""

from reachy_mini import ReachyMini

from openclaw_embodiment.core.pipeline import HALRegistry, EmbodimentSDK
from openclaw_embodiment.core.trigger import TriggerConfig
from openclaw_embodiment.hal.reachy_reference import (
    REACHY_TRIGGER_CONFIG,
    ReachyAudioOutputHAL,
    ReachyCameraHAL,
    ReachyDisplayHAL,
    ReachyHeadEncoderIMU,
    ReachyMicrophoneHAL,
    ReachyTransportHAL,
)


def main() -> None:
    print("Starting Reachy + OpenClaw...")

    with ReachyMini(media_backend="default") as reachy:
        registry = HALRegistry()
        registry.register_imu(ReachyHeadEncoderIMU(reachy))
        registry.register_camera(ReachyCameraHAL(reachy))
        registry.register_microphone(ReachyMicrophoneHAL(reachy))
        registry.register_audio_output(ReachyAudioOutputHAL(reachy))
        registry.register_display(ReachyDisplayHAL(reachy))
        registry.register_transport(ReachyTransportHAL(), priority=0)

        trigger_config = TriggerConfig(**REACHY_TRIGGER_CONFIG)
        sdk = EmbodimentSDK(registry, trigger_config=trigger_config)

        sdk.on_trigger(lambda e: print(f"[TRIGGER] {e.event_id} confidence={e.trigger_confidence:.2f}"))
        sdk.on_response(lambda r: print(f"[RESPONSE] {r.title}: {r.body}"))

        sdk.start()
        print("Reachy is watching. Ctrl+C to stop.")

        try:
            import time
            while True:
                time.sleep(0.1)
        except KeyboardInterrupt:
            pass
        finally:
            sdk.stop()
            print("Stopped.")


if __name__ == "__main__":
    main()
