"""Shared utilities for web provider adapters.

Centralises constants and helpers used across multiple provider
implementations to eliminate duplication and make magic numbers explicit.
"""

from __future__ import annotations

import httpx

# Maximum number of bytes included from an HTTP error response body.
# Kept short to avoid leaking large HTML error pages into structured errors
# that flow up to the LLM (LLM02 — sensitive information disclosure).
_MAX_ERROR_TEXT_BYTES: int = 200


def format_http_error(exc: httpx.HTTPStatusError) -> str:
    """Return a compact, truncated description of an HTTP status error.

    Args:
        exc: The ``httpx.HTTPStatusError`` to describe.

    Returns:
        String of the form ``"HTTP <status>: <truncated body>"``.
    """
    return f"HTTP {exc.response.status_code}: {exc.response.text[:_MAX_ERROR_TEXT_BYTES]}"


__all__ = ["_MAX_ERROR_TEXT_BYTES", "format_http_error"]
