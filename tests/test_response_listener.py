"""Tests for AgentResponseListener -- bidirectional agent-to-device response routing.

Covers:
  - ResponseType routing (text->audio, display->display, action->actuator)
  - AgentResponse dataclass creation
  - DeviceResponseRouter dispatch
  - Pipeline integration (listener registers/deregisters)
  - G2 HAL render_agent_response (mock BLE)
"""

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from openclaw_embodiment.core.response import (
    AgentResponse,
    AgentResponseListener,
    DeviceResponseRouter,
    ResponseType,
)


# ---------------------------------------------------------------------------
# Test 1: AgentResponse dataclass creation
# ---------------------------------------------------------------------------

class TestAgentResponseDataclass:
    """AgentResponse dataclass creation and field access."""

    def test_basic_creation(self):
        """Create an AgentResponse with required fields."""
        resp = AgentResponse(
            response_type=ResponseType.TEXT,
            content="Hello world",
        )
        assert resp.response_type == ResponseType.TEXT
        assert resp.content == "Hello world"
        assert resp.metadata == {}
        assert isinstance(resp.timestamp, datetime)

    def test_creation_with_metadata(self):
        """Create an AgentResponse with metadata."""
        meta = {"title": "Agent Says", "priority": "high"}
        resp = AgentResponse(
            response_type=ResponseType.DISPLAY,
            content="Status OK",
            metadata=meta,
        )
        assert resp.metadata["title"] == "Agent Says"
        assert resp.metadata["priority"] == "high"

    def test_all_response_types(self):
        """Verify all ResponseType variants are accessible."""
        types = [
            ResponseType.TEXT,
            ResponseType.AUDIO,
            ResponseType.DISPLAY,
            ResponseType.ACTION,
            ResponseType.HEARTBEAT,
        ]
        for rt in types:
            resp = AgentResponse(response_type=rt, content="test")
            assert resp.response_type == rt

    def test_timestamp_default(self):
        """Timestamp defaults to current UTC time."""
        before = datetime.utcnow()
        resp = AgentResponse(response_type=ResponseType.HEARTBEAT, content="ping")
        after = datetime.utcnow()
        assert before <= resp.timestamp <= after


# ---------------------------------------------------------------------------
# Test 2: ResponseType routing -- TEXT -> audio
# ---------------------------------------------------------------------------

class TestDeviceResponseRouterTextRouting:
    """TEXT responses route to AudioOutputHal first, then DisplayHal as fallback."""

    def test_text_routes_to_audio_when_available(self):
        """TEXT response dispatched to audio_output_hal.speak_agent_response()."""
        mock_audio = MagicMock()
        mock_display = MagicMock()

        router = DeviceResponseRouter(
            display_hal=mock_display,
            audio_output_hal=mock_audio,
        )
        resp = AgentResponse(response_type=ResponseType.TEXT, content="say this")
        router.route(resp)

        mock_audio.speak_agent_response.assert_called_once_with(resp)
        mock_display.render_agent_response.assert_not_called()

    def test_text_falls_back_to_display_when_no_audio(self):
        """TEXT response falls back to display when audio HAL is absent."""
        mock_display = MagicMock()

        router = DeviceResponseRouter(display_hal=mock_display)
        resp = AgentResponse(response_type=ResponseType.TEXT, content="show this")
        router.route(resp)

        mock_display.render_agent_response.assert_called_once_with(resp)

    def test_text_dropped_silently_when_no_hals(self):
        """TEXT with no HALs registered does not raise."""
        router = DeviceResponseRouter()
        resp = AgentResponse(response_type=ResponseType.TEXT, content="dropped")
        # Should not raise
        router.route(resp)


# ---------------------------------------------------------------------------
# Test 3: ResponseType routing -- DISPLAY -> display
# ---------------------------------------------------------------------------

class TestDeviceResponseRouterDisplayRouting:
    """DISPLAY responses route to DisplayHal.render_agent_response()."""

    def test_display_routes_to_display_hal(self):
        """DISPLAY response dispatched to display_hal.render_agent_response()."""
        mock_display = MagicMock()
        mock_audio = MagicMock()

        router = DeviceResponseRouter(
            display_hal=mock_display,
            audio_output_hal=mock_audio,
        )
        resp = AgentResponse(response_type=ResponseType.DISPLAY, content="render this")
        router.route(resp)

        mock_display.render_agent_response.assert_called_once_with(resp)
        mock_audio.speak_agent_response.assert_not_called()

    def test_display_dropped_when_no_display_hal(self):
        """DISPLAY with no display HAL does not raise."""
        router = DeviceResponseRouter()
        resp = AgentResponse(response_type=ResponseType.DISPLAY, content="no display")
        router.route(resp)  # Should not raise


# ---------------------------------------------------------------------------
# Test 4: ResponseType routing -- ACTION -> actuator
# ---------------------------------------------------------------------------

