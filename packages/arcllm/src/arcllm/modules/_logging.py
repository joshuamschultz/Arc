"""Shared structured logging helper for ArcLLM modules.

All modules call log_structured() instead of building log lines manually.
Format, sanitization, and output logic live here — one place to change.
"""

import logging
from typing import Any

from arcllm.exceptions import ArcLLMConfigError

_CONTROL_CHARS = str.maketrans({"\n": "\\n", "\r": "\\r", "\t": "\\t"})

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _sanitize(value: Any) -> str:
    """Escape control characters in string values to prevent log injection."""
    s = str(value)
    return s.translate(_CONTROL_CHARS)


def validate_log_level(config: dict[str, Any], default: str = "INFO") -> int:
    """Validate and convert log_level config to a Python logging constant.

    Args:
        config: Module configuration dict (may contain ``log_level`` key).
        default: Level name when ``log_level`` is absent.

    Returns:
        Python logging level constant (e.g. ``logging.INFO``).

    Raises:
        ArcLLMConfigError: If the level name is not a standard Python level.
    """
    log_level_name: str = config.get("log_level", default)
    if log_level_name not in _VALID_LOG_LEVELS:
        raise ArcLLMConfigError(
            f"Invalid log_level '{log_level_name}'. "
            f"Must be one of: {', '.join(sorted(_VALID_LOG_LEVELS))}"
        )
    return getattr(logging, log_level_name)


def log_structured(
    logger: logging.Logger,
    level: int,
    label: str,
    **fields: Any,
) -> None:
    """Emit a structured log line with key=value pairs.

    Args:
        logger: Module-specific logger instance.
        level: Python logging level (e.g., logging.INFO).
        label: Log line prefix (e.g., "LLM call", "Audit").
        **fields: Key-value pairs to log. None values are omitted.
            String values are sanitized against log injection.
    """
    if not logger.isEnabledFor(level):
        return

    parts = []
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str):
            parts.append(f"{key}={_sanitize(value)}")
        elif isinstance(value, float):
            parts.append(f"{key}={value:.6f}")
        else:
            parts.append(f"{key}={value}")

    logger.log(level, "%s | %s", label, " ".join(parts))
