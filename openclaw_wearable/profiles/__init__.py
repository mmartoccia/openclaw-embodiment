"""Device profile loader for OpenClaw Embodiment SDK."""

import os
from typing import Any, Dict

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore


def load_profile(name: str) -> Dict[str, Any]:
    """Load a device profile by name and return its config as a dict.

    Args:
        name: Profile name, e.g. 'reachy-mini' or 'pi5-picam'.

    Returns:
        Dict of profile configuration.

    Raises:
        ValueError: If the profile name is unknown.

    TODO: Wire HALs once device SDK packages are installed.
    """
    filename = name.replace("-", "_") + ".yaml"
    profiles_dir = os.path.dirname(__file__)
    path = os.path.join(profiles_dir, filename)

    if not os.path.exists(path):
        raise ValueError(
            f"Unknown profile: {name}. Available: reachy-mini, pi5-picam, "
            "pi-zero2w, luxonis-oakd, frame-glasses"
        )

    if yaml is None:
        raise ImportError("PyYAML is required for load_profile: pip install pyyaml")

    with open(path, "r") as f:
        return yaml.safe_load(f)


__all__ = ["load_profile"]
