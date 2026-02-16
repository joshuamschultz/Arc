"""Request signing — HMAC-SHA256 default, ECDSA P-256 optional."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any, Protocol

from arcllm.exceptions import ArcLLMConfigError
from arcllm.types import Message, Tool


class RequestSigner(Protocol):
    """Protocol for request signing backends."""

    def sign(self, payload: bytes) -> str: ...


class HmacSigner:
    """HMAC-SHA256 request signer using stdlib."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def sign(self, payload: bytes) -> str:
        """Return hex-encoded HMAC-SHA256 signature."""
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()


def canonical_payload(
    messages: list[Message],
    tools: list[Tool] | None,
    model: str,
) -> bytes:
    """Serialize request content to deterministic canonical JSON bytes.

    Uses sorted keys and compact separators for determinism.
    """
    data: dict[str, Any] = {
        "messages": [m.model_dump() for m in messages],
        "model": model,
        "tools": [t.model_dump() for t in tools] if tools else [],
    }
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def create_signer(algorithm: str, signing_key_env: str) -> RequestSigner:
    """Factory: create signer from algorithm name and env var.

    Raises:
        ArcLLMConfigError: On missing env var, unsupported algorithm,
            or missing optional dependency.
    """
    key_value = os.environ.get(signing_key_env)
    if key_value is None:
        raise ArcLLMConfigError(
            f"Signing key environment variable '{signing_key_env}' not set"
        )

    if algorithm == "hmac-sha256":
        return HmacSigner(key=key_value.encode("utf-8"))

    if algorithm == "ecdsa-p256":
        try:
            import cryptography  # noqa: F401
        except ImportError:
            raise ArcLLMConfigError(
                f"signing_algorithm='{algorithm}' requires arcllm[signing] "
                "(pip install arcllm[signing])"
            )
        # ECDSA implementation would go here once cryptography is available
        raise ArcLLMConfigError(
            "ECDSA signing is available but not yet fully implemented"
        )

    raise ArcLLMConfigError(
        f"Unsupported signing algorithm: '{algorithm}'. "
        "Supported: 'hmac-sha256', 'ecdsa-p256'"
    )
