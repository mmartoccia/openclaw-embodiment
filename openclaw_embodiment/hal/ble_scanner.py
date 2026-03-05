"""BLE ProximityScanner -- scans for nearby BLE devices via bleak.

Designed to run on the Distiller CM5 (Raspberry Pi CM5 with built-in BLE radio).
Uses async bleak scanning and is invoked synchronously from the ContextBuilder.

Can be run standalone:
    python -m openclaw_embodiment.hal.ble_scanner [--duration 5] [--known mac=name ...]
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProximityContext:
    """BLE proximity snapshot.

    Attributes:
        known_devices:  Names of devices in ``known_map`` that were detected.
        unknown_count:  Number of detected BLE devices NOT in ``known_map``.
        rssi_map:       Maps device address (lowercase) -> RSSI (dBm, negative).
        scan_duration_s: How long the scan ran.
        confidence:     0.0–1.0. Based on scan duration and device count.
        timestamp_ms:   Unix ms when scan completed.
    """
    known_devices: List[str] = field(default_factory=list)
    unknown_count: int = 0
    rssi_map: Dict[str, int] = field(default_factory=dict)
    scan_duration_s: float = 3.0
    confidence: float = 0.0
    timestamp_ms: int = 0


class BLEProximityScanner:
    """Scans for nearby BLE devices and classifies them as known or unknown.

    Args:
        known_map:      Dict mapping MAC address (lowercase) to human name.
                        e.g. {"aa:bb:cc:dd:ee:ff": "Mike's Pendant"}
        scan_duration_s: How long to scan (seconds). Longer = more complete.

    Usage::

        scanner = BLEProximityScanner(known_map={"aa:bb:cc:dd:ee:ff": "Pendant"})
        ctx = scanner.scan()
        print(ctx.known_devices, ctx.unknown_count, ctx.rssi_map)
    """

    def __init__(
        self,
        known_map: Optional[Dict[str, str]] = None,
        scan_duration_s: float = 3.0,
    ) -> None:
        self.known_map: Dict[str, str] = {
            k.lower(): v for k, v in (known_map or {}).items()
        }
        self.scan_duration_s = scan_duration_s

    def scan(self) -> ProximityContext:
        """Synchronous BLE scan. Runs the async scan and returns results.

        Returns:
            ProximityContext with whatever was discovered.
            On bleak import failure (e.g., not in venv), returns empty context
            with confidence=0.0 and logs a warning.
        """
        try:
            return asyncio.run(self._async_scan())
        except RuntimeError:
            # Already inside an event loop (e.g., Jupyter) -- use new loop
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._async_scan())
            finally:
                loop.close()
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- BLE scan -- adapter errors vary by OS and driver
            logger.warning("[BLEScanner] Scan failed: %s", e)
            return ProximityContext(
                timestamp_ms=int(time.time() * 1000),
                confidence=0.0,
            )

    async def _async_scan(self) -> ProximityContext:
        """Async BLE scan using bleak."""
        try:
            from bleak import BleakScanner  # type: ignore
        except ImportError:
            logger.warning(
                "[BLEScanner] bleak not available. "
                "Install in distiller-sdk venv: pip install bleak"
            )
            return ProximityContext(
                timestamp_ms=int(time.time() * 1000),
                confidence=0.0,
            )

        t0 = time.time()
        try:
            # bleak 2.x: discover(return_adv=True) returns dict {addr: (BLEDevice, AdvertisementData)}
            # bleak <2.x: discover() returns list[BLEDevice] with .rssi attribute
            try:
                discovery = await BleakScanner.discover(
                    timeout=self.scan_duration_s, return_adv=True
                )
                # bleak 2.x path: dict {addr: (BLEDevice, AdvertisementData)}
                raw_devices = []
                for addr, (dev, adv) in discovery.items():
                    rssi = getattr(adv, "rssi", None) or getattr(dev, "rssi", -100) or -100
                    raw_devices.append((dev, rssi))
            except TypeError:
                # return_adv not supported in this bleak version
                old_devices = await BleakScanner.discover(timeout=self.scan_duration_s)
                raw_devices = [(d, getattr(d, "rssi", -100) or -100) for d in old_devices]
        except Exception as e:  # grain: ignore NAKED_EXCEPT -- cosmetic output -- best-effort, never crash on display failure
            logger.warning("[BLEScanner] BleakScanner.discover() error: %s", e)
            return ProximityContext(
                timestamp_ms=int(time.time() * 1000),
                confidence=0.0,
            )

        elapsed = time.time() - t0
        rssi_map: Dict[str, int] = {}
        known_devices: List[str] = []
        unknown_count = 0

        for dev, rssi in raw_devices:
            addr = dev.address.lower()
            rssi_map[addr] = int(rssi)

            if addr in self.known_map:
                name = self.known_map[addr]
                known_devices.append(name)
                logger.debug("[BLEScanner] Known device: %s (%s) rssi=%d", name, addr, rssi)
            else:
                unknown_count += 1
                logger.debug("[BLEScanner] Unknown device: %s rssi=%d", addr, rssi)

        # Confidence: scales with scan duration and non-zero results
        # Full scan = 0.85; any devices found adds up to 0.15 bonus
        n_devices = len(raw_devices)
        duration_factor = min(elapsed / max(self.scan_duration_s, 1.0), 1.0) * 0.85
        device_factor = min(n_devices / 10.0, 0.15) if n_devices else 0.0
        confidence = round(duration_factor + device_factor, 3)

        ctx = ProximityContext(
            known_devices=known_devices,
            unknown_count=unknown_count,
            rssi_map=rssi_map,
            scan_duration_s=elapsed,
            confidence=confidence,
            timestamp_ms=int(time.time() * 1000),
        )
        logger.info(
            "[BLEScanner] Scan complete: %d known, %d unknown, %d total, conf=%.2f (%.1fs)",
            len(known_devices), unknown_count, n_devices, confidence, elapsed,
        )
        return ctx


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="BLE ProximityScanner smoke test")
    parser.add_argument("--duration", type=float, default=5.0, help="Scan duration (s)")
    parser.add_argument(
        "--known", nargs="*", default=[],
        metavar="MAC=NAME",
        help="Known device mappings, e.g. aa:bb:cc:dd:ee:ff=MyPendant"
    )
    args = parser.parse_args()

    known_map = {}
    for entry in (args.known or []):
        if "=" in entry:
            mac, name = entry.split("=", 1)
            known_map[mac.strip()] = name.strip()

    scanner = BLEProximityScanner(known_map=known_map, scan_duration_s=args.duration)
    print(f"Scanning for {args.duration}s...")
    ctx = scanner.scan()
    print(json.dumps({
        "known_devices": ctx.known_devices,
        "unknown_count": ctx.unknown_count,
        "rssi_map": ctx.rssi_map,
        "confidence": ctx.confidence,
        "scan_duration_s": ctx.scan_duration_s,
    }, indent=2))