class TestDeviceResponseRouterActionRouting:
    """ACTION responses route to ActuatorHal.execute()."""

    def test_action_routes_to_actuator_hal(self):
        """ACTION response dispatched to actuator_hal.execute()."""
        mock_actuator = MagicMock()

        router = DeviceResponseRouter(actuator_hal=mock_actuator)
        resp = AgentResponse(
            response_type=ResponseType.ACTION,
            content="wave_arm",
            metadata={"command_id": "cmd-123", "speed": "slow"},
        )
        router.route(resp)

        mock_actuator.execute.assert_called_once()
        call_args = mock_actuator.execute.call_args[0][0]
        assert call_args.action == "wave_arm"
        assert call_args.command_id == "cmd-123"

    def test_action_dropped_when_no_actuator(self):
        """ACTION with no actuator HAL does not raise."""
        router = DeviceResponseRouter()
        resp = AgentResponse(response_type=ResponseType.ACTION, content="move")
        router.route(resp)  # Should not raise


# ---------------------------------------------------------------------------
# Test 5: DeviceResponseRouter dispatch -- HEARTBEAT and AUDIO
# ---------------------------------------------------------------------------

class TestDeviceResponseRouterOtherTypes:
    """HEARTBEAT and AUDIO routing."""

    def test_heartbeat_does_not_dispatch_to_any_hal(self):
        """HEARTBEAT is acknowledged without dispatching to HALs."""
        mock_display = MagicMock()
        mock_audio = MagicMock()
        mock_actuator = MagicMock()

        router = DeviceResponseRouter(
            display_hal=mock_display,
            audio_output_hal=mock_audio,
            actuator_hal=mock_actuator,
        )
        resp = AgentResponse(response_type=ResponseType.HEARTBEAT, content="ping")
        router.route(resp)

        mock_display.render_agent_response.assert_not_called()
        mock_audio.speak_agent_response.assert_not_called()
        mock_actuator.execute.assert_not_called()

    def test_audio_routes_to_audio_hal(self):
        """AUDIO response dispatched to audio_output_hal.speak_agent_response()."""
        mock_audio = MagicMock()
        router = DeviceResponseRouter(audio_output_hal=mock_audio)
        resp = AgentResponse(response_type=ResponseType.AUDIO, content="beep")
        router.route(resp)
        mock_audio.speak_agent_response.assert_called_once_with(resp)


# ---------------------------------------------------------------------------
# Test 6: AgentResponseListener lifecycle
# ---------------------------------------------------------------------------

class TestAgentResponseListenerLifecycle:
    """AgentResponseListener registers, handles events, and deregisters."""

    def test_register_activates_listener(self):
        """register() sets is_active to True."""
        listener = AgentResponseListener()
        assert not listener.is_active
        listener.register()
        assert listener.is_active

    def test_deregister_deactivates_listener(self):
        """deregister() sets is_active to False."""
        listener = AgentResponseListener()
        listener.register()
        listener.deregister()
        assert not listener.is_active

    def test_inactive_listener_drops_events(self):
        """Events are silently dropped when listener is not registered."""
        mock_router = MagicMock()
        listener = AgentResponseListener(router=mock_router)
        # Not registered -- inactive
        resp = AgentResponse(response_type=ResponseType.TEXT, content="dropped")
        listener.on_agent_event(resp)
        mock_router.route.assert_not_called()

    def test_active_listener_dispatches_events(self):
        """Active listener routes events through the DeviceResponseRouter."""
        mock_router = MagicMock()
        listener = AgentResponseListener(router=mock_router)
        listener.register()
        resp = AgentResponse(response_type=ResponseType.TEXT, content="hello")
        listener.on_agent_event(resp)
        mock_router.route.assert_called_once_with(resp)

    def test_callbacks_invoked_after_routing(self):
        """Registered callbacks are called after HAL dispatch."""
        received: List[AgentResponse] = []

        def my_callback(r: AgentResponse) -> None:
            received.append(r)

        listener = AgentResponseListener()
        listener.register()
        listener.add_callback(my_callback)

        resp = AgentResponse(response_type=ResponseType.HEARTBEAT, content="ping")
        listener.on_agent_event(resp)

        assert len(received) == 1
        assert received[0] is resp

    def test_remove_callback(self):
        """remove_callback() stops future invocations."""
        called: List[bool] = []

        def cb(r: AgentResponse) -> None:
            called.append(True)

        listener = AgentResponseListener()
        listener.register()
        listener.add_callback(cb)
        listener.remove_callback(cb)

        listener.on_agent_event(AgentResponse(ResponseType.HEARTBEAT, "ping"))
        assert called == []


