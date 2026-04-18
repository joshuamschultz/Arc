"""VaultBackend Protocol — the single interface every secret backend must satisfy.

All backends (Azure KV, HashiCorp Vault, AWS Secrets Manager, file, env) implement
this Protocol so the tier-driven resolver can treat them uniformly.

Design decisions:
- Async ``get_secret`` is the only required method; backends that wrap synchronous
  SDKs run them in a thread pool via ``asyncio.to_thread``.
- ``VaultUnreachable`` is the canonical exception for network/auth failures so the
  resolver can distinguish "could not connect" from "secret not found".
- Protocol is ``@runtime_checkable`` to allow ``isinstance`` guards in tests and
  the config loader; the static type checker still enforces the interface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class VaultUnreachable(Exception):  # noqa: N818 — domain convention; peers use Unreachable for connectivity errors
    """Raised when a vault backend cannot be contacted.

    Distinct from "secret not found" (KeyError / None return). Callers that
    receive this should apply tier policy: federal → hard error, enterprise →
    warn + env fallback, personal → env / file fallback.
    """


@runtime_checkable
class VaultBackend(Protocol):
    """Async interface every secret backend must satisfy.

    A backend is responsible only for fetching a named secret.  Caching,
    fallback logic, and tier policy are handled by the resolver and cache
    layers above it.

    Raises:
        VaultUnreachable: When the backend is not contactable (network error,
            authentication failure, service unavailable).  Should NOT be raised
            for missing secrets — return ``None`` instead.
    """

    async def get_secret(self, path: str) -> str | None:
        """Retrieve a secret by name/path.

        Args:
            path: Secret identifier (e.g., ``"openai-api-key"`` or
                ``"myapp/database/password"``).

        Returns:
            Secret value as a string, or ``None`` if the secret does not exist
            in this backend.

        Raises:
            VaultUnreachable: If the backend cannot be reached.
        """
        ...


__all__ = ["VaultBackend", "VaultUnreachable"]
