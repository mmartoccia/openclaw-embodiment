"""Frame v1.0 cloud-gate stubs.

Frame devices in v1.0 rely on node-side classification.
"""

from .simulator import SimulatedCamera, SimulatedIMU, SimulatedMicrophone, SimulatedTransport, SimulatedDisplay

FrameIMU = SimulatedIMU
FrameCamera = SimulatedCamera
FrameMicrophone = SimulatedMicrophone
FrameBLETransport = SimulatedTransport
FrameDisplay = SimulatedDisplay
