# Luxonis OAK-D Spatial AI Camera Hardware Validation Checklist

## Prerequisites
- [ ] Luxonis OAK-D or OAK-D Lite connected via USB 3.0
- [ ] `pip install depthai`
- [ ] udev rules installed (Linux): `echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="03e7", MODE="0666"' | sudo tee /etc/udev/rules.d/80-movidius.rules`

## Environment Setup
- [ ] USB 3.0 port used (not USB 2.0 -- performance significantly degraded)
- [ ] Verify detection: `lsusb | grep 03e7` or `system_profiler SPUSBDataType | grep Movidius`
- [ ] DepthAI version ≥ 2.24.0: `python -c "import depthai; print(depthai.__version__)"`

## HAL Tests
- [ ] **CameraHal**: `capture_frame()` returns CameraFrame within 2s
  - Expected: RGB JPEG from 12MP IMX378, non-null data
  - Test: `frame = hal.capture_frame(); assert len(frame.data) > 10000`
- [ ] **CameraHal**: Depth map accessible (OAK-D specific)
  - Expected: Stereo depth data available on left/right cameras
- [ ] **ClassifierHal**: `classify()` returns ClassificationResult within 1s
  - Expected: confidence 0.0-1.0, inference via OAK-D Myriad X VPU
- [ ] **TransportHal**: USB transport to host PC working
- [ ] **TransportHal**: `send()` returns SendResult(success=True)
- [ ] **TransportHal**: `get_expected_latency_ms()` returns ≤20 (USB 3.0)

## Pipeline Tests
- [ ] Full trigger→capture→transport cycle completes in <5s
- [ ] Agent receives CameraFrame with populated RGB frame
- [ ] Spatial depth data included in context when available
- [ ] On-device inference via VPU faster than host CPU

## Performance Benchmarks
- [ ] RGB capture latency: <100ms at full resolution
- [ ] Depth inference latency: <50ms
- [ ] USB transport latency: <20ms
- [ ] End-to-end pipeline: <3s

## Error Scenarios
- [ ] USB disconnect: HAL logs error, pipeline pauses, reconnect attempted
- [ ] OAK-D overheats: temperature warning logged, frame rate reduced

## Profile Validator
- [ ] `openclaw-embodiment validate luxonis-oakd` returns PASS
