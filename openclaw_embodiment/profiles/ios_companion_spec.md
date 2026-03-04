# iOS Companion App — OpenClaw Embodiment SDK Protocol Spec

**Version:** 1.0  
**Receiver port:** 18800  
**Transport:** HTTP/1.1 over local WiFi  
**Auth:** HMAC-SHA256 per-request signing  

---

## Overview

The iOS Companion Profile enables an iPhone to function as a live sensor node in the OpenClaw Embodiment network. The iPhone runs a companion app (not part of this SDK) that captures sensor data via native iOS APIs and streams it to the Python-side receiver over local WiFi.

```
iPhone (companion app)
  ├── AVFoundation  → camera frames  ──┐
  ├── AVAudioEngine → audio chunks   ──┤
  ├── CoreMotion    → IMU samples    ──┼──► POST HTTP → iOSCompanionReceiver (port 18800)
  ├── CoreLocation  → GPS fixes      ──┤                      │
  └── UIDevice      → battery state  ──┘                      ▼
                                                       Agent context pipeline
```

**Key constraints:**
- All communication is local network only (no cloud relay)
- iPhone and receiver host must be on the same WiFi network (or connected via USB Ethernet)
- Camera max: **30 fps**
- IMU max: **50 Hz**
- Audio: continuous chunked streaming (recommend 100 ms chunks)

---

## Authentication

Every POST request must include an HMAC-SHA256 signature computed over the raw request body.

**Header:** `X-OpenClaw-Signature: <hex-digest>`

### Setup

1. Generate a shared secret (32+ random bytes) and store it in `~/.secrets/ios-companion.env`:
   ```
   IOS_COMPANION_SECRET=<hex-encoded-secret>
   ```

2. Pass the secret to the receiver:
   ```python
   import os
   secret = bytes.fromhex(os.environ["IOS_COMPANION_SECRET"])
   receiver = iOSCompanionReceiver(hmac_secret=secret)
   ```

3. In the iOS app, sign each request body:

   ```swift
   import CryptoKit

   func signBody(_ body: Data, secret: SymmetricKey) -> String {
       let mac = HMAC<SHA256>.authenticationCode(for: body, using: secret)
       return Data(mac).map { String(format: "%02x", $0) }.joined()
   }

   // Add to every URLRequest:
   request.setValue(signBody(body, secret: sharedSecret),
                    forHTTPHeaderField: "X-OpenClaw-Signature")
   ```

**Development mode:** Pass `hmac_secret=None` to disable auth verification (never use in production).

---

## Endpoint Reference

All endpoints accept `Content-Type: application/json` POST requests.

### Common request envelope

Every payload must include these top-level fields:

| Field | Type | Description |
|---|---|---|
| `device_id` | string | iPhone UUID (`UIDevice.current.identifierForVendor?.uuidString`) |
| `sensor_type` | string | One of: `imu`, `camera`, `audio`, `location`, `battery` |
| `timestamp` | float | Unix epoch timestamp from device clock (seconds.microseconds) |
| `format_version` | string | Always `"1.0"` |
| `data` | object | Sensor-specific payload (see below) |

### Common responses

| HTTP Code | Body |
|---|---|
| `200 OK` | `{"status": "ok", "received_at": <unix_timestamp>}` |
| `400 Bad Request` | `{"status": "error", "message": "<description>"}` |
| `401 Unauthorized` | `{"status": "error", "message": "invalid signature"}` |
| `404 Not Found` | `{"status": "error", "message": "Unknown endpoint: <path>"}` |
| `500 Internal Server Error` | `{"status": "error", "message": "<description>"}` |

---

### POST /sensor/imu

CoreMotion accelerometer + gyroscope sample.

**`data` fields:**

| Field | Type | Unit | Description |
|---|---|---|---|
| `accel_x` | float | m/s² | Acceleration X axis |
| `accel_y` | float | m/s² | Acceleration Y axis |
| `accel_z` | float | m/s² | Acceleration Z axis |
| `gyro_x` | float | rad/s | Angular velocity X |
| `gyro_y` | float | rad/s | Angular velocity Y |
| `gyro_z` | float | rad/s | Angular velocity Z |
| `sample_rate_hz` | int | Hz | Actual sample rate (max 50) |

**Example:**
```json
{
  "device_id": "A1B2-C3D4-E5F6-G7H8",
  "sensor_type": "imu",
  "timestamp": 1709500000.123,
  "format_version": "1.0",
  "data": {
    "accel_x": 0.012,
    "accel_y": -9.806,
    "accel_z": 0.054,
    "gyro_x": 0.001,
    "gyro_y": -0.003,
    "gyro_z": 0.000,
    "sample_rate_hz": 50
  }
}
```

