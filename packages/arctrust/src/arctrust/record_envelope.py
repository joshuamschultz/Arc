"""Authenticated record-envelope seam shared by local and external custody."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class RecordEnvelopeCipher(Protocol):
    """Seal and open one sensitive record field without exposing a key."""

    def seal(self, event: dict[str, Any]) -> dict[str, Any]: ...

    def unseal(self, event: Mapping[str, Any]) -> dict[str, Any]: ...
