"""Neutral authenticated byte sealing contract for persistent records."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ByteCipher(Protocol):
    """Seal bytes into an opaque transport string and open them again."""

    def seal(self, payload: bytes) -> str: ...

    def open(self, sealed: str) -> bytes: ...
