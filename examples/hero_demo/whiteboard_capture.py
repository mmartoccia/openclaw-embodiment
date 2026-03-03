"""Hero demo for contextual whiteboard capture."""

import time

from openclaw_embodiment.core.pipeline import HALRegistry, EmbodimentSDK
from openclaw_embodiment.hal.simulator import SimulatedCamera, SimulatedClassifier, SimulatedDisplay, SimulatedIMU, SimulatedMicrophone, SimulatedTransport


def main() -> None:
    """Run full demo pipeline with simulator HALs."""
    registry = HALRegistry()
    registry.register_imu(SimulatedIMU())
    registry.register_camera(SimulatedCamera())
    registry.register_microphone(SimulatedMicrophone())
    registry.register_classifier(SimulatedClassifier())
    registry.register_transport(SimulatedTransport(), priority=0)
    registry.register_display(SimulatedDisplay())
    sdk = EmbodimentSDK(registry)
    sdk.on_response(lambda r: print("[DEMO]", r.title, r.body))
    sdk.start()
    time.sleep(1.5)
    sdk.stop()


if __name__ == "__main__":
    main()
