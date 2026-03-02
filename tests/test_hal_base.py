from openclaw_wearable.hal.base import AudioOutputHal, CameraHal, ClassifierHal, DisplayHal, IMUHal, MicrophoneHal, TransportHal
from openclaw_wearable.hal.simulator import SimulatedAudioOutput, SimulatedCamera, SimulatedClassifier, SimulatedDisplay, SimulatedIMU, SimulatedMicrophone, SimulatedTransport


def test_hal_contract_instances():
    assert isinstance(SimulatedIMU(), IMUHal)
    assert isinstance(SimulatedCamera(), CameraHal)
    assert isinstance(SimulatedMicrophone(), MicrophoneHal)
    assert isinstance(SimulatedClassifier(), ClassifierHal)
    assert isinstance(SimulatedTransport(), TransportHal)
    assert isinstance(SimulatedDisplay(), DisplayHal)
    assert isinstance(SimulatedAudioOutput(), AudioOutputHal)