**Swift CoreMotion snippet:**
```swift
let motionManager = CMMotionManager()
motionManager.deviceMotionUpdateInterval = 1.0 / 50.0  // 50 Hz

motionManager.startDeviceMotionUpdates(to: .main) { motion, _ in
    guard let m = motion else { return }
    let payload = IMUPayload(
        accel_x: m.userAcceleration.x * 9.806,
        accel_y: m.userAcceleration.y * 9.806,
        accel_z: m.userAcceleration.z * 9.806,
        gyro_x: m.rotationRate.x,
        gyro_y: m.rotationRate.y,
        gyro_z: m.rotationRate.z,
        sample_rate_hz: 50
    )
    // POST to /sensor/imu
}
```

---

### POST /sensor/camera

JPEG or PNG camera frame. Frames must be gzip-compressed before base64 encoding.

**`data` fields:**

| Field | Type | Description |
|---|---|---|
| `width` | int | Frame width in pixels |
| `height` | int | Frame height in pixels |
| `format` | string | `"JPEG"` or `"PNG"` |
| `encoding` | string | `"gzip+b64"` (required for camera) |
| `frame_data` | string | gzip(frame_bytes) → base64 encoded |

**Example:**
```json
{
  "device_id": "A1B2-C3D4-E5F6-G7H8",
  "sensor_type": "camera",
  "timestamp": 1709500000.033,
  "format_version": "1.0",
  "data": {
    "width": 1920,
    "height": 1080,
    "format": "JPEG",
    "encoding": "gzip+b64",
    "frame_data": "<gzip-compressed-JPEG-then-base64>"
  }
}
```

**Swift AVFoundation snippet:**
```swift
func captureOutput(_ output: AVCaptureOutput,
                   didOutput sampleBuffer: CMSampleBuffer,
                   from connection: AVCaptureConnection) {
    guard let imageBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
    let ciImage = CIImage(cvPixelBuffer: imageBuffer)
    let jpegData = context.jpegRepresentation(of: ciImage, colorSpace: .sRGB)!
    
    let compressed = try! (jpegData as NSData).compressed(using: .zlib) as Data
    let encoded = compressed.base64EncodedString()
    
    let payload = CameraPayload(width: 1920, height: 1080,
                                format: "JPEG", encoding: "gzip+b64",
                                frame_data: encoded)
    // POST to /sensor/camera (max 30fps -- throttle with timer)
}
```

**Rate limiting:** The app must throttle to ≤30 fps. The receiver does not enforce a frame rate limit but high frame rates will saturate WiFi and the pipeline.

---

### POST /sensor/audio

PCM audio chunk from iPhone microphone.

**`data` fields:**

| Field | Type | Description |
|---|---|---|
| `sample_rate` | int | Sample rate in Hz (recommended: 16000) |
| `channels` | int | Number of channels (1 = mono recommended) |
| `format` | string | `"PCM_S16LE"` |
| `encoding` | string | `"b64"` |
| `audio_data` | string | Base64-encoded raw PCM bytes |
| `duration_ms` | int | Duration of this chunk in milliseconds |

**Example:**
```json
{
  "device_id": "A1B2-C3D4-E5F6-G7H8",
  "sensor_type": "audio",
  "timestamp": 1709500000.100,
  "format_version": "1.0",
  "data": {
    "sample_rate": 16000,
    "channels": 1,
    "format": "PCM_S16LE",
    "encoding": "b64",
    "audio_data": "<base64-encoded-PCM-bytes>",
    "duration_ms": 100
  }
}
```

**Swift AVAudioEngine snippet:**
```swift
let engine = AVAudioEngine()
let format = AVAudioFormat(commonFormat: .pcmFormatInt16,
                            sampleRate: 16000, channels: 1,
                            interleaved: true)!
engine.inputNode.installTap(onBus: 0, bufferSize: 1600, format: format) { buffer, time in
    let data = Data(buffer: buffer)
    let encoded = data.base64EncodedString()
    let payload = AudioPayload(sample_rate: 16000, channels: 1,
                               format: "PCM_S16LE", encoding: "b64",
                               audio_data: encoded, duration_ms: 100)
    // POST to /sensor/audio
}
try! engine.start()
```

---

### POST /sensor/location

CoreLocation GPS fix.

**`data` fields:**

| Field | Type | Unit | Description |
|---|---|---|---|
| `latitude` | float | degrees | WGS-84 latitude |
| `longitude` | float | degrees | WGS-84 longitude |
| `altitude` | float | meters | Altitude above sea level |
| `accuracy_m` | float | meters | Horizontal accuracy radius |
| `speed_ms` | float | m/s | Ground speed (-1 if unknown) |
| `heading_deg` | float | degrees | True north heading (-1 if unknown) |

