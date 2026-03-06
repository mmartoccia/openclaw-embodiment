"""Tests for profile auto-discovery (discovery/auto.py)."""

from __future__ import annotations

import pytest

from openclaw_embodiment.discovery.auto import (
    DeviceSignature,
    NoDeviceFoundError,
    ProfileManifest,
    _probe_port,
    _scan_usb,
    _score_signatures,
    auto_discover_profile,
)


class TestProfileManifest:
    def test_default_entries_populated(self) -> None:
        m = ProfileManifest()
        entries = m.all_entries()
        assert len(entries) > 0
        names = [e.profile_name for e in entries]
        assert "reachy2" in names
        assert "even-g2" in names

    def test_register_custom(self) -> None:
        m = ProfileManifest()
        custom = DeviceSignature(
            profile_name="my-device",
            ble_names=["MyDevice"],
            usb_ids=["dead:beef"],
        )
        m.register(custom)
        names = [e.profile_name for e in m.all_entries()]
        assert "my-device" in names

    def test_all_entries_returns_copy(self) -> None:
        m = ProfileManifest()
        e1 = m.all_entries()
        e2 = m.all_entries()
        assert e1 is not e2


class TestDeviceSignature:
    def test_defaults(self) -> None:
        sig = DeviceSignature(profile_name="test")
        assert sig.usb_ids == []
        assert sig.ble_names == []
        assert sig.network_ports == []
        assert sig.confidence == 1.0


class TestScoreSignatures:
    def _make_sig(self, name: str, usb=None, ble=None, ports=None) -> DeviceSignature:
        return DeviceSignature(
            profile_name=name,
            usb_ids=usb or [],
            ble_names=ble or [],
            network_ports=ports or [],
            confidence=1.0,
        )

    def test_no_matches(self) -> None:
        sigs = [self._make_sig("a", usb=["1234:5678"])]
        matches = _score_signatures(sigs, usb_ids=[], ble_names=[], network_results={})
        assert matches == []

    def test_usb_match(self) -> None:
        sigs = [self._make_sig("a", usb=["1234:5678"])]
        matches = _score_signatures(sigs, usb_ids=["1234:5678"], ble_names=[], network_results={})
        assert len(matches) == 1
        assert matches[0].signature.profile_name == "a"
        assert "usb" in matches[0].matched_via

    def test_ble_match(self) -> None:
        sigs = [self._make_sig("g2", ble=["Even G2"])]
        matches = _score_signatures(sigs, usb_ids=[], ble_names=["My Even G2 Glasses"], network_results={})
        assert len(matches) == 1
        assert "ble" in matches[0].matched_via

    def test_network_match(self) -> None:
        sigs = [self._make_sig("reachy", ports=[("reachy.local", 50051)])]
        net = {"reachy.local:50051": True}
        matches = _score_signatures(sigs, usb_ids=[], ble_names=[], network_results=net)
        assert len(matches) == 1
        assert "network" in matches[0].matched_via

    def test_multi_match_sorted_by_score(self) -> None:
        sigs = [
            self._make_sig("low-score", ble=["DevA"]),
            self._make_sig("high-score", usb=["ab:cd"], ble=["DevB"]),
        ]
        matches = _score_signatures(
            sigs,
            usb_ids=["ab:cd"],
            ble_names=["DevA", "DevB"],
            network_results={},
        )
        assert matches[0].signature.profile_name == "high-score"


class TestNoDeviceFoundError:
    def test_has_scan_results(self) -> None:
        err = NoDeviceFoundError("not found", scan_results={"usb": [], "ble": []})
        assert err.scan_results == {"usb": [], "ble": []}


class TestScanUsb:
    def test_returns_list(self) -> None:
        """_scan_usb should return a list (may be empty if no USB devices present)."""
        result = _scan_usb(timeout_s=2.0)
        assert isinstance(result, list)


class TestProbePort:
    def test_closed_port_returns_false(self) -> None:
        # Port 19999 should not be listening
        result = _probe_port("127.0.0.1", 19999, timeout_s=0.2)
        assert result is False

    def test_open_port_returns_true(self) -> None:
        import socket
        import threading

        # Start a tiny TCP server to confirm probe works
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)

        def accept_once():
            try:
                conn, _ = server.accept()
                conn.close()
            except Exception:
                pass
            finally:
                server.close()

        t = threading.Thread(target=accept_once, daemon=True)
        t.start()
        result = _probe_port("127.0.0.1", port, timeout_s=1.0)
        assert result is True


class TestAutoDiscoverProfile:
    def test_raises_when_no_device(self) -> None:
        """With an empty manifest, auto_discover_profile must raise NoDeviceFoundError."""
        empty_manifest = ProfileManifest()
        empty_manifest._entries = []  # Clear all entries
        with pytest.raises(NoDeviceFoundError) as exc_info:
            auto_discover_profile(timeout_s=0.5, manifest=empty_manifest)
        assert exc_info.value.scan_results is not None

    def test_returns_tuple_str_dict(self) -> None:
        """If any device is found, must return (str, dict)."""
        # Create a manifest with a signature that matches via a local port
        import socket
        import threading

        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(5)

        def serve():
            while True:
                try:
                    conn, _ = server.accept()
                    conn.close()
                except Exception:
                    break

        t = threading.Thread(target=serve, daemon=True)
        t.start()

        manifest = ProfileManifest()
        manifest._entries = [
            DeviceSignature(
                profile_name="test-device",
                network_ports=[("127.0.0.1", port)],
                suggested_config={"host": "127.0.0.1", "port": port},
                confidence=1.0,
            )
        ]

        name, cfg = auto_discover_profile(timeout_s=2.0, manifest=manifest)
        server.close()

        assert isinstance(name, str)
        assert isinstance(cfg, dict)
        assert name == "test-device"
        assert "_matched_via" in cfg
        assert "network" in cfg["_matched_via"]
