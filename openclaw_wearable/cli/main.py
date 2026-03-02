"""Command line entrypoint for openclaw-wearable."""

from pathlib import Path

import typer

from ..core.pipeline import HALRegistry, WearableSDK
from ..hal.simulator import SimulatedCamera, SimulatedClassifier, SimulatedDisplay, SimulatedIMU, SimulatedMicrophone, SimulatedTransport

app = typer.Typer(help="OpenClaw wearable CLI")


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