**Example:**
```json
{
  "device_id": "A1B2-C3D4-E5F6-G7H8",
  "sensor_type": "location",
  "timestamp": 1709500000.000,
  "format_version": "1.0",
  "data": {
    "latitude": 37.7749,
    "longitude": -122.4194,
    "altitude": 12.5,
    "accuracy_m": 5.0,
    "speed_ms": 0.0,
    "heading_deg": 270.0
  }
}
```

**Recommended strategy:** Use `CLLocationManager` in significant-change mode to avoid battery drain. Only send when location changes by >10m or every 30 seconds.

---

### POST /sensor/battery

UIDevice battery state.

**`data` fields:**

| Field | Type | Description |
|---|---|---|
| `level` | float | Battery level 0.0–1.0 |
| `state` | string | `"charging"`, `"full"`, `"unplugged"`, `"unknown"` |
| `low_power_mode` | bool | `ProcessInfo.processInfo.isLowPowerModeEnabled` |

**Example:**
```json
{
  "device_id": "A1B2-C3D4-E5F6-G7H8",
  "sensor_type": "battery",
  "timestamp": 1709500000.000,
  "format_version": "1.0",
  "data": {
    "level": 0.82,
    "state": "unplugged",
    "low_power_mode": false
  }
}
```

---

## SwiftUI Integration Guide

### 1. Add the OpenClawCompanion SDK target

Create a new Swift file `OpenClawClient.swift` in your SwiftUI project:

```swift
import Foundation
import CryptoKit

class OpenClawClient: ObservableObject {
    let receiverURL: URL
    let secret: SymmetricKey
    
    init(host: String = "192.168.1.183", port: Int = 18800, secretHex: String) {
        self.receiverURL = URL(string: "http://\(host):\(port)")!
        let secretData = Data(hexString: secretHex)!
        self.secret = SymmetricKey(data: secretData)
    }
    
    func post(endpoint: String, payload: Encodable) async throws {
        var request = URLRequest(url: receiverURL.appendingPathComponent(endpoint))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        
        let body = try JSONEncoder().encode(payload)
        request.httpBody = body
        
        // Sign
        let mac = HMAC<SHA256>.authenticationCode(for: body, using: secret)
        let sig = Data(mac).hexString
        request.setValue(sig, forHTTPHeaderField: "X-OpenClaw-Signature")
        
        let (_, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else {
            throw OpenClawError.badResponse
        }
    }
}
```

### 2. Wire up in your SwiftUI App

```swift
@main
struct CompanionApp: App {
    @StateObject var client = OpenClawClient(secretHex: "your-hex-secret")
    @StateObject var sensorManager = SensorManager()
    
    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(client)
                .environmentObject(sensorManager)
                .onAppear {
                    sensorManager.start(client: client)
                }
        }
    }
}
```

### 3. Background streaming

Enable **Background Modes** in Xcode → Capabilities:
- ✅ Location updates
- ✅ Audio (for background mic streaming)
- ✅ Background fetch

---

## Rate Limiting and Backpressure

### Receiver-side backpressure signals

The receiver returns `200 OK` immediately after enqueuing data. If the agent pipeline is slow to consume, buffers grow. The app should implement client-side rate limiting:

| Sensor | Max rate | Recommended |
|---|---|---|
| Camera | 30 fps | 15 fps for general use |
| IMU | 50 Hz | 25 Hz for general use |
| Audio | continuous | 100 ms chunks |
| Location | as-needed | significant-change mode |
| Battery | as-needed | on state change only |

### Client-side throttling (Swift)

```swift
// Camera: use AVCaptureVideoDataOutput with videoMinFrameDuration
connection.videoMinFrameDuration = CMTime(value: 1, timescale: 15)  // 15 fps

// IMU: set updateInterval
motionManager.deviceMotionUpdateInterval = 1.0 / 25.0  // 25 Hz

// Audio: 100 ms tap buffer
engine.inputNode.installTap(onBus: 0, bufferSize: 1600, ...)  // 1600 samples @ 16kHz
```

### Network considerations

- Use local WiFi only; avoid cellular (latency + cost)
- Camera frames at 1080p JPEG ≈ 50-150 KB each; 15fps ≈ 1-2 MB/s
- If WiFi is congested, reduce camera resolution or fps
- iPhone USB Ethernet (via Lightning/USB-C adapter) eliminates WiFi contention

---

## Security Notes

- The HMAC secret must be pre-shared out-of-band (QR code, NFC, direct entry)
- Never transmit the secret over the network
- Rotate the secret if the iPhone is lost or companion app is uninstalled
- The receiver binds to `0.0.0.0`; restrict access via firewall if on untrusted networks
- For production, consider wrapping in TLS (self-signed cert + certificate pinning in app)

---

*This spec is implemented by `openclaw_embodiment/profiles/ios_companion.py`. Protocol version 1.0.*
