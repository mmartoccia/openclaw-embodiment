"""SDK exception hierarchy."""

from typing import Any, Dict, Optional


class OpenClawWearableError(Exception):
    """Base SDK error with stable fields."""

    def __init__(self, message: str, error_code: str, remediation: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.remediation = remediation
        self.details = details or {}


class ConfigurationError(OpenClawWearableError):
    """Raised for invalid configuration."""


class HardwareError(OpenClawWearableError):
    """Raised for hardware failures."""


class TransportError(OpenClawWearableError):
    """Raised for transport/runtime wire failures."""


class IncompatibleHALError(OpenClawWearableError):
    """Raised when HAL major version is incompatible."""


class PayloadValidationError(OpenClawWearableError):
    """Base packet validation error."""


class PayloadMagicError(PayloadValidationError):
    """Raised when packet magic is invalid."""


class PayloadVersionError(PayloadValidationError):
    """Raised when packet version is unsupported."""


class PayloadSizeError(PayloadValidationError):
    """Raised when payload size is invalid."""


class PayloadTruncatedError(PayloadValidationError):
    """Raised when payload is truncated."""


class PayloadCRCError(PayloadValidationError):
    """Raised when CRC check fails."""


class PayloadFragmentTimeoutError(PayloadValidationError):
    """Raised on fragment timeout."""


class PayloadFragmentOrderError(PayloadValidationError):
    """Raised on fragment order/gap issues."""


class ContextAPIError(OpenClawWearableError):
    """Base context API error."""


class ContextNetworkError(ContextAPIError):
    """Raised for context network failures."""


class ContextAuthError(ContextAPIError):
    """Raised for authentication failures."""


class ContextRateLimitError(ContextAPIError):
    """Raised for context rate limits."""


class ContextServiceUnavailableError(ContextAPIError):
    """Raised when context service is unavailable."""


class PipelineRuntimeError(OpenClawWearableError):
    """Raised for pipeline runtime faults."""
