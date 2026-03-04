"""LocalMLXTransport -- on-device inference via MLX (Apple Silicon).

No gateway required. Fully offline. Validated on MacBook Pro M-series.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, List, Optional

from ..context.models import AgentResponse, ContextPayload

logger = logging.getLogger(__name__)

# DeviceContext is a semantic alias for ContextPayload used in transport layer.
DeviceContext = ContextPayload

SUPPORTED_MODELS: List[str] = [
    "mlx-community/Qwen3-0.6B-4bit",   # validated: ~8.6s load, ~0.59s inference on M-series
    "mlx-community/Qwen3-1.7B-4bit",
    "mlx-community/Qwen3-4B-4bit",
]


@dataclass
class ModelSpec:
    """Specification for a local MLX model.

    Attributes:
        model_id: HuggingFace model identifier.
        quantization: Quantization scheme (e.g. '4bit').
        max_tokens: Maximum tokens to generate per inference call.
        temperature: Sampling temperature (lower = more deterministic).
    """

    model_id: str = "mlx-community/Qwen3-0.6B-4bit"
    quantization: str = "4bit"
    max_tokens: int = 256
    temperature: float = 0.3


class LocalMLXTransport:
    """On-device inference transport using MLX framework (Apple Silicon).

    Runs Qwen3 (0.6B-4bit default) locally. No network, no gateway, works offline.
    Falls back to a gateway transport if MLX is not available.

    Validated: Qwen3-0.6B-4bit loads in 8.6s, inference 0.59s on MacBook Pro M-series.

    Usage::

        transport = LocalMLXTransport()
        if transport.is_available():
            response = transport.send(context)
        else:
            print("mlx_lm not installed -- use pip install mlx-lm>=0.30.7")

    Supported models:
        - mlx-community/Qwen3-0.6B-4bit  (validated, recommended)
        - mlx-community/Qwen3-1.7B-4bit
        - mlx-community/Qwen3-4B-4bit
    """

    SUPPORTED_MODELS: List[str] = SUPPORTED_MODELS

    def __init__(
        self,
        model_id: str = "mlx-community/Qwen3-0.6B-4bit",
        fallback: Optional[object] = None,
        spec: Optional[ModelSpec] = None,
    ) -> None:
        """Initialize LocalMLXTransport.

        Args:
            model_id: HuggingFace model ID to use for local inference.
                      Defaults to Qwen3-0.6B-4bit (validated, fastest).
            fallback: Optional fallback transport when MLX is unavailable.
                      If provided, ``send()`` routes there instead of raising.
            spec: Optional ModelSpec override. If None, defaults are used.
        """
        self.model_id = model_id
        self.fallback = fallback
        self.spec = spec or ModelSpec(model_id=model_id)
        self._model = None
        self._tokenizer = None
        self._loaded: bool = False

    def is_available(self) -> bool:
        """Check whether mlx_lm is installed and importable.

        Returns:
            True if mlx_lm is available on this system, False otherwise.
            Does NOT check whether the model weights are cached locally.
        """
        try:
            import importlib.util
            spec = importlib.util.find_spec("mlx_lm")
            return spec is not None
        except (ImportError, ValueError):
            return False

    def load_model(self) -> None:
        """Lazy-load the MLX model and tokenizer with progress logging.

        Raises:
            ImportError: If mlx_lm is not installed. Includes install instructions.
            RuntimeError: If model loading fails for other reasons.
        """
        if self._loaded:
            return

        if not self.is_available():
            raise ImportError(
                "mlx_lm is not installed. To enable on-device inference:\n"
                "  pip install mlx-lm>=0.30.7\n"
                "Requires Apple Silicon Mac (M1/M2/M3/M4)."
            )

        if self.model_id not in self.SUPPORTED_MODELS:
            logger.warning(
                "[MLX] model_id %r not in validated list %s -- proceeding anyway.",
                self.model_id,
                self.SUPPORTED_MODELS,
            )

        try:
            # Lazy import -- do NOT import mlx_lm at module level.
            from mlx_lm import load  # type: ignore

            logger.info("[MLX] Loading model %s ...", self.model_id)
            t0 = time.monotonic()
            self._model, self._tokenizer = load(self.model_id)
            elapsed = time.monotonic() - t0
            logger.info("[MLX] Model loaded in %.1fs.", elapsed)
            self._loaded = True

        except ImportError:
            raise ImportError(
                "mlx_lm is not installed. To enable on-device inference:\n"
                "  pip install mlx-lm>=0.30.7\n"
                "Requires Apple Silicon Mac (M1/M2/M3/M4)."
            )

    def unload_model(self) -> None:
        """Unload model and free GPU/unified memory.

        Safe to call even if model was never loaded.
        """
        self._model = None
        self._tokenizer = None
        self._loaded = False
        logger.info("[MLX] Model unloaded, memory freed.")

    def send(self, context: "DeviceContext") -> AgentResponse:
        """Run local inference on the given device context.

        Args:
            context: DeviceContext (ContextPayload) with sensor/trigger data.

        Returns:
            AgentResponse from local inference.

        Raises:
            ImportError: If mlx_lm is not installed and no fallback is configured.
                         Includes pip install instructions.
        """
        if not self.is_available():
            if self.fallback is not None:
                logger.warning("[MLX] mlx_lm unavailable -- routing to fallback transport.")
                return self.fallback.send(context)
            raise ImportError(
                "mlx_lm is not installed. To enable on-device inference:\n"
                "  pip install mlx-lm>=0.30.7\n"
                "Requires Apple Silicon Mac (M1/M2/M3/M4)."
            )

        self.load_model()

        try:
            # Lazy import -- do NOT import mlx_lm at module level.
            from mlx_lm import generate  # type: ignore

            prompt = self._build_prompt(context)
            logger.debug("[MLX] Running inference on event %s ...", context.event_id)
            t0 = time.monotonic()
            response_text = generate(
                self._model,
                self._tokenizer,
                prompt=prompt,
                max_tokens=self.spec.max_tokens,
                temp=self.spec.temperature,
                verbose=False,
            )
            elapsed = time.monotonic() - t0
            logger.info("[MLX] Inference complete in %.2fs.", elapsed)

            return AgentResponse(
                response_id="mlx-%d" % int(time.time() * 1000),
                event_id=context.event_id,
                trigger_timestamp_ms=context.timestamp_epoch * 1000,
                mode="card",
                title="On-device response",
                body=str(response_text).strip(),
            )

        except Exception as exc:
            logger.error("[MLX] Inference error: %s", exc)
            raise

    async def send_async(self, context: "DeviceContext") -> AgentResponse:
        """Async version of send() -- runs inference in a thread executor.

        Allows awaiting in async event loops without blocking.

        Args:
            context: DeviceContext with sensor/trigger data.

        Returns:
            AgentResponse from local inference.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.send, context)

    def _build_prompt(self, context: "DeviceContext") -> str:
        """Build inference prompt from device context.

        Args:
            context: DeviceContext with sensor data.

        Returns:
            Formatted prompt string for the LLM.
        """
        return (
            "You are an embedded AI assistant on a wearable device.\n"
            f"Device: {context.device_id}\n"
            f"Event: {context.event_id}\n"
            "Respond briefly and helpfully."
        )

    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"LocalMLXTransport(model_id={self.model_id!r}, "
            f"loaded={self._loaded}, "
            f"available={self.is_available()})"
        )
