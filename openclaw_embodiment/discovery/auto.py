"""Profile Auto-Discovery for OpenClaw Embodiment SDK.

Scans USB devices, BLE advertisements, and network service ports to identify
connected hardware and return the best matching device profile.

Usage::

    profile_name, config = auto_discover_profile()
    # or via the convenience alias:
    from openclaw_embodiment.profiles import load_profile
    name, cfg = load_profile("auto")
"""

from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class NoDeviceFoundError(Exception):
    """Raised when no known device is found during auto-discovery.

    Attributes:
        scan_results: Dictionary of scan results from each discovery method.
    """

    def __init__(self, message: str, scan_results: Optional[dict] = None) -> None:
        super().__init__(message)
        self.scan_results = scan_results or {}


# ---------------------------------------------------------------------------
# Profile Manifest
# ---------------------------------------------------------------------------


@dataclass
class DeviceSignature:
    """A device signature used for matching against scan results.

    Attributes:
        profile_name: Name of the matching profile.
        usb_ids: List of USB vendor:product strings (e.g. "0483:5740").
        ble_names: List of BLE advertisement name substrings to match.
        network_ports: List of (host, port) tuples to probe for service presence.
        suggested_config: Default config dict for this profile.
        confidence: Base confidence score (0.0-1.0) for a match.
    """

    profile_name: str
    usb_ids: List[str] = field(default_factory=list)
    ble_names: List[str] = field(default_factory=list)
    network_ports: List[Tuple[str, int]] = field(default_factory=list)
    suggested_config: dict = field(default_factory=dict)
    confidence: float = 1.0


class ProfileManifest:
    """Registry mapping device signatures to profile names.

    The manifest is pre-populated with known OpenClaw device signatures.
    Add custom entries via ``register()``.
    """

    DEFAULT_ENTRIES: List[DeviceSignature] = [
        DeviceSignature(
            profile_name="reachy2",
            usb_ids=["0483:5740"],  # Reachy USB serial
            ble_names=["reachy"],
            network_ports=[("reachy.local", 50051), ("192.168.1.100", 50051)],
            suggested_config={"host": "reachy.local", "port": 50051},
            confidence=1.0,
        ),
        DeviceSignature(
            profile_name="reachy-mini",
            ble_names=["reachy-mini", "ReachyMini"],
            network_ports=[("reachy-mini.local", 50051)],
            suggested_config={"host": "reachy-mini.local", "port": 50051},
            confidence=0.9,
        ),
        DeviceSignature(
            profile_name="reachy-mini-wireless",
            ble_names=["reachy-mini-w", "ReachyMiniWireless"],
            network_ports=[("reachy-mini-wireless.local", 50051)],
            suggested_config={"host": "reachy-mini-wireless.local", "port": 50051},
            confidence=0.9,
        ),
        DeviceSignature(
            profile_name="luxonis-oakd",
            usb_ids=["03e7:2485", "03e7:f63b"],  # OAK-D USB VIDs
            ble_names=["OAK-D"],
            network_ports=[("192.168.1.101", 8080)],
            suggested_config={"usb_vid": "03e7"},
            confidence=1.0,
        ),
        DeviceSignature(
            profile_name="even-g2",
            ble_names=["Even G2", "EvenG2", "G2"],
            suggested_config={"ble_address": "auto"},
            confidence=0.95,
        ),
        DeviceSignature(
            profile_name="frame-glasses",
            ble_names=["Frame", "BrilliantFrame"],
            suggested_config={"ble_address": "auto"},
            confidence=0.95,
        ),
        DeviceSignature(
            profile_name="pi5-picam",
            usb_ids=["2109:2817"],  # Raspberry Pi USB hub
            ble_names=["pi5", "picam"],
            network_ports=[("raspberrypi.local", 22)],
            suggested_config={"host": "raspberrypi.local"},
            confidence=0.7,
        ),
        DeviceSignature(
            profile_name="pi-zero2w",
            ble_names=["pizero", "pi-zero"],
            network_ports=[("pizero.local", 22)],
            suggested_config={"host": "pizero.local"},
            confidence=0.7,
        ),
        DeviceSignature(
            profile_name="meta-rayban",
            ble_names=["Ray-Ban", "RayBan", "MWDAT"],
            network_ports=[("localhost", 8421)],
            suggested_config={"mwdat": {"mock_mode": False}, "hal_server": {"http_port": 8421, "ws_port": 8422}},
            confidence=0.9,
        ),
        DeviceSignature(
            profile_name="unitree-go2",
            ble_names=["Go2", "Unitree"],
            network_ports=[("192.168.123.161", 8080)],
            suggested_config={"transport": {"host": "192.168.123.161", "port": 8080}},
            confidence=0.95,
        ),
        DeviceSignature(
            profile_name="apple-vision-pro",
            ble_names=["Vision Pro", "AVP"],
            network_ports=[("localhost", 8430)],
            suggested_config={"transport": {"host": "localhost", "port": 8430}},
            confidence=0.9,
        ),
        DeviceSignature(
            profile_name="openglass",
            ble_names=["OpenGlass"],
            suggested_config={"transport": {"advertised_name": "OpenGlass"}},
            confidence=0.95,
        ),
    ]

    def __init__(self) -> None:
        self._entries: List[DeviceSignature] = list(self.DEFAULT_ENTRIES)

    def register(self, signature: DeviceSignature) -> None:
        """Register a custom device signature.

        Args:
            signature: DeviceSignature to add to the manifest.
        """
        self._entries.append(signature)

    def all_entries(self) -> List[DeviceSignature]:
        """Return all registered device signatures."""
        return list(self._entries)


