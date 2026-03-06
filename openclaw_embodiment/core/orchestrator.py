"""HalOrchestrator -- hookable async pipeline for OpenClaw Embodiment SDK v2.0.

Replaces the manual trigger→capture→classify→transport→actuate wiring in
``EmbodimentSDK._run_loop()`` with a first-class, introspectable object.

Per-stage middleware hooks enable logging, metrics, retries, and custom
preprocessing without subclassing or monkey-patching the SDK.

Classes:
    OrchestratorStage: Enum of pipeline stages.
    StageContext: Contextual metadata passed to every hook.
    OrchestratorConfig: Configuration dataclass.
    HalOrchestrator: Main orchestrator class.

Usage::

    async def log_trigger(event: TriggerEvent, ctx: StageContext) -> TriggerEvent:
        print(f"[TRIGGER] {event.event_id}")
        return event

    config = OrchestratorConfig(
        hooks={OrchestratorStage.TRIGGER: [log_trigger]}
    )
    orch = HalOrchestrator(registry=registry, config=config)
    await orch.run()
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from ..context.models import AgentResponse, ContextPayload
from ..hal.base import (
    AudioOutputHal,
    DisplayCard,
    DisplayHal,
    SendResult,
    TransportHal,
)
from ..transport.ble import PacketSerializer
from .multi_device import DeviceRegistry
from .trigger import TriggerConfig, TriggerDetector, TriggerEvent

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Stage enum
# ---------------------------------------------------------------------------


class OrchestratorStage(Enum):
    """Pipeline stages for the HalOrchestrator.

    TRIGGER:    IMU/audio event detection.
    CAPTURE:    Camera frame + audio grab.
    CLASSIFY:   Classifier gate (interesting/uninteresting).
    TRANSPORT:  Payload delivery to the agent.
    ACTUATE:    Physical actuator commands (robot response).
    RESPONSE:   Agent response routed back to device(s).
    """

    TRIGGER = "trigger"
    CAPTURE = "capture"
    CLASSIFY = "classify"
    TRANSPORT = "transport"
    ACTUATE = "actuate"
    RESPONSE = "response"


# ---------------------------------------------------------------------------
# StageContext
# ---------------------------------------------------------------------------


@dataclass
class StageContext:
    """Metadata passed to every stage hook call.

    Attributes:
        stage: The pipeline stage being executed.
        orchestrator_id: Unique ID of the HalOrchestrator instance.
        device_id: ID of the device driving this pipeline run.
        run_id: Unique ID for this single trigger→actuate pass.
        started_at: UTC datetime when the run started.
        metadata: Mutable dict for hooks to share state within a run.
    """

    stage: OrchestratorStage
    orchestrator_id: str
    device_id: str
    run_id: str
    started_at: datetime
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# StageHook type alias
# ---------------------------------------------------------------------------

# A hook is an async callable: (data, context) -> data (same type)
StageHook = Callable[[Any, StageContext], Awaitable[Any]]

# ---------------------------------------------------------------------------
# OrchestratorConfig
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    """Configuration for HalOrchestrator.

    Attributes:
        hooks: Per-stage lists of hook callables. Applied in order.
        polling_hz: IMU polling frequency. Passed to TriggerConfig.
        concurrent_triggers: Maximum concurrent trigger→actuate passes.
        error_policy: 'continue' (log and continue), 'abort' (raise), or
                      'retry' (retry up to retry_count times).
        retry_count: Number of retries per stage when error_policy='retry'.
        metrics_enabled: If True, built-in latency logging hooks are active.
        stage_timeout_ms: Per-stage timeout in milliseconds (0 = no timeout).
    """

    hooks: Dict[OrchestratorStage, List[StageHook]] = field(default_factory=dict)
    polling_hz: int = 25
    concurrent_triggers: int = 4
    error_policy: str = "continue"
    retry_count: int = 3
    metrics_enabled: bool = True
    stage_timeout_ms: int = 5000


# ---------------------------------------------------------------------------
# Built-in hooks
# ---------------------------------------------------------------------------


async def _builtin_latency_hook(data: Any, ctx: StageContext) -> Any:
    """Built-in hook: log stage entry and record elapsed time in metadata."""
    now_ms = time.monotonic_ns() // 1_000_000
    stage_start_key = f"_stage_start_{ctx.stage.value}"
    if stage_start_key not in ctx.metadata:
        ctx.metadata[stage_start_key] = now_ms
        logger.debug(
            "[%s] stage=%s run=%s device=%s",
            ctx.orchestrator_id[:8],
            ctx.stage.value,
            ctx.run_id[:8],
            ctx.device_id,
        )
    else:
        elapsed = now_ms - ctx.metadata[stage_start_key]
        logger.debug(
            "[%s] stage=%s done in %dms run=%s",
            ctx.orchestrator_id[:8],
            ctx.stage.value,
            elapsed,
            ctx.run_id[:8],
        )
    return data


# ---------------------------------------------------------------------------
# HalOrchestrator
# ---------------------------------------------------------------------------


class HalOrchestrator:
    """Hookable async pipeline for the Embodiment SDK.

    Drives the trigger→capture→classify→transport→actuate loop using asyncio.
    Per-stage hooks are composable middleware that transform or observe stage data.

    Blocking HAL calls (camera capture, transport send) are dispatched to a
    thread executor to avoid blocking the event loop.

    Args:
        registry: HALRegistry with initialized HAL instances.
        config: OrchestratorConfig controlling hooks, concurrency, and policy.
        device_id: Identifier for this device in log output.
    """

    def __init__(
        self,
        registry: Any,  # HALRegistry (avoid circular import)
        config: Optional[OrchestratorConfig] = None,
        device_id: str = "default",
    ) -> None:
        self._registry = registry
        self._config = config or OrchestratorConfig()
        self._device_id = device_id
        self._orchestrator_id = str(uuid.uuid4())
        self._running = False
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._trigger_config = TriggerConfig(polling_hz=self._config.polling_hz)
        self._detector = TriggerDetector(self._trigger_config)

        # Merge built-in latency hooks (prepend to each stage)
        if self._config.metrics_enabled:
            self._hooks: Dict[OrchestratorStage, List[StageHook]] = {}
            for stage in OrchestratorStage:
                user_hooks = self._config.hooks.get(stage, [])
                self._hooks[stage] = [_builtin_latency_hook] + list(user_hooks)
        else:
            self._hooks = {stage: list(hooks) for stage, hooks in self._config.hooks.items()}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_hook(self, stage: OrchestratorStage, hook: StageHook) -> None:
        """Add a middleware hook at the end of the stage's hook list.

        Args:
            stage: The pipeline stage to attach the hook to.
            hook: Async callable (data, StageContext) -> data.
        """
        if stage not in self._hooks:
            self._hooks[stage] = [_builtin_latency_hook] if self._config.metrics_enabled else []
        self._hooks[stage].append(hook)
        logger.debug("Added hook %s to stage %s", hook.__name__, stage.value)

    def remove_hook(self, stage: OrchestratorStage, hook: StageHook) -> None:
        """Remove a previously registered hook from a stage.

        Args:
            stage: The pipeline stage to remove from.
            hook: The exact hook callable to remove.

        Raises:
            ValueError: If the hook is not registered for the stage.
        """
        if stage not in self._hooks or hook not in self._hooks[stage]:
            raise ValueError(f"Hook {hook!r} not registered for stage {stage.value}")
        self._hooks[stage].remove(hook)
        logger.debug("Removed hook %s from stage %s", hook.__name__, stage.value)

    async def run(self) -> None:
        """Start the main orchestration loop.

        Polls the IMU at the configured rate, fires trigger events, and
        processes them concurrently (up to ``concurrent_triggers`` in parallel).

        Returns when ``stop()`` is called.
        """
        self._running = True
        self._semaphore = asyncio.Semaphore(self._config.concurrent_triggers)
        loop = asyncio.get_event_loop()
        poll_interval = 1.0 / max(1, self._config.polling_hz)

        logger.info(
            "HalOrchestrator %s starting (device=%s, polling_hz=%d)",
            self._orchestrator_id[:8],
            self._device_id,
            self._config.polling_hz,
        )

        try:
            while self._running:
                sample = await loop.run_in_executor(
                    None, self._registry.imu.read_sample
                )
                if sample is not None:
                    evt = self._detector.update(sample)
                    if evt:
                        asyncio.ensure_future(self._run_with_semaphore(evt))
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            logger.info("HalOrchestrator %s cancelled", self._orchestrator_id[:8])
            raise
        finally:
            self._running = False
            logger.info("HalOrchestrator %s stopped", self._orchestrator_id[:8])

    def stop(self) -> None:
        """Signal the orchestrator loop to stop after the current iteration."""
        self._running = False

    async def run_once(self, trigger_event: TriggerEvent) -> Optional[AgentResponse]:
        """Execute a single trigger→actuate pass.

        Useful for testing and scripted scenarios.

        Args:
            trigger_event: The trigger event to process.

        Returns:
            AgentResponse produced at the end of the pipeline, or None if
            the classify stage rejected the event.
        """
        return await self._execute_pipeline(trigger_event)

    # ------------------------------------------------------------------
    # Internal pipeline
    # ------------------------------------------------------------------

    async def _run_with_semaphore(self, event: TriggerEvent) -> None:
        """Execute the pipeline under the concurrency semaphore."""
        assert self._semaphore is not None
        async with self._semaphore:
            try:
                await self._execute_pipeline(event)
            except Exception as exc:  # grain: ignore NAKED_EXCEPT -- top-level pipeline catch; device/driver errors are unpredictable
                logger.error("Pipeline error for event %s: %s", event.event_id, exc)

    async def _execute_pipeline(self, trigger_event: TriggerEvent) -> Optional[AgentResponse]:
        """Run the full pipeline for one trigger event.

        Args:
            trigger_event: Trigger that started this run.

        Returns:
            AgentResponse or None.
        """
        run_id = str(uuid.uuid4())
        started_at = datetime.utcnow()
        loop = asyncio.get_event_loop()

        def _ctx(stage: OrchestratorStage) -> StageContext:
            return StageContext(
                stage=stage,
                orchestrator_id=self._orchestrator_id,
                device_id=self._device_id,
                run_id=run_id,
                started_at=started_at,
            )

        # ── TRIGGER stage ──
        event = await self._run_stage(
            OrchestratorStage.TRIGGER, trigger_event, _ctx(OrchestratorStage.TRIGGER)
        )
        if event is None:
            return None

        # ── CAPTURE stage ──
        frame = await self._run_in_executor(loop, self._registry.camera.capture_frame)
        audio_data = b""
        if self._registry.microphone:
            chunk = await self._run_in_executor(loop, self._registry.microphone.get_buffer, 120)
            audio_data = chunk.data

        capture_result = {"frame": frame, "audio": audio_data}
        capture_result = await self._run_stage(
            OrchestratorStage.CAPTURE, capture_result, _ctx(OrchestratorStage.CAPTURE)
        )
        if capture_result is None:
            return None

        frame = capture_result["frame"]
        audio_data = capture_result["audio"]

        # ── CLASSIFY stage ──
        if self._registry.classifier:
            classify_input = {"frame": frame, "event": event}
            classify_input = await self._run_stage(
                OrchestratorStage.CLASSIFY, classify_input, _ctx(OrchestratorStage.CLASSIFY)
            )
            if classify_input is None:
                return None

            result = await self._run_in_executor(
                loop,
                self._registry.classifier.classify,
                frame.data,
                frame.width,
                frame.height,
                frame.format,
            )
            if result.label != "interesting":
                logger.debug(
                    "Classifier rejected event %s (label=%s conf=%.2f)",
                    event.event_id,
                    result.label,
                    result.confidence,
                )
                return None
            conf = int(max(0.0, min(1.0, result.confidence)) * 32767)
        else:
            conf = int(0.5 * 32767)

        # Build context payload
        payload = ContextPayload(
            event_id=event.event_id,
            device_id=self._device_id,
            timestamp_epoch=event.timestamp_epoch,
            flags=0b00000111,
            image_data=frame.data,
            audio_data=audio_data,
            imu_pitch=int(event.head_pitch * 100),
            imu_yaw=int(event.head_yaw * 100),
            imu_roll=int(event.head_roll * 100),
            imu_trigger_confidence=int(event.trigger_confidence * 65535),
            scene_gate_confidence=conf,
        )
        packet = PacketSerializer.serialize(payload)

        # ── TRANSPORT stage ──
        send_result: Optional[SendResult] = None
        transport_data = {"packet": packet, "payload": payload}
        transport_data = await self._run_stage(
            OrchestratorStage.TRANSPORT, transport_data, _ctx(OrchestratorStage.TRANSPORT)
        )
        if transport_data is None:
            return None

        for _, tx in self._registry.transports:
            send_result = await self._run_in_executor(loop, tx.send, transport_data["packet"])
            if send_result.success:
                break

        bytes_sent = send_result.bytes_sent if send_result else 0

        # Build response
        agent_response = AgentResponse(
            response_id=f"resp-{event.event_id}",
            event_id=event.event_id,
            trigger_timestamp_ms=event.timestamp_ms,
            title="Captured",
            body=f"Context sent ({bytes_sent} bytes)",
        )

        # ── ACTUATE stage ──
        actuate_data = {"response": agent_response, "event": event}
        actuate_data = await self._run_stage(
            OrchestratorStage.ACTUATE, actuate_data, _ctx(OrchestratorStage.ACTUATE)
        )

        # Render to display if available
        if self._registry.display:
            card = DisplayCard("card", agent_response.title, agent_response.body, 12, 3000)
            await self._run_in_executor(loop, self._registry.display.show, card)

        # ── RESPONSE stage ──
        await self._run_stage(
            OrchestratorStage.RESPONSE, agent_response, _ctx(OrchestratorStage.RESPONSE)
        )

        return agent_response

    async def _run_stage(
        self,
        stage: OrchestratorStage,
        data: Any,
        ctx: StageContext,
    ) -> Optional[Any]:
        """Run all hooks for a stage in order, applying error policy.

        Args:
            stage: The stage to run.
            data: Input data passed to the first hook.
            ctx: StageContext for this run.

        Returns:
            Transformed data after all hooks, or None if a hook aborts.
        """
        hooks = self._hooks.get(stage, [])
        timeout = self._config.stage_timeout_ms / 1000.0 if self._config.stage_timeout_ms else None

        for hook in hooks:
            attempt = 0
            max_attempts = self._config.retry_count if self._config.error_policy == "retry" else 1
            while attempt < max_attempts:
                try:
                    if timeout:
                        data = await asyncio.wait_for(hook(data, ctx), timeout=timeout)
                    else:
                        data = await hook(data, ctx)
                    if data is None:
                        logger.debug(
                            "Hook %s returned None at stage %s -- aborting run",
                            getattr(hook, "__name__", repr(hook)),
                            stage.value,
                        )
                        return None
                    break  # success
                except asyncio.TimeoutError:
                    logger.warning(
                        "Hook %s timed out at stage %s (timeout=%.1fs)",
                        getattr(hook, "__name__", repr(hook)),
                        stage.value,
                        timeout,
                    )
                    if self._config.error_policy == "abort":
                        raise
                    return data  # continue with last known data
                except Exception as exc:
                    attempt += 1
                    if attempt >= max_attempts or self._config.error_policy != "retry":
                        if self._config.error_policy == "abort":
                            raise
                        logger.error(
                            "Hook %s failed at stage %s: %s",
                            getattr(hook, "__name__", repr(hook)),
                            stage.value,
                            exc,
                        )
                        break  # continue pipeline with unmodified data
                    logger.warning(
                        "Hook %s failed (attempt %d/%d): %s -- retrying",
                        getattr(hook, "__name__", repr(hook)),
                        attempt,
                        max_attempts,
                        exc,
                    )
                    await asyncio.sleep(0.05 * attempt)

        return data

    @staticmethod
    async def _run_in_executor(loop: asyncio.AbstractEventLoop, fn: Callable, *args: Any) -> Any:
        """Run a blocking function in the default thread executor.

        Args:
            loop: The running event loop.
            fn: Blocking callable.
            *args: Arguments to pass to fn.

        Returns:
            Result of fn(*args).
        """
        return await loop.run_in_executor(None, fn, *args)


__all__ = [
    "HalOrchestrator",
    "OrchestratorConfig",
    "OrchestratorStage",
    "StageContext",
    "StageHook",
]