# ---------------------------------------------------------------------------
# Test 7: Pipeline integration -- listener registers/deregisters
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    """EmbodimentSDK wires AgentResponseListener on start/stop."""

    def _make_registry(self):
        """Build a HALRegistry with all required mocks."""
        from openclaw_embodiment.core.pipeline import HALRegistry

        registry = HALRegistry()

        imu = MagicMock()
        imu.HAL_VERSION = "1.0.0"
        imu.read_sample.return_value = None

        camera = MagicMock()
        camera.HAL_VERSION = "1.0.0"

        transport = MagicMock()
        transport.HAL_VERSION = "1.0.0"
        transport.send.return_value = MagicMock(success=True, bytes_sent=0)

        registry.register_imu(imu)
        registry.register_camera(camera)
        registry.register_transport(transport)
        return registry

    def test_listener_registered_on_start(self):
        """EmbodimentSDK.start() registers the AgentResponseListener."""
        from openclaw_embodiment.core.pipeline import EmbodimentSDK

        registry = self._make_registry()

        mock_listener = MagicMock(spec=AgentResponseListener)
        mock_listener.is_active = False

        sdk = EmbodimentSDK(registry=registry, response_listener=mock_listener)
        sdk.start()

        mock_listener.register.assert_called_once()
        sdk.stop()

    def test_listener_deregistered_on_stop(self):
        """EmbodimentSDK.stop() deregisters the AgentResponseListener."""
        from openclaw_embodiment.core.pipeline import EmbodimentSDK

        registry = self._make_registry()

        mock_listener = MagicMock(spec=AgentResponseListener)
        mock_listener.is_active = False

        sdk = EmbodimentSDK(registry=registry, response_listener=mock_listener)
        sdk.start()
        sdk.stop()

        mock_listener.deregister.assert_called_once()

    def test_auto_builds_listener_if_none_provided(self):
        """EmbodimentSDK builds a default listener when none is provided."""
        from openclaw_embodiment.core.pipeline import EmbodimentSDK

        registry = self._make_registry()
        sdk = EmbodimentSDK(registry=registry)
        sdk.start()

        assert sdk.response_listener is not None
        assert sdk.response_listener.is_active
        sdk.stop()
        assert not sdk.response_listener.is_active


# ---------------------------------------------------------------------------
# Test 8: G2 HAL render_agent_response (mock BLE)
# ---------------------------------------------------------------------------

class TestG2DisplayHALRenderAgentResponse:
    """G2DisplayHAL.render_agent_response() sends text via BLE Teleprompter."""

    def test_render_agent_response_calls_send_teleprompter(self):
        """render_agent_response sends response content via BLE teleprompter."""
        from openclaw_embodiment.hal.even_g2_reference import G2DisplayHAL

        hal = G2DisplayHAL(right_address="AA:BB:CC:DD:EE:FF")
        hal.initialize()

        resp = AgentResponse(
            response_type=ResponseType.DISPLAY,
            content="Meeting at 3pm",
        )

        sent_texts: List[str] = []

        async def mock_send_teleprompter(text: str) -> None:
            sent_texts.append(text)

        hal._send_teleprompter = mock_send_teleprompter

        # Patch _run_async to actually run the coroutine
        import openclaw_embodiment.hal.even_g2_reference as g2_mod

        original_run_async = g2_mod._run_async

        def sync_run_async(coro):
            return asyncio.get_event_loop().run_until_complete(coro)

        g2_mod._run_async = sync_run_async
        try:
            hal.render_agent_response(resp)
        finally:
            g2_mod._run_async = original_run_async

        assert len(sent_texts) == 1
        assert "Meeting at 3pm" in sent_texts[0]

    def test_render_agent_response_with_title_metadata(self):
        """render_agent_response prepends title from metadata."""
        from openclaw_embodiment.hal.even_g2_reference import G2DisplayHAL
        import openclaw_embodiment.hal.even_g2_reference as g2_mod

        hal = G2DisplayHAL(right_address="AA:BB:CC:DD:EE:FF")
        hal.initialize()

        resp = AgentResponse(
            response_type=ResponseType.DISPLAY,
            content="Remember to call Dr. Smith",
            metadata={"title": "Reminder"},
        )

        sent_texts: List[str] = []

        async def mock_send_teleprompter(text: str) -> None:
            sent_texts.append(text)

        hal._send_teleprompter = mock_send_teleprompter

        original_run_async = g2_mod._run_async

        def sync_run_async(coro):
            return asyncio.get_event_loop().run_until_complete(coro)

        g2_mod._run_async = sync_run_async
        try:
            hal.render_agent_response(resp)
        finally:
            g2_mod._run_async = original_run_async

        assert len(sent_texts) == 1
        assert "Reminder" in sent_texts[0]
        assert "Remember to call Dr. Smith" in sent_texts[0]

    def test_render_agent_response_truncates_long_content(self):
        """render_agent_response truncates content > 200 chars."""
        from openclaw_embodiment.hal.even_g2_reference import G2DisplayHAL
        import openclaw_embodiment.hal.even_g2_reference as g2_mod

        hal = G2DisplayHAL(right_address="AA:BB:CC:DD:EE:FF")
        hal.initialize()

        long_content = "A" * 500
        resp = AgentResponse(
            response_type=ResponseType.DISPLAY,
            content=long_content,
        )

        sent_texts: List[str] = []

        async def mock_send_teleprompter(text: str) -> None:
            sent_texts.append(text)

        hal._send_teleprompter = mock_send_teleprompter

        original_run_async = g2_mod._run_async

        def sync_run_async(coro):
            return asyncio.get_event_loop().run_until_complete(coro)

        g2_mod._run_async = sync_run_async
        try:
            hal.render_agent_response(resp)
        finally:
            g2_mod._run_async = original_run_async

        assert len(sent_texts[0]) <= 200