# ---------------------------------------------------------------------------
# Discovery methods
# ---------------------------------------------------------------------------


def _scan_usb(timeout_s: float = 3.0) -> List[str]:
    """Return list of 'vid:pid' strings for connected USB devices.

    Uses 'lsusb' on Linux or 'system_profiler' on macOS.

    Args:
        timeout_s: Maximum time to wait for the scan command.

    Returns:
        List of lowercase 'vid:pid' strings.
    """
    ids: List[str] = []
    try:
        result = subprocess.run(
            ["lsusb"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        for line in result.stdout.splitlines():
            # Format: 'Bus NNN Device NNN: ID vid:pid Description'
            parts = line.split()
            for part in parts:
                if ":" in part and len(part) == 9:
                    ids.append(part.lower())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Try macOS system_profiler
        try:
            result = subprocess.run(
                ["system_profiler", "SPUSBDataType"],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            # Parse 'Product ID: 0x2485' and 'Vendor ID: 0x03e7'
            vid = None
            pid = None
            for line in result.stdout.splitlines():
                stripped = line.strip().lower()
                if "vendor id:" in stripped:
                    try:
                        vid = stripped.split("0x")[1][:4]
                    except IndexError:
                        vid = None
                elif "product id:" in stripped:
                    try:
                        pid = stripped.split("0x")[1][:4]
                    except IndexError:
                        pid = None
                if vid and pid:
                    ids.append(f"{vid}:{pid}")
                    vid = None
                    pid = None
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):  # grain: ignore NAKED_EXCEPT -- macOS system_profiler may fail with any error
            pass
    except Exception as exc:  # grain: ignore NAKED_EXCEPT -- USB scan uses platform CLI; errors are unpredictable
        logger.debug("USB scan error: %s", exc)
    return ids


def _scan_ble(timeout_s: float = 5.0) -> List[str]:
    """Return list of BLE advertisement names discovered nearby.

    Attempts to use the ``bleak`` library for cross-platform BLE scanning.

    Args:
        timeout_s: Duration of the BLE scan.

    Returns:
        List of device names (may contain None entries from nameless devices, filtered out).
    """
    names: List[str] = []
    try:
        import asyncio

        import bleak  # type: ignore

        async def _scan() -> List[str]:
            devices = await bleak.BleakScanner.discover(timeout=timeout_s)
            return [d.name for d in devices if d.name]

        loop = asyncio.new_event_loop()
        try:
            names = loop.run_until_complete(_scan())
        finally:
            loop.close()
    except ImportError:
        logger.debug("bleak not installed -- BLE scan skipped")
    except Exception as exc:  # grain: ignore NAKED_EXCEPT -- BLE scanner may raise with any driver/platform error
        logger.debug("BLE scan error: %s", exc)
    return names


def _probe_port(host: str, port: int, timeout_s: float = 1.0) -> bool:
    """Return True if a TCP connection to host:port succeeds within timeout.

    Args:
        host: Hostname or IP address.
        port: TCP port number.
        timeout_s: Connection timeout in seconds.

    Returns:
        True if the port is open, False otherwise.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except (OSError, socket.timeout, ConnectionRefusedError):
        return False


def _scan_network(
    signatures: List[DeviceSignature],
    timeout_s: float = 2.0,
) -> Dict[str, bool]:
    """Probe all network ports in the manifest in parallel.

    Args:
        signatures: List of device signatures to probe.
        timeout_s: Per-connection timeout in seconds.

    Returns:
        Dict mapping 'host:port' -> bool for each probed endpoint.
    """
    results: Dict[str, bool] = {}
    threads: List[threading.Thread] = []
    lock = threading.Lock()

    def probe(host: str, port: int) -> None:
        key = f"{host}:{port}"
        open_ = _probe_port(host, port, timeout_s)
        with lock:
            results[key] = open_

    for sig in signatures:
        for host, port in sig.network_ports:
            t = threading.Thread(target=probe, args=(host, port), daemon=True)
            threads.append(t)
            t.start()

    for t in threads:
        t.join(timeout=timeout_s + 0.5)

    return results


# ---------------------------------------------------------------------------
# Match scoring
# ---------------------------------------------------------------------------


@dataclass
class _Match:
    signature: DeviceSignature
    score: float
    matched_via: List[str]  # e.g. ["usb", "ble", "network"]


def _score_signatures(
    signatures: List[DeviceSignature],
    usb_ids: List[str],
    ble_names: List[str],
    network_results: Dict[str, bool],
) -> List[_Match]:
    """Score each signature against scan results.

    Args:
        signatures: All manifest entries.
        usb_ids: Discovered USB 'vid:pid' strings.
        ble_names: Discovered BLE advertisement names.
        network_results: Map of 'host:port' -> bool for probed ports.

    Returns:
        List of _Match objects sorted by descending score.
    """
    matches: List[_Match] = []

    for sig in signatures:
        score = 0.0
        matched_via: List[str] = []

        # USB match -- high confidence (1.0 * base)
        for uid in sig.usb_ids:
            if uid.lower() in usb_ids:
                score += sig.confidence * 1.0
                matched_via.append("usb")
                break

        # BLE match -- medium confidence (0.8 * base)
        for bn in sig.ble_names:
            for discovered in ble_names:
                if bn.lower() in discovered.lower():
                    score += sig.confidence * 0.8
                    matched_via.append("ble")
                    break
            else:
                continue
            break

        # Network match -- medium confidence (0.7 * base)
        for host, port in sig.network_ports:
            key = f"{host}:{port}"
            if network_results.get(key, False):
                score += sig.confidence * 0.7
                matched_via.append("network")
                break

        if score > 0.0:
            matches.append(_Match(signature=sig, score=score, matched_via=matched_via))

    matches.sort(key=lambda m: m.score, reverse=True)
    return matches


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def auto_discover_profile(
    timeout_s: float = 10.0,
    manifest: Optional[ProfileManifest] = None,
) -> Tuple[str, dict]:
    """Discover connected hardware and return the best matching profile.

    Scans USB devices, BLE advertisements, and network service ports in
    parallel against the ProfileManifest. Returns the highest-confidence
    match.

    Args:
        timeout_s: Total time budget for all scans (default 10s).
        manifest: Custom ProfileManifest. Uses the default manifest if None.

    Returns:
        Tuple of (profile_name, suggested_config).
        The suggested_config dict is suitable for passing to load_profile().

    Raises:
        NoDeviceFoundError: If no known device is found within the timeout.
            The exception carries scan_results for debugging.

    Example::

        name, config = auto_discover_profile()
        # Returns: ("even-g2", {"ble_address": "auto"})
    """
    if manifest is None:
        manifest = ProfileManifest()

    signatures = manifest.all_entries()

    # Allocate time budget: USB + BLE in parallel, network separately
    ble_budget = min(timeout_s * 0.5, 5.0)
    net_budget = min(timeout_s * 0.2, 2.0)

    scan_results: dict = {}

    # Run USB and network scans in threads; BLE scan in main thread (async)
    usb_result: List[List[str]] = [[]]
    net_result: List[Dict[str, bool]] = [{}]

    def do_usb() -> None:
        usb_result[0] = _scan_usb(timeout_s=3.0)

    def do_network() -> None:
        net_result[0] = _scan_network(signatures, timeout_s=net_budget)

    usb_thread = threading.Thread(target=do_usb, daemon=True)
    net_thread = threading.Thread(target=do_network, daemon=True)
    usb_thread.start()
    net_thread.start()

    # BLE scan (main thread -- uses asyncio internally)
    ble_names = _scan_ble(timeout_s=ble_budget)

    usb_thread.join(timeout=4.0)
    net_thread.join(timeout=net_budget + 1.0)

    usb_ids = usb_result[0]
    network_results = net_result[0]

    scan_results = {
        "usb_ids": usb_ids,
        "ble_names": ble_names,
        "network": network_results,
    }

    logger.debug("Auto-discovery scan results: %s", scan_results)

    matches = _score_signatures(signatures, usb_ids, ble_names, network_results)

    if not matches:
        raise NoDeviceFoundError(
            "No known device found. Connect a device and retry.",
            scan_results=scan_results,
        )

    best = matches[0]
    config = dict(best.signature.suggested_config)

    # Attach alternatives in metadata
    if len(matches) > 1:
        config["_alternatives"] = [
            {"profile": m.signature.profile_name, "score": m.score, "via": m.matched_via}
            for m in matches[1:]
        ]

    config["_matched_via"] = best.matched_via
    config["_score"] = best.score

    logger.info(
        "Auto-discovered profile '%s' (score=%.2f, via=%s)",
        best.signature.profile_name,
        best.score,
        best.matched_via,
    )

    return best.signature.profile_name, config


__all__ = [
    "auto_discover_profile",
    "ProfileManifest",
    "DeviceSignature",
    "NoDeviceFoundError",
]
