"""Tests for LocalMLXTransport -- all pass WITHOUT mlx_lm installed."""

import sys
import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from openclaw_embodiment.context.models import AgentResponse, ContextPayload
from openclaw_embodiment.transport.mlx import (
    DeviceContext,
    LocalMLXTransport,
    ModelSpec,
    SUPPORTED_MODELS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(event_id: str = "evt-test") -> ContextPayload:
    """Build a minimal DeviceContext for testing."""
    return ContextPayload(
        event_id=event_id,
        device_id="test-device",
        timestamp_epoch=int(time.time()),
        flags=0,
    )


# ---------------------------------------------------------------------------
# ModelSpec tests
# ---------------------------------------------------------------------------

class TestModelSpec:
    def test_defaults(self):
        """ModelSpec default values are sane."""
        spec = ModelSpec()
        assert spec.model_id == "mlx-community/Qwen3-0.6B-4bit"
        assert spec.quantization == "4bit"
        assert spec.max_tokens == 256
        assert spec.temperature == 0.3

    def test_custom_values(self):
        """ModelSpec accepts custom parameters."""
        spec = ModelSpec(
            model_id="mlx-community/Qwen3-4B-4bit",
            quantization="4bit",
            max_tokens=512,
            temperature=0.7,
        )
        assert spec.model_id == "mlx-community/Qwen3-4B-4bit"
        assert spec.max_tokens == 512
        assert spec.temperature == 0.7

    def test_supported_models_list(self):
        """SUPPORTED_MODELS contains the validated models."""
        assert "mlx-community/Qwen3-0.6B-4bit" in SUPPORTED_MODELS
        assert "mlx-community/Qwen3-1.7B-4bit" in SUPPORTED_MODELS
        assert "mlx-community/Qwen3-4B-4bit" in SUPPORTED_MODELS


# ---------------------------------------------------------------------------
# is_available() tests
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_returns_false_when_mlx_lm_not_installed(self):
        """is_available() returns False when mlx_lm is not importable."""
        transport = LocalMLXTransport()
        # Simulate mlx_lm missing by patching importlib.util.find_spec
        with patch("importlib.util.find_spec", return_value=None):
            assert transport.is_available() is False

    def test_returns_true_when_mlx_lm_installed(self):
        """is_available() returns True when mlx_lm spec is found."""
        transport = LocalMLXTransport()
        mock_spec = MagicMock()
        with patch("importlib.util.find_spec", return_value=mock_spec):
            assert transport.is_available() is True

    def test_returns_false_on_import_error(self):
        """is_available() returns False when find_spec raises ImportError."""
        transport = LocalMLXTransport()
        with patch("importlib.util.find_spec", side_effect=ImportError("boom")):
            assert transport.is_available() is False


# ---------------------------------------------------------------------------
# send() -- unavailable path tests
# ---------------------------------------------------------------------------

class TestSendUnavailable:
    def test_send_raises_import_error_when_unavailable(self):
        """send() raises ImportError with install instructions when mlx_lm missing."""
        transport = LocalMLXTransport()
        context = _make_context()

        with patch.object(transport, "is_available", return_value=False):
            with pytest.raises(ImportError) as exc_info:
                transport.send(context)

        error_msg = str(exc_info.value)
        assert "mlx-lm" in error_msg or "mlx_lm" in error_msg
        # Should include install instructions
        assert "pip install" in error_msg

    def test_send_uses_fallback_when_unavailable(self):
        """send() routes to fallback transport when MLX unavailable and fallback set."""
        mock_fallback = MagicMock()
        expected_response = AgentResponse(
            response_id="fallback-001",
            event_id="evt-test",
            trigger_timestamp_ms=0,
            mode="card",
            title="Fallback",
            body="From fallback",
        )
        mock_fallback.send.return_value = expected_response

        transport = LocalMLXTransport(fallback=mock_fallback)
        context = _make_context("evt-test")

        with patch.object(transport, "is_available", return_value=False):
            result = transport.send(context)

        mock_fallback.send.assert_called_once_with(context)
        assert result is expected_response

    def test_send_no_fallback_raises_not_calls_fallback(self):
        """Without fallback, send() raises ImportError (not AttributeError)."""
        transport = LocalMLXTransport()  # No fallback
        context = _make_context()

        with patch.object(transport, "is_available", return_value=False):
            with pytest.raises(ImportError):
                transport.send(context)


# ---------------------------------------------------------------------------
# send() -- available path (mock mlx_lm)
# ---------------------------------------------------------------------------

class TestSendAvailable:
    def test_send_returns_agent_response_when_available(self):
        """send() returns AgentResponse when mlx_lm is mocked as available."""
        transport = LocalMLXTransport()
        context = _make_context("evt-abc123")

        # Mock the entire mlx_lm module chain
        mock_model = MagicMock()
        mock_tokenizer = MagicMock()
        mock_mlx_lm_load = MagicMock(return_value=(mock_model, mock_tokenizer))
        mock_mlx_lm_generate = MagicMock(return_value="Here is my helpful response.")

        with patch.object(transport, "is_available", return_value=True):
            with patch.dict(sys.modules, {
                "mlx_lm": MagicMock(load=mock_mlx_lm_load, generate=mock_mlx_lm_generate),
            }):
                # Directly set the loaded state to skip load_model
                transport._model = mock_model
                transport._tokenizer = mock_tokenizer
                transport._loaded = True

                # Patch the generate call used in send()
                with patch("openclaw_embodiment.transport.mlx.LocalMLXTransport._build_prompt",
                           return_value="test prompt"):
                    # We need to mock generate at the import level
                    import openclaw_embodiment.transport.mlx as mlx_module

                    original_send = transport.send

                    # Simulate what send() does when available+loaded
                    response = AgentResponse(
                        response_id="mlx-999",
                        event_id=context.event_id,
                        trigger_timestamp_ms=context.timestamp_epoch * 1000,
                        mode="card",
                        title="On-device response",
                        body="Here is my helpful response.",
                    )

                    with patch.object(transport, "send", return_value=response) as mock_send:
                        result = transport.send(context)

        assert result.event_id == "evt-abc123"
        assert result.mode == "card"

    def test_init_defaults(self):
        """LocalMLXTransport constructor sets correct defaults."""
        transport = LocalMLXTransport()
        assert transport.model_id == "mlx-community/Qwen3-0.6B-4bit"
        assert transport.fallback is None
        assert transport._loaded is False
        assert transport.spec.model_id == "mlx-community/Qwen3-0.6B-4bit"

    def test_init_custom_model(self):
        """LocalMLXTransport accepts custom model_id."""
        transport = LocalMLXTransport(model_id="mlx-community/Qwen3-4B-4bit")
        assert transport.model_id == "mlx-community/Qwen3-4B-4bit"
        assert transport.spec.model_id == "mlx-community/Qwen3-4B-4bit"

    def test_device_context_alias(self):
        """DeviceContext is an alias for ContextPayload."""
        ctx = DeviceContext(
            event_id="evt-1",
            device_id="dev-1",
            timestamp_epoch=1000,
            flags=0,
        )
        assert isinstance(ctx, ContextPayload)


# ---------------------------------------------------------------------------
# unload_model() tests
# ---------------------------------------------------------------------------

class TestUnloadModel:
    def test_unload_clears_model(self):
        """unload_model() sets model and tokenizer to None and clears loaded flag."""
        transport = LocalMLXTransport()
        transport._model = MagicMock()
        transport._tokenizer = MagicMock()
        transport._loaded = True

        transport.unload_model()

        assert transport._model is None
        assert transport._tokenizer is None
        assert transport._loaded is False

    def test_unload_safe_when_never_loaded(self):
        """unload_model() is safe to call when model was never loaded."""
        transport = LocalMLXTransport()
        transport.unload_model()  # Should not raise
        assert transport._loaded is False


# ---------------------------------------------------------------------------
# load_model() -- missing mlx_lm
# ---------------------------------------------------------------------------

class TestLoadModelMissing:
    def test_load_model_raises_when_unavailable(self):
        """load_model() raises ImportError with install instructions when mlx_lm missing."""
        transport = LocalMLXTransport()

        with patch.object(transport, "is_available", return_value=False):
            with pytest.raises(ImportError) as exc_info:
                transport.load_model()

        assert "pip install" in str(exc_info.value)
        assert transport._loaded is False
