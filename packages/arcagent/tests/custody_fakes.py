"""Shared fakes for P18-2 credential custody tests.

Concurrency tests force interleaving at named points (between a read and the
``update_if`` that depends on it) with ``asyncio.Barrier``/``Event``, never with
sleeps: an instant fake never interleaves on its own.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from arcstore.backends.memory import FakeBackend
from arctrust.connector_cipher import ConnectorSecretCipher

from arcagent.extension.secrets import Secret


def make_cipher(label: str = "test") -> ConnectorSecretCipher:
    """A deterministic in-process custody cipher for tests (never a real operator key)."""
    return ConnectorSecretCipher(hashlib.blake2b(label.encode(), digest_size=32).digest())


Hook = Callable[[str, str], Awaitable[None]]


class InterleavingBackend(FakeBackend):
    """A FakeBackend that can pause at the read and at the CAS of one collection.

    ``before_read`` / ``before_update_if`` are awaited (with ``(collection, key)``)
    before the real operation, so a test can hold two writers at the same point.
    """

    def __init__(self) -> None:
        super().__init__()
        self.before_read: Hook | None = None
        self.before_update_if: Hook | None = None
        self.update_if_calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = []

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        if self.before_read is not None:
            await self.before_read(collection, key)
        return await super().mutable_read(collection, key)

    async def update_if(
        self,
        collection: str,
        key: str,
        patch: dict[str, Any],
        where: dict[str, Any],
        **kwargs: Any,
    ) -> bool:
        self.update_if_calls.append((collection, key, dict(patch), dict(where)))
        if self.before_update_if is not None:
            await self.before_update_if(collection, key)
        return await super().update_if(collection, key, patch, where, **kwargs)


def once_per_task(gate: Callable[[], Awaitable[None]], *, collection: str) -> Hook:
    """A hook that runs ``gate`` the first time each task reaches it for ``collection``."""
    seen: set[int] = set()

    async def hook(name: str, _key: str) -> None:
        task = asyncio.current_task()
        ident = id(task)
        if name != collection or ident in seen:
            return
        seen.add(ident)
        await gate()

    return hook


class FakeCredentialHandle:
    """A duck-typed ``AccessTokenHandle`` serving fixed sensitive fields by name."""

    def __init__(self, fields: dict[str, str]) -> None:
        self._fields = dict(fields)
        self.connection = "fake-connection"

    async def field(self, name: str) -> Secret:
        if name not in self._fields:
            raise KeyError(name)
        return Secret(self._fields[name])

    async def maybe_field(self, name: str) -> Secret | None:
        value = self._fields.get(name)
        return None if value is None else Secret(value)

    def __repr__(self) -> str:
        return f"FakeCredentialHandle({self.connection})"


__all__ = ["FakeCredentialHandle", "InterleavingBackend", "make_cipher", "once_per_task"]
