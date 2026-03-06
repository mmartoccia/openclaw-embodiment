"""Tests for HalOrchestrator and OrchestratorConfig."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import pytest

from openclaw_embodiment.core.orchestrator import (
    HalOrchestrator,
    OrchestratorConfig,
    OrchestratorStage,
    StageContext,
)
from openclaw_embodiment.core.pipeline import EmbodimentSDK, HALRegistry
from openclaw_embodiment.core.trigger import TriggerEvent
from openclaw_embodiment.hal.simulator import (
    SimulatedCamera,
    SimulatedClassifier,
    SimulatedDisplay,
    SimulatedIMU,
    SimulatedMicrophone,
    SimulatedTransport,
)


def _make_registry() -> HALRegistry:
    reg = HALRegistry()
    imu = SimulatedIMU()
    imu.initialize(25)
    reg.register_imu(imu)
    cam = SimulatedCamera()
    cam.initialize((320, 240))
    reg.register_camera(cam)
    mic = SimulatedMicrophone()
    mic.initialize()
    mic.start_recording()
    reg.register_microphone(mic)
    tx = SimulatedTransport()
    tx.initialize({})
    tx.connect()
    reg.register_transport(tx, priority=0)
    disp = SimulatedDisplay()
    disp.initialize()
    reg.register_display(disp)
    return reg


def _make_trigger() -> TriggerEvent:
    ts = 1000
    return TriggerEvent(
        event_id=str(uuid.uuid4()),
        timestamp_ms=ts,
        timestamp_epoch=ts,
        trigger_confidence=0.9,
        head_pitch=5.0,
        head_yaw=2.0,
        head_roll=1.0,
    )


class TestOrchestratorStage:
    def test_all_stages_defined(self) -> None:
        stages = {s.value for s in OrchestratorStage}
        assert "trigger" in stages
        assert "capture" in stages
        assert "classify" in stages
        assert "transport" in stages
        assert "actuate" in stages
        assert "response" in stages


class TestStageContext:
    def test_creation(self) -> None:
        ctx = StageContext(
            stage=OrchestratorStage.TRIGGER,
            orchestrator_id="orch-1",
            device_id="dev-1",
            run_id="run-1",
            started_at=datetime.utcnow(),
        )
        assert ctx.stage == OrchestratorStage.TRIGGER
        assert ctx.metadata == {}


class TestOrchestratorConfig:
    def test_defaults(self) -> None:
        cfg = OrchestratorConfig()
        assert cfg.polling_hz == 25
        assert cfg.concurrent_triggers == 4
        assert cfg.stage_timeout_ms == 5000
        assert cfg.error_policy == "continue"
        assert cfg.metrics_enabled is True

    def test_custom_hooks(self) -> None:
        async def my_hook(data, ctx):
            return data

        cfg = OrchestratorConfig(hooks={OrchestratorStage.TRIGGER: [my_hook]})
        assert my_hook in cfg.hooks[OrchestratorStage.TRIGGER]


class TestHalOrchestrator:
    def test_instantiation(self) -> None:
        reg = _make_registry()
        orch = HalOrchestrator(registry=reg)
        assert orch is not None

    def test_run_once(self) -> None:
        """run_once should execute the full pipeline for a trigger event."""
        reg = _make_registry()
        orch = HalOrchestrator(registry=reg)
        trigger = _make_trigger()
        result = asyncio.run(orch.run_once(trigger))
        # Should return an AgentResponse (not None) since SimulatedClassifier is absent
        assert result is not None
        assert result.event_id == trigger.event_id

    def test_run_once_with_classifier_interesting(self) -> None:
        """run_once with an interesting classifier should complete pipeline."""
        reg = _make_registry()
        reg.register_classifier(SimulatedClassifier())
        orch = HalOrchestrator(registry=reg)
        trigger = _make_trigger()
        result = asyncio.run(orch.run_once(trigger))
        # SimulatedClassifier returns "interesting" -- pipeline completes
        assert result is not None

    def test_hook_is_called(self) -> None:
        """Hooks registered for TRIGGER stage must be called."""
        called = []

        async def capture_hook(data, ctx: StageContext):
            called.append(ctx.stage)
            return data

        cfg = OrchestratorConfig(hooks={OrchestratorStage.TRIGGER: [capture_hook]})
        reg = _make_registry()
        orch = HalOrchestrator(registry=reg, config=cfg)
        asyncio.run(orch.run_once(_make_trigger()))
        assert OrchestratorStage.TRIGGER in called

    def test_hook_abort_on_none(self) -> None:
        """A hook that returns None should abort the pipeline."""
        async def abort_hook(data, ctx):
            return None  # signal abort

        cfg = OrchestratorConfig(hooks={OrchestratorStage.TRIGGER: [abort_hook]})
        reg = _make_registry()
        orch = HalOrchestrator(registry=reg, config=cfg)
        result = asyncio.run(orch.run_once(_make_trigger()))
        assert result is None

    def test_add_hook(self) -> None:
        reg = _make_registry()
        orch = HalOrchestrator(registry=reg)
        called = []

        async def my_hook(data, ctx):
            called.append(True)
            return data

        orch.add_hook(OrchestratorStage.CAPTURE, my_hook)
        asyncio.run(orch.run_once(_make_trigger()))
        assert len(called) > 0

    def test_remove_hook(self) -> None:
        called = []

        async def my_hook(data, ctx):
            called.append(True)
            return data

        cfg = OrchestratorConfig(hooks={OrchestratorStage.CAPTURE: [my_hook]}, metrics_enabled=False)
        reg = _make_registry()
        orch = HalOrchestrator(registry=reg, config=cfg)
        orch.remove_hook(OrchestratorStage.CAPTURE, my_hook)
        asyncio.run(orch.run_once(_make_trigger()))
        assert len(called) == 0

    def test_remove_nonexistent_hook_raises(self) -> None:
        async def my_hook(data, ctx):
            return data

        reg = _make_registry()
        orch = HalOrchestrator(registry=reg)
        with pytest.raises(ValueError):
            orch.remove_hook(OrchestratorStage.TRANSPORT, my_hook)

    def test_hook_receives_context(self) -> None:
        contexts = []

        async def ctx_hook(data, ctx: StageContext):
            contexts.append(ctx)
            return data

        cfg = OrchestratorConfig(hooks={OrchestratorStage.TRIGGER: [ctx_hook]})
        reg = _make_registry()
        orch = HalOrchestrator(registry=reg, config=cfg)
        asyncio.run(orch.run_once(_make_trigger()))
        assert len(contexts) == 1
        assert contexts[0].stage == OrchestratorStage.TRIGGER
        assert contexts[0].orchestrator_id is not None

    def test_multiple_hooks_ordered(self) -> None:
        order = []

        async def hook_a(data, ctx):
            order.append("a")
            return data

        async def hook_b(data, ctx):
            order.append("b")
            return data

        cfg = OrchestratorConfig(
            hooks={OrchestratorStage.TRIGGER: [hook_a, hook_b]},
            metrics_enabled=False,
        )
        reg = _make_registry()
        orch = HalOrchestrator(registry=reg, config=cfg)
        asyncio.run(orch.run_once(_make_trigger()))
        assert order == ["a", "b"]


class TestEmbodimentSDKUseOrchestrator:
    def test_use_orchestrator_returns_hal_orchestrator(self) -> None:
        reg = _make_registry()
        sdk = EmbodimentSDK(registry=reg)
        orch = sdk.use_orchestrator()
        assert isinstance(orch, HalOrchestrator)

    def test_use_orchestrator_with_config(self) -> None:
        reg = _make_registry()
        sdk = EmbodimentSDK(registry=reg)
        cfg = OrchestratorConfig(polling_hz=10)
        orch = sdk.use_orchestrator(config=cfg)
        assert orch._config.polling_hz == 10

    def test_legacy_start_stop_still_works(self) -> None:
        """use_orchestrator() must not break legacy start/stop."""
        import time

        reg = _make_registry()
        sdk = EmbodimentSDK(registry=reg)
        # Ensure use_orchestrator doesn't side-effect start/stop
        _ = sdk.use_orchestrator()
        sdk.start()
        time.sleep(0.2)
        sdk.stop()
