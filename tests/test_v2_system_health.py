"""Tests for SystemHealthHal (10th HAL ABC) and SimulatedSystemHealth."""

from __future__ import annotations

import datetime

import pytest

from openclaw_embodiment.hal.base import HealthReport, SystemHealthHal
from openclaw_embodiment.hal.simulator import SimulatedSystemHealth


class TestHealthReportDataclass:
    def test_instantiation(self) -> None:
        report = HealthReport(
            timestamp=datetime.datetime.utcnow(),
            device_id="test",
            cpu_percent=10.0,
            memory_percent=30.0,
            temperature_c=45.0,
            battery_percent=80.0,
            connectivity={"wifi": True},
            sensor_status={"camera": True},
            is_operational=True,
            warnings=[],
        )
        assert report.device_id == "test"
        assert report.is_operational is True
        assert isinstance(report.connectivity, dict)

    def test_optional_fields_none(self) -> None:
        report = HealthReport(
            timestamp=datetime.datetime.utcnow(),
            device_id="test",
            cpu_percent=None,
            memory_percent=None,
            temperature_c=None,
            battery_percent=None,
            connectivity={},
            sensor_status={},
            is_operational=True,
            warnings=[],
        )
        assert report.cpu_percent is None
        assert report.battery_percent is None


class TestSystemHealthHalABC:
    def test_is_abstract(self) -> None:
        """SystemHealthHal cannot be instantiated directly."""
        with pytest.raises(TypeError):
            SystemHealthHal()  # type: ignore[abstract]

    def test_required_methods(self) -> None:
        """Verify abstract methods exist on the ABC."""
        assert hasattr(SystemHealthHal, "get_health_report")
        assert hasattr(SystemHealthHal, "is_operational")
        assert hasattr(SystemHealthHal, "on_degraded")


class TestSimulatedSystemHealth:
    def test_get_health_report_returns_green(self) -> None:
        health = SimulatedSystemHealth("sim-dev")
        report = health.get_health_report()
        assert isinstance(report, HealthReport)
        assert report.is_operational is True
        assert report.warnings == []
        assert report.device_id == "sim-dev"

    def test_is_operational_true(self) -> None:
        health = SimulatedSystemHealth()
        assert health.is_operational() is True

    def test_cpu_in_range(self) -> None:
        health = SimulatedSystemHealth()
        report = health.get_health_report()
        assert report.cpu_percent is not None
        assert 0.0 <= report.cpu_percent <= 100.0

    def test_connectivity_keys(self) -> None:
        health = SimulatedSystemHealth()
        report = health.get_health_report()
        assert "wifi" in report.connectivity
        assert "ble" in report.connectivity

    def test_sensor_status_keys(self) -> None:
        health = SimulatedSystemHealth()
        report = health.get_health_report()
        assert "camera" in report.sensor_status
        assert "imu" in report.sensor_status

    def test_on_degraded_callback_registered(self) -> None:
        health = SimulatedSystemHealth()
        called = []
        health.on_degraded(lambda r: called.append(r))
        assert len(health._degraded_callbacks) == 1

    def test_validate(self) -> None:
        health = SimulatedSystemHealth()
        assert health.validate() is True

    def test_get_device_info(self) -> None:
        health = SimulatedSystemHealth("my-device")
        info = health.get_device_info()
        assert "name" in info
        assert info["device_id"] == "my-device"

    def test_implements_hal_base(self) -> None:
        health = SimulatedSystemHealth()
        assert isinstance(health, SystemHealthHal)
