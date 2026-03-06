"""Device profile loader for OpenClaw Embodiment SDK."""

import os
from typing import Any, Dict, Optional, Tuple

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None  # type: ignore

from .ios_companion import (
    iOSCompanionProfile,
    iOSCompanionReceiver,
    iOSSensorPayload,
    PROFILE as IOS_COMPANION_PROFILE,
)

# Registry of Python-native profiles (not YAML-based)
_NATIVE_PROFILES: Dict[str, Any] = {
    "ios-companion": IOS_COMPANION_PROFILE,
}


def load_profile(name: Optional[str] = None) -> Any:
    """Load a device profile by name and return its config as a dict.

    Supports both YAML-based hardware profiles, Python-native profiles
    (such as 'ios-companion'), and auto-discovery (name=None or name="auto").

    Args:
        name: Profile name, e.g. 'reachy-mini', 'pi5-picam', 'ios-companion',
              'auto', or None (triggers hardware auto-discovery).

    Returns:
        Dict of profile configuration, or Tuple[str, dict] for auto-discovery.

    Raises:
        ValueError: If the profile name is unknown.
        NoDeviceFoundError: If name="auto" and no device is found.

    TODO: Wire HALs once device SDK packages are installed.
    """
    # Auto-discovery mode
    if name is None or name == "auto":
        from ..discovery.auto import auto_discover_profile
        return auto_discover_profile()

    # Check native Python profiles first
    if name in _NATIVE_PROFILES:
        profile = _NATIVE_PROFILES[name]
        if hasattr(profile, "as_dict"):
            return profile.as_dict()
        return {"name": name}

    filename = name.replace("-", "_") + ".yaml"
    profiles_dir = os.path.dirname(__file__)
    path = os.path.join(profiles_dir, filename)

    if not os.path.exists(path):
        yaml_profiles = "reachy-mini, reachy-mini-wireless, reachy2, pi5-picam, pi-zero2w, luxonis-oakd, frame-glasses, even-g2"
        native_profiles = ", ".join(sorted(_NATIVE_PROFILES.keys()))
        raise ValueError(
            f"Unknown profile: {name}. "
            f"YAML profiles: {yaml_profiles}. "
            f"Native profiles: {native_profiles}"
        )

    if yaml is None:
        raise ImportError("PyYAML is required for load_profile: pip install pyyaml")

    with open(path, "r") as f:
        return yaml.safe_load(f)


__all__ = [
    "load_profile",
    "iOSCompanionProfile",
    "iOSCompanionReceiver",
    "iOSSensorPayload",
    "IOS_COMPANION_PROFILE",
]
