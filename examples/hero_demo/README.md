# Hero Demo: Whiteboard Capture (Pi 3 + Mac Mini .183)

## Pi 3
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ../../.[pi3,dev]
openclaw-wearable init --platform pi3
openclaw-wearable check --config config.yaml
python whiteboard_capture.py
```

## Mac Mini .183 (receiver/context node)
Install this package and run your OpenClaw receiver service that consumes wearable packets.

Expected output:
- Trigger fires
- Packet transmitted
- Response card rendered
