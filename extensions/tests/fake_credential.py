"""A stand-in for ``AccessTokenHandle`` that bundle tests hand to an attachment.

Records every call so a test can assert how many times the attachment asked for a
value and whether it invalidated after a provider 401.
"""

from __future__ import annotations

from arcagent.core.errors import ExtensionError
from arcagent.extension.secrets import Secret


class FakeCredentialHandle:
    """``bearer_values`` are served in order (the last one repeats); ``fields`` by name."""

    def __init__(
        self,
        bearer_values: list[str] | None = None,
        fields: dict[str, str] | None = None,
    ) -> None:
        self._bearers = list(bearer_values or [])
        self._fields = dict(fields or {})
        self.connection = "fake-connection"
        self.calls: list[str] = []
        self.bearer_calls = 0
        self.invalidations = 0

    async def bearer(self) -> Secret:
        self.calls.append("bearer")
        if not self._bearers:
            raise ExtensionError(code="CREDENTIAL_NO_BEARER", message="no bearer", details={})
        value = self._bearers[min(self.bearer_calls, len(self._bearers) - 1)]
        self.bearer_calls += 1
        return Secret(value)

    async def field(self, name: str) -> Secret:
        self.calls.append(f"field:{name}")
        if name not in self._fields:
            raise ExtensionError(
                code="CREDENTIAL_MISSING", message=f"no {name}", details={"field": name}
            )
        return Secret(self._fields[name])

    async def maybe_field(self, name: str) -> Secret | None:
        self.calls.append(f"maybe_field:{name}")
        value = self._fields.get(name)
        return None if value is None else Secret(value)

    async def invalidate(self) -> None:
        self.calls.append("invalidate")
        self.invalidations += 1
