"""Pytest fixtures for simulator-backed tests."""

import pytest

from openclaw_wearable.core.pipeline import HALRegistry, WearableSDK
from openclaw_wearable.core.trigger import TriggerConfig
from openclaw_wearable.hal.simulator import SimulatedCamera, SimulatedClassifier, SimulatedDisplay, SimulatedIMU, SimulatedMicrophone, SimulatedTransport


@pytest.fixture
def simulator_registry():
    reg = HALRegistry()
    reg.register_imu(SimulatedIMU())
    reg.register_camera(SimulatedCamera())
    reg.register_microphone(SimulatedMicrophone())
    reg.register_classifier(SimulatedClassifier())
    reg.register_transport(SimulatedTransport())
    reg.register_display(SimulatedDisplay())
    return reg


@pytest.fixture
def simulator_sdk(simulator_registry):
    # Use short thresholds so simulator IMU pattern fires within test window
    test_config = TriggerConfig(
        saccade_duration_ms=40,
        fixation_duration_ms=80,
        refractory_period_ms=200,
    )
    sdk = WearableSDK(simulator_registry, trigger_config=test_config)
    yield sdk
    sdk.stop()
