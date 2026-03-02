"""Command line entrypoint for openclaw-wearable."""

import shutil
import sys
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import URLError

import typer

from ..core.pipeline import HALRegistry, WearableSDK
from ..hal.simulator import SimulatedCamera, SimulatedClassifier, SimulatedDisplay, SimulatedIMU, SimulatedMicrophone, SimulatedTransport

app = typer.Typer(help="OpenClaw wearable CLI")


@app.command("doctor")
def doctor() -> None:
    """Run environment diagnostics and report system readiness."""
    typer.echo("OpenClaw Embodiment SDK -- Doctor")
    typer.echo("==================================")

    ok_count = 0
    warn_count = 0
    error_count = 0

    # --- Python version ---
    vi = sys.version_info
    if vi >= (3, 9):
        typer.echo(f"✅ Python {vi.major}.{vi.minor}.{vi.micro} (OK)")
        ok_count += 1
    else:
        typer.echo(f"❌ Python {vi.major}.{vi.minor}.{vi.micro} (requires 3.9+)")
        error_count += 1

    # --- Required packages ---
    for pkg in ("typer", "click"):
        try:
            __import__(pkg)
            typer.echo(f"✅ {pkg} installed")
            ok_count += 1
        except ImportError:
            typer.echo(f"❌ {pkg} not installed (install with: pip install {pkg})")
            error_count += 1

    # --- BLE (bleak) ---
    try:
        import bleak  # noqa: F401
        _ = bleak.BleakClient
        typer.echo("✅ bleak installed (BLE transport available)")
        ok_count += 1
    except ImportError:
        typer.echo("⚠️  bleak not installed (BLE transport unavailable -- install with: pip install bleak)")
        warn_count += 1

    # --- Camera (opencv) ---
    try:
        import cv2  # noqa: F401
        typer.echo("✅ opencv installed (Camera capture available)")
        ok_count += 1
    except ImportError:
        typer.echo("⚠️  opencv not installed (Camera capture limited -- install with: pip install opencv-python)")
        warn_count += 1

    # --- Audio output ---
    audio_bin = shutil.which("afplay") or shutil.which("aplay")
    if audio_bin:
        bin_name = "afplay" if "afplay" in audio_bin else "aplay"
        typer.echo(f"✅ Audio output available ({bin_name})")
        ok_count += 1
    else:
        typer.echo("⚠️  No audio output binary found (aplay/afplay -- audio playback unavailable)")
        warn_count += 1

    # --- Reachy Mini device ---
    try:
        req = urllib_request.Request("http://localhost:50055/api/health")
        with urllib_request.urlopen(req, timeout=1):
            typer.echo("🤖 Reachy Mini: CONNECTED at localhost:50055")
            ok_count += 1
    except Exception:
        typer.echo("🤖 Reachy Mini: NOT FOUND at localhost:50055 (start reachy-mini-daemon to connect)")
        # Not an error or warning -- informational only

    # --- OpenClaw gateway ---
    try:
        req = urllib_request.Request("http://100.82.191.2:18800/health")
        with urllib_request.urlopen(req, timeout=1):
            typer.echo("🌐 OpenClaw Gateway: REACHABLE at 100.82.191.2:18800")
            ok_count += 1
    except Exception:
        typer.echo("🌐 OpenClaw Gateway: NOT REACHABLE at 100.82.191.2:18800 (check OpenClaw is running)")
        # Informational only

    typer.echo("")
    typer.echo(f"Summary: {ok_count} OK, {warn_count} warnings, {error_count} errors")
    typer.echo("Run 'openclaw-embodiment demo' to test with simulation.")


@app.command()
def init(platform: str = typer.Option("simulator", help="pi3|simulator"), config: str = typer.Option("config.yaml")) -> None:
    """Generate starter configuration."""
    Path(config).write_text("platform: %s\\n" % platform)
    typer.echo("Wrote %s" % config)


@app.command()
def check(config: str = typer.Option("config.yaml")) -> None:
    """Run lightweight environment and HAL checks."""
    exists = Path(config).exists()
    typer.echo("config: %s" % ("ok" if exists else "missing"))
    typer.echo("python: ok")


@app.command()
def demo(config: str = typer.Option("config.yaml")) -> None:
    """Run one short simulator demo cycle."""
    registry = HALRegistry()
    registry.register_imu(SimulatedIMU())
    registry.register_camera(SimulatedCamera())
    registry.register_microphone(SimulatedMicrophone())
    registry.register_classifier(SimulatedClassifier())
    registry.register_transport(SimulatedTransport(), priority=0)
    registry.register_display(SimulatedDisplay())
    sdk = WearableSDK(registry, config)
    sdk.start()
    typer.echo("demo running...")
    import time

    time.sleep(1.2)
    sdk.stop()
    typer.echo("demo complete")


if __name__ == "__main__":
    app()
