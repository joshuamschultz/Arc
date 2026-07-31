"""Architecture test: ExecutorBackend Protocol duck-typing (G3.2 deliverable).

Any class that implements the 4 required methods + name + capabilities
must pass isinstance(obj, ExecutorBackend) via @runtime_checkable.

This test proves the Protocol contract is structurally correct and that
third-party backends (ssh, modal, daytona) shipped as separate packages
will slot in without inheriting from any Arc base class.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from arcrun.backends import BackendCapabilities, ExecutorBackend
from arcrun.backends.base import ExecHandle

# ---------------------------------------------------------------------------
# Minimal duck-typed backend (no inheritance from arcrun)
# ---------------------------------------------------------------------------


class _MinimalDuckBackend:
    """Bare minimum implementation — no Arc base class, no imports beyond typing."""

    name: str = "duck"
    capabilities: BackendCapabilities = BackendCapabilities()

    async def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 120.0,
        stdin: str | None = None,
    ) -> ExecHandle:
        return ExecHandle(handle_id="duck-1", backend_name="duck")

    async def stream(self, handle: ExecHandle) -> AsyncIterator[bytes]:  # type: ignore[override]
        yield b"duck output"

    async def cancel(self, handle: ExecHandle, *, grace: float = 5.0) -> None:
        pass

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Variants that should NOT pass
# ---------------------------------------------------------------------------


class _MissingClose:
    name: str = "bad"
    capabilities: BackendCapabilities = BackendCapabilities()

    async def run(self, command: str, **kw: object) -> ExecHandle:  # type: ignore[override]
        return ExecHandle(handle_id="x", backend_name="bad")

    async def stream(self, h: ExecHandle) -> AsyncIterator[bytes]:  # type: ignore[override]
        yield b""

    async def cancel(self, h: ExecHandle, *, grace: float = 5.0) -> None:
        pass

    # close() missing


class _MissingCapabilities:
    name: str = "bad2"

    async def run(self, command: str, **kw: object) -> ExecHandle:  # type: ignore[override]
        return ExecHandle(handle_id="x", backend_name="bad2")

    async def stream(self, h: ExecHandle) -> AsyncIterator[bytes]:  # type: ignore[override]
        yield b""

    async def cancel(self, h: ExecHandle, *, grace: float = 5.0) -> None:
        pass

    async def close(self) -> None:
        pass

    # capabilities missing


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_minimal_duck_backend_passes_isinstance() -> None:
    """Core G3.2: a class with the right shape satisfies the Protocol."""
    obj = _MinimalDuckBackend()
    assert isinstance(obj, ExecutorBackend)


def test_missing_close_fails_isinstance() -> None:
    """Without close() the Protocol check fails."""
    obj = _MissingClose()
    assert not isinstance(obj, ExecutorBackend)


def test_missing_capabilities_fails_isinstance() -> None:
    """Without capabilities attribute the Protocol check fails."""
    obj = _MissingCapabilities()
    assert not isinstance(obj, ExecutorBackend)


def test_local_backend_satisfies_protocol() -> None:
    from arcrun.backends import LocalBackend

    assert isinstance(LocalBackend(), ExecutorBackend)


def test_docker_backend_satisfies_protocol() -> None:
    from arcrun.backends import DockerBackend

    assert isinstance(DockerBackend(), ExecutorBackend)


def test_protocol_is_runtime_checkable() -> None:
    """Verify the Protocol has the @runtime_checkable marker."""
    # If not runtime_checkable, isinstance would raise TypeError.
    # Simply calling isinstance without TypeError proves the marker is present.
    try:
        isinstance(object(), ExecutorBackend)
    except TypeError:
        pytest.fail("ExecutorBackend Protocol is not @runtime_checkable")


def test_multiple_independent_implementations_all_pass() -> None:
    """Three independent backend implementations all satisfy the Protocol."""
    from arcrun.backends import DockerBackend, LocalBackend

    backends: list[object] = [
        LocalBackend(),
        DockerBackend(),
        _MinimalDuckBackend(),
    ]
    for backend in backends:
        assert isinstance(backend, ExecutorBackend), f"{type(backend)} failed Protocol check"
