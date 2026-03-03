"""Logging helpers for structured SDK logs."""

import logging
from typing import Optional


def setup_logging(level: str = "INFO") -> logging.Logger:
    """Configure root logger with predictable format and return package logger."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s module=%(name)s op=%(operation)s event_id=%(event_id)s device_id=%(device_id)s code=%(error_code)s elapsed_ms=%(elapsed_ms)s msg=%(message)s",
    )
    return logging.getLogger("openclaw_embodiment")


def log_extra(operation: str, event_id: str = "", device_id: str = "", error_code: str = "", elapsed_ms: int = 0) -> dict:
    """Create a consistent structured logging extra payload."""
    return {
        "operation": operation,
        "event_id": event_id,
        "device_id": device_id,
        "error_code": error_code,
        "elapsed_ms": elapsed_ms,
    }
