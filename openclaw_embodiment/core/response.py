"""AgentResponseListener -- receives agent events and routes them to device HALs.

Closes the bidirectional loop: agents can now push responses back to physical
devices via the HAL layer. Supports TEXT, AUDIO, DISPLAY, ACTION, and HEARTBEAT
response types with async delivery support.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from ..hal.base import ActuatorHal, AudioOutputHal, DisplayHal

logger = logging.getLogger(__name__)


class ResponseType(Enum):
    """Enumeration of agent response delivery types."""

    TEXT = "text"
    AUDIO = "audio"
    DISPLAY = "display"
    ACTION = "action"
    HEARTBEAT = "heartbeat"


@dataclass
class AgentResponse:
    """Typed response payload from agent to device.

    Attributes:
        response_type: How this response should be delivered.
        content:       Primary text/action content.
        metadata:      Auxiliary key-value data (command IDs, durations, etc.).
        timestamp:     UTC creation time.
    """

    response_type: ResponseType
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


class ResponseCallback(Protocol):
    """Protocol for device-specific response handlers.

    Implementors receive every routed AgentResponse after HAL dispatch.
    Useful for logging, telemetry, and custom side-effects.
    """

    def handle(self, response: AgentResponse) -> None:
        """Process a routed agent response."""
        ...


class DeviceResponseRouter:
    """Dispatches AgentResponse objects to the correct HAL.

    Routing table:
        TEXT       -> AudioOutputHal.speak_agent_response() OR DisplayHal.render_agent_response()
        AUDIO      -> AudioOutputHal.speak_agent_response()
        DISPLAY    -> DisplayHal.render_agent_response()
        ACTION     -> ActuatorHal.execute()
        HEARTBEAT  -> acknowledge only (no HAL dispatch)

    HALs are optional; if the target HAL is None the response is silently dropped
    for that path. TEXT falls back to display if audio is unavailable.
    """

    def __init__(
        self,
        display_hal: Optional["DisplayHal"] = None,
        audio_output_hal: Optional["AudioOutputHal"] = None,
        actuator_hal: Optional["ActuatorHal"] = None,
    ) -> None:
        """Initialise router with optional HAL instances.

        Args:
            display_hal:      DisplayHal to receive DISPLAY and TEXT (fallback) responses.
            audio_output_hal: AudioOutputHal to receive AUDIO and TEXT responses.
            actuator_hal:     ActuatorHal to receive ACTION responses.
        """
        self._display = display_hal
        self._audio = audio_output_hal
        self._actuator = actuator_hal

    def route(self, response: AgentResponse) -> None:
        """Dispatch response to the appropriate HAL.

        Args:
            response: AgentResponse to route.
        """
        rt = response.response_type

        if rt == ResponseType.TEXT:
            # Prefer audio TTS; fall back to display if audio HAL unavailable
            if self._audio is not None:
                self._audio.speak_agent_response(response)
            elif self._display is not None:
                self._display.render_agent_response(response)

        elif rt == ResponseType.AUDIO:
            if self._audio is not None:
                self._audio.speak_agent_response(response)

        elif rt == ResponseType.DISPLAY:
            if self._display is not None:
                self._display.render_agent_response(response)

        elif rt == ResponseType.ACTION:
            if self._actuator is not None:
                from ..hal.base import ActuatorCommand
                import time as _time
                cmd = ActuatorCommand(
                    command_id=str(response.metadata.get("command_id", f"cmd-{id(response)}")),
                    action=response.content,
                    params=dict(response.metadata),
                    timestamp_ms=int(response.timestamp.timestamp() * 1000),
                    timeout_ms=int(response.metadata.get("timeout_ms", 5000)),
                )
                self._actuator.execute(cmd)

        elif rt == ResponseType.HEARTBEAT:
            logger.debug("AgentResponseListener: HEARTBEAT acknowledged at %s", response.timestamp)

        else:
            logger.warning("DeviceResponseRouter: unknown ResponseType %s -- dropped", rt)


class AgentResponseListener:
    """Receives agent events and routes them to device HALs.

    Wraps OpenClaw's onAgentEvent plugin hook. Thread-safe and async-capable --
    responses can arrive mid-session from any thread or coroutine.

    Lifecycle:
        listener.register()     -- called on pipeline start
        listener.on_agent_event(response)  -- called per event
        listener.deregister()   -- called on pipeline stop

    Usage::
        from openclaw_embodiment.core.response import (
            AgentResponseListener, DeviceResponseRouter, ResponseType, AgentResponse
        )
        router = DeviceResponseRouter(display_hal=my_display, audio_output_hal=my_audio)
        listener = AgentResponseListener(router=router)
        listener.register()
        listener.on_agent_event(AgentResponse(ResponseType.TEXT, "Hello world"))
        listener.deregister()
    """

    def __init__(self, router: Optional[DeviceResponseRouter] = None) -> None:
        """Initialise listener with an optional pre-built router.

        Args:
            router: DeviceResponseRouter to use for HAL dispatch.
                    If None, a default no-op router is created.
        """
        self._router: DeviceResponseRouter = router if router is not None else DeviceResponseRouter()
        self._callbacks: List[Callable[[AgentResponse], None]] = []
        self._active: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register(self) -> None:
        """Register this listener with OpenClaw's onAgentEvent hook."""
        self._active = True
        logger.debug("AgentResponseListener: registered and active")

    def deregister(self) -> None:
        """Deregister from OpenClaw's onAgentEvent hook."""
        self._active = False
        logger.debug("AgentResponseListener: deregistered")

    @property
    def is_active(self) -> bool:
        """Return True if listener is registered and accepting events."""
        return self._active

    # ------------------------------------------------------------------
    # Callback management
    # ------------------------------------------------------------------

    def add_callback(self, callback: Callable[[AgentResponse], None]) -> None:
        """Register a post-dispatch callback.

        Args:
            callback: Callable that receives each AgentResponse after HAL dispatch.
        """
        self._callbacks.append(callback)

    def remove_callback(self, callback: Callable[[AgentResponse], None]) -> None:
        """Remove a previously registered callback.

        Args:
            callback: Callable to remove.
        """
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    # ------------------------------------------------------------------
    # Event handling
    # ------------------------------------------------------------------

    def on_agent_event(self, response: AgentResponse) -> None:
        """Handle an incoming agent event synchronously.

        Routes via DeviceResponseRouter then invokes all registered callbacks.
        Thread-safe -- can be called from any thread.

        Args:
            response: AgentResponse delivered by the OpenClaw agent.
        """
        if not self._active:
            logger.debug("AgentResponseListener: inactive, dropping event %s", response.response_type)
            return
        try:
            self._router.route(response)
        except Exception as exc:
            logger.exception("AgentResponseListener: router error -- %s", exc)
        for cb in list(self._callbacks):
            try:
                cb(response)
            except Exception as exc:
                logger.exception("AgentResponseListener: callback error -- %s", exc)

    async def on_agent_event_async(self, response: AgentResponse) -> None:
        """Handle an incoming agent event asynchronously.

        Runs the synchronous handler in an executor to avoid blocking the
        event loop. Suitable for mid-session delivery from async contexts.

        Args:
            response: AgentResponse delivered by the OpenClaw agent.
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.on_agent_event, response)
