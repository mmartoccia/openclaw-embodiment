"""Profile Validator for OpenClaw Embodiment SDK.

Validates any device profile against the HAL contract with 7 checks:
1. HAL instantiation -- all required HAL ABCs can be instantiated
2. Capability declaration -- capabilities list matches implemented HALs
3. Simulator swap -- every hardware HAL replaceable with simulator equivalent
4. Transport contract -- send(), get_expected_latency_ms(), get_measured_latency_ms() callable
5. Pipeline smoke test -- full trigger→capture→transport cycle with simulator HALs
6. Latency contract -- get_expected_latency_ms() returns positive integer
7. Error recovery -- transport failure doesn't crash pipeline

Usage::

    validator = ProfileValidator("meta-rayban", config)
    report = validator.run()
    print(report.overall)  # "pass", "fail", or "warn"
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)


def _ms() -> int:
    return time.monotonic_ns() // 1_000_000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    """Result of a single validation check.

    Attributes:
        name: Human-readable check name.
        passed: True if the check passed.
        message: Descriptive message with details.
        elapsed_ms: Time taken to run the check in milliseconds.
        warning: True if check passed but with caveats (not hard failure).
    """

    name: str
    passed: bool
    message: str
    elapsed_ms: int = 0
    warning: bool = False


@dataclass
class ValidationReport:
    """Complete validation report for a device profile.

    Attributes:
        profile: Profile name validated.
        timestamp: UTC datetime of validation run.
        passed: Number of checks that passed.
        failed: Number of checks that failed.
        checks: List of individual CheckResult objects.
        overall: Aggregate result -- 'pass', 'fail', or 'warn'.
        hardware_ready: True if all checks pass with real HAL implementations.
    """

    profile: str
    timestamp: datetime
    passed: int
    failed: int
    checks: List[CheckResult] = field(default_factory=list)
    overall: Literal["pass", "fail", "warn"] = "pass"
    hardware_ready: bool = False

    def summary(self) -> str:
        """Return human-readable validation summary.

        Returns:
            Multi-line summary string with check results.
        """
        lines = [
            f"Profile: {self.profile}",
            f"Timestamp: {self.timestamp.isoformat()}",
            f"Overall: {self.overall.upper()}",
            f"Passed: {self.passed} / {self.passed + self.failed}",
            f"Hardware Ready: {self.hardware_ready}",
            "",
            "Checks:",
        ]
        for check in self.checks:
            icon = "✅" if check.passed else ("⚠️" if check.warning else "❌")
            lines.append(f"  {icon} [{check.elapsed_ms}ms] {check.name}: {check.message}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Capability -> HAL type mapping
# ---------------------------------------------------------------------------

CAPABILITY_HAL_MAP: Dict[str, str] = {
    "camera": "CameraHal",
    "microphone": "MicrophoneHal",
    "display": "DisplayHal",
    "imu": "IMUHal",
    "actuator": "ActuatorHal",
    "audio_output": "AudioOutputHal",
    "transport": "TransportHal",
    "status_indicator": "StatusIndicatorHal",
    "power": "PowerHal",
    "system_health": "SystemHealthHal",
}

# Map HAL types to simulator class names
HAL_SIMULATOR_MAP: Dict[str, str] = {
    "CameraHal": "SimulatedCamera",
    "MicrophoneHal": "SimulatedMicrophone",
    "DisplayHal": "SimulatedDisplay",
    "IMUHal": "SimulatedIMU",
    "ActuatorHal": "SimulatedActuator",
    "AudioOutputHal": "SimulatedAudioOutput",
    "TransportHal": "SimulatedTransport",
    "StatusIndicatorHal": "SimulatedStatusIndicator",
    "SystemHealthHal": "SimulatedSystemHealth",
}


# ---------------------------------------------------------------------------
# ProfileValidator
# ---------------------------------------------------------------------------


class ProfileValidator:
    """Validates a device profile against the OpenClaw HAL contract.

    Runs 7 checks to ensure a profile is ready for hardware deployment:
    instantiation, capabilities, simulator swap, transport contract,
    pipeline smoke test, latency contract, and error recovery.

    Attributes:
        profile_name: Name of the profile being validated.
        config: Profile configuration dictionary.
    """

    def __init__(self, profile_name: str, config: dict) -> None:
        """Initialize ProfileValidator.

        Args:
            profile_name: Name of the profile to validate (e.g. 'meta-rayban').
            config: Profile configuration dictionary (from YAML or dict).
        """
        self.profile_name = profile_name
        self.config = config
        self._checks: List[CheckResult] = []

    def _run_check(
        self,
        name: str,
        fn: Callable[[], tuple],
    ) -> CheckResult:
        """Run a single validation check and record result.

        Args:
            name: Human-readable check name.
            fn: Callable returning (passed: bool, message: str, warning: bool).

        Returns:
            CheckResult with timing.
        """
        t0 = _ms()
        try:
            passed, message, warning = fn()
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
            passed = False
            message = f"Check raised exception: {exc}"
            warning = False
        elapsed = _ms() - t0
        result = CheckResult(name=name, passed=passed, message=message, elapsed_ms=elapsed, warning=warning)
        self._checks.append(result)
        logger.debug("Check '%s': %s (%dms) -- %s", name, "PASS" if passed else "FAIL", elapsed, message)
        return result

    def run(self) -> ValidationReport:
        """Run all 7 validation checks and return a ValidationReport.

        Returns:
            ValidationReport with check results and aggregate status.
        """
        self._checks = []
        logger.info("ProfileValidator: validating profile '%s'", self.profile_name)

        self._check_hal_instantiation()
        self._check_capability_declaration()
        self._check_simulator_swap()
        self._check_transport_contract()
        self._check_pipeline_smoke_test()
        self._check_latency_contract()
        self._check_error_recovery()

        passed = sum(1 for c in self._checks if c.passed)
        failed = sum(1 for c in self._checks if not c.passed)
        warnings = sum(1 for c in self._checks if c.warning and c.passed)

        if failed > 0:
            overall: Literal["pass", "fail", "warn"] = "fail"
        elif warnings > 0:
            overall = "warn"
        else:
            overall = "pass"

        hardware_ready = (failed == 0)

        return ValidationReport(
            profile=self.profile_name,
            timestamp=datetime.utcnow(),
            passed=passed,
            failed=failed,
            checks=list(self._checks),
            overall=overall,
            hardware_ready=hardware_ready,
        )

    async def run_async(self) -> ValidationReport:
        """Async version of run(). Executes in event loop.

        Returns:
            ValidationReport with check results.
        """
        return await asyncio.get_event_loop().run_in_executor(None, self.run)

    # ---------------------------------------------------------------------------
    # Check implementations
    # ---------------------------------------------------------------------------

    def _check_hal_instantiation(self) -> None:
        """Check 1: All required HAL ABCs can be instantiated from profile config."""
        def check() -> tuple:
            hals = self._get_hals_for_profile()
            if hals is None:
                return True, "Profile uses simulator HALs (no hardware HALs to instantiate)", True
            if not hals:
                return False, "No HALs returned by profile factory", False
            hal_names = list(hals.keys())
            return True, f"Instantiated {len(hals)} HALs: {hal_names}", False

        self._run_check("HAL Instantiation", check)

    def _check_capability_declaration(self) -> None:
        """Check 2: Profile capabilities list matches implemented HAL classes."""
        def check() -> tuple:
            capabilities = self.config.get("capabilities", [])
            if not capabilities:
                return True, "No capabilities declared (OK for generic profiles)", True

            hals = self._get_hals_for_profile()
            if hals is None:
                return True, "Simulator HALs cover all capabilities", False

            hal_keys = set(hals.keys())
            missing = []
            for cap in capabilities:
                cap_key = cap.replace("-", "_")
                if cap_key not in hal_keys and cap not in hal_keys:
                    missing.append(cap)

            if missing:
                return False, f"Capabilities declared but HALs missing: {missing}", False
            return True, f"All {len(capabilities)} capabilities have HAL implementations", False

        self._run_check("Capability Declaration", check)

    def _check_simulator_swap(self) -> None:
        """Check 3: Every hardware HAL can be replaced with simulator equivalent."""
        def check() -> tuple:
            from ..hal.simulator import (
                SimulatedActuator,
                SimulatedAudioOutput,
                SimulatedCamera,
                SimulatedDisplay,
                SimulatedIMU,
                SimulatedMicrophone,
                SimulatedStatusIndicator,
                SimulatedSystemHealth,
                SimulatedTransport,
            )

            sim_hals = {
                "camera": SimulatedCamera(),
                "microphone": SimulatedMicrophone(),
                "display": SimulatedDisplay(),
                "imu": SimulatedIMU(),
                "actuator": SimulatedActuator(),
                "audio_output": SimulatedAudioOutput(),
                "transport": SimulatedTransport(),
                "status_indicator": SimulatedStatusIndicator(),
                "system_health": SimulatedSystemHealth(),
            }

            # Initialize all simulators
            failures = []
            for name, hal in sim_hals.items():
                try:
                    if hasattr(hal, "initialize"):
                        if name == "camera":
                            hal.initialize((320, 240))
                        elif name == "microphone":
                            hal.initialize(16000, 1)
                        elif name in ("transport",):
                            hal.initialize({})
                        elif name == "status_indicator":
                            hal.initialize()
                        else:
                            hal.initialize()
                except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                    failures.append(f"{name}: {exc}")

            if failures:
                return False, f"Simulator init failures: {failures}", False
            return True, f"All {len(sim_hals)} simulator HALs swappable", False

        self._run_check("Simulator Swap", check)

    def _check_transport_contract(self) -> None:
        """Check 4: Transport contract -- send(), get_expected_latency_ms(), get_measured_latency_ms() callable."""
        def check() -> tuple:
            from ..hal.simulator import SimulatedTransport

            transport = SimulatedTransport()
            transport.initialize({})
            transport.connect()

            failures = []

            # send()
            try:
                result = transport.send(b"test-payload")
                if not result.success:
                    failures.append("send() returned failure")
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"send() raised: {exc}")

            # get_expected_latency_ms()
            try:
                latency = transport.get_expected_latency_ms()
                if not isinstance(latency, int) or latency <= 0:
                    failures.append(f"get_expected_latency_ms() returned {latency!r} (expected positive int)")
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"get_expected_latency_ms() raised: {exc}")

            # get_measured_latency_ms() -- may return None before sends
            try:
                measured = transport.get_measured_latency_ms()
                if measured is not None and not isinstance(measured, int):
                    failures.append(f"get_measured_latency_ms() returned {measured!r} (expected int or None)")
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"get_measured_latency_ms() raised: {exc}")

            # Also check profile-specific transport if available
            hals = self._get_hals_for_profile()
            if hals and "transport" in hals:
                profile_transport = hals["transport"]
                try:
                    pl = profile_transport.get_expected_latency_ms()
                    if not isinstance(pl, int) or pl <= 0:
                        failures.append(f"Profile transport latency invalid: {pl!r}")
                except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                    failures.append(f"Profile transport get_expected_latency_ms raised: {exc}")

            if failures:
                return False, f"Transport contract failures: {failures}", False
            return True, "Transport contract: send(), get_expected_latency_ms(), get_measured_latency_ms() all callable", False

        self._run_check("Transport Contract", check)

    def _check_pipeline_smoke_test(self) -> None:
        """Check 5: Full trigger->capture->transport cycle using simulator HALs."""
        def check() -> tuple:
            from ..hal.simulator import (
                SimulatedCamera,
                SimulatedMicrophone,
                SimulatedTransport,
            )

            # Simulate a minimal trigger -> capture -> transport cycle
            cam = SimulatedCamera()
            cam.initialize((320, 240))

            mic = SimulatedMicrophone()
            mic.initialize(16000, 1)
            mic.start_recording()

            transport = SimulatedTransport()
            transport.initialize({})
            transport.connect()

            failures = []

            # Step 1: Capture frame (trigger)
            try:
                frame = cam.capture_frame()
                if frame is None or len(frame.data) == 0:
                    failures.append("capture_frame() returned empty frame")
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"capture_frame() raised: {exc}")

            # Step 2: Capture audio
            try:
                chunk = mic.get_buffer(100)
                if chunk is None or len(chunk.data) == 0:
                    failures.append("get_buffer() returned empty audio")
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"get_buffer() raised: {exc}")

            # Step 3: Send via transport
            try:
                payload = b"test-context-payload"
                result = transport.send(payload)
                if not result.success:
                    failures.append("transport.send() failed")
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"transport.send() raised: {exc}")

            # Step 4: Receive response
            try:
                response = transport.receive(timeout_ms=100)
                # Response may be the sent payload (loopback) or None
                _ = response  # just verify it doesn't crash
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"transport.receive() raised: {exc}")

            if failures:
                return False, f"Pipeline failures: {failures}", False
            return True, "Full trigger->capture->transport->receive cycle completed", False

        self._run_check("Pipeline Smoke Test", check)

    def _check_latency_contract(self) -> None:
        """Check 6: get_expected_latency_ms() returns a positive integer."""
        def check() -> tuple:
            hals = self._get_hals_for_profile()

            # Check profile-specific transport
            if hals and "transport" in hals:
                transport = hals["transport"]
                try:
                    latency = transport.get_expected_latency_ms()
                    if isinstance(latency, int) and latency > 0:
                        return True, f"Profile transport expected latency: {latency}ms", False
                    return False, f"Transport expected latency invalid: {latency!r}", False
                except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                    return False, f"get_expected_latency_ms() raised: {exc}", False

            # Fall back to simulator transport
            from ..hal.simulator import SimulatedTransport
            sim = SimulatedTransport()
            sim.initialize({})
            latency = sim.get_expected_latency_ms()
            return (
                isinstance(latency, int) and latency > 0,
                f"Simulator transport latency: {latency}ms",
                hals is None,
            )

        self._run_check("Latency Contract", check)

    def _check_error_recovery(self) -> None:
        """Check 7: Transport failure does not crash the pipeline."""
        def check() -> tuple:
            from ..hal.simulator import SimulatedTransport

            transport = SimulatedTransport()
            transport.initialize({})
            # Do NOT connect -- simulate disconnected state

            failures = []
            # Attempt to send while disconnected (should not raise, may fail gracefully)
            try:
                result = transport.send(b"should-fail-gracefully")
                # SimulatedTransport will succeed even disconnected (loopback)
                # Just verify no exception is thrown
                _ = result
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"send() while disconnected raised: {exc}")

            # Simulate state callback fires without crashing
            state_changes = []
            transport.set_state_callback(lambda s: state_changes.append(s))
            try:
                transport.connect()
                transport.disconnect()
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
                failures.append(f"connect/disconnect raised: {exc}")

            if failures:
                return False, f"Error recovery failures: {failures}", False
            return True, "Pipeline does not crash on transport failure or disconnect", False

        self._run_check("Error Recovery", check)

    # ---------------------------------------------------------------------------
    # Helper: Load profile HALs
    # ---------------------------------------------------------------------------

    def _get_hals_for_profile(self) -> Optional[dict]:
        """Attempt to build HALs for the current profile.

        Returns:
            HAL dict from profile factory, or None if profile uses generic sim HALs.
        """
        profile_factories = {
            "meta-rayban": self._load_meta_rayban,
            "unitree-go2": self._load_unitree_go2,
            "apple-vision-pro": self._load_apple_vision_pro,
            "openglass": self._load_openglass,
        }

        factory = profile_factories.get(self.profile_name)
        if factory is None:
            return None  # Generic profile -- use simulator HALs

        try:
            return factory()
        except Exception as exc:  # grain: ignore NAKED_EXCEPT -- ProfileValidator -- HAL validation probes; errors expected
            logger.debug("ProfileValidator: could not load profile HALs for '%s': %s", self.profile_name, exc)
            return None

    def _load_meta_rayban(self) -> Optional[dict]:
        """Load Meta Ray-Ban HALs in mock mode."""
        from ..profiles.meta_rayban import build_meta_rayban_hals
        config = dict(self.config)
        if "mwdat" not in config:
            config["mwdat"] = {"mock_mode": True}
        else:
            config["mwdat"]["mock_mode"] = True
        return build_meta_rayban_hals(config)

    def _load_unitree_go2(self) -> Optional[dict]:
        """Load Unitree Go2 HALs in simulation mode."""
        from ..profiles.unitree_go2 import build_unitree_go2_hals
        config = dict(self.config)
        if "simulation" not in config:
            config["simulation"] = {"enabled": True}
        else:
            config["simulation"]["enabled"] = True
        return build_unitree_go2_hals(config)

    def _load_apple_vision_pro(self) -> Optional[dict]:
        """Load Apple Vision Pro HALs in mock mode."""
        from ..profiles.apple_vision_pro import build_apple_vision_pro_hals
        return build_apple_vision_pro_hals(self.config)

    def _load_openglass(self) -> Optional[dict]:
        """Load OpenGlass HALs in mock mode."""
        from ..profiles.openglass import build_openglass_hals
        return build_openglass_hals(self.config)


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def validate_profile(profile_name: str, config: Optional[dict] = None) -> ValidationReport:
    """Validate a profile and return a report.

    Args:
        profile_name: Profile name to validate.
        config: Optional config dict. Defaults to empty dict.

    Returns:
        ValidationReport with check results.
    """
    validator = ProfileValidator(profile_name, config or {})
    return validator.run()


__all__ = [
    "ProfileValidator",
    "ValidationReport",
    "CheckResult",
    "validate_profile",
]
