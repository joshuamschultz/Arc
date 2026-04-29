"""Test that LocalBackend and DockerBackend satisfy the ExecutorBackend Protocol.

G3.2 deliverable: any class with the 4 required methods + name + capabilities
passes isinstance(obj, ExecutorBackend) via @runtime_checkable.
"""

from __future__ import annotations

from arcrun.backends import (
    BackendCapabilities,
    DockerBackend,
    ExecutorBackend,
    LocalBackend,
)


class TestProtocolConformance:
    """isinstance checks via @runtime_checkable Protocol."""

    def test_local_backend_is_executor_backend(self) -> None:
        backend = LocalBackend()
        assert isinstance(backend, ExecutorBackend)

    def test_docker_backend_is_executor_backend(self) -> None:
        backend = DockerBackend()
        assert isinstance(backend, ExecutorBackend)

    def test_arbitrary_conforming_class_passes_protocol(self) -> None:
        """Any class with the required interface satisfies the Protocol."""
        from collections.abc import AsyncIterator

        from arcrun.backends.base import ExecHandle

        class MinimalBackend:
            name = "minimal"
            capabilities = BackendCapabilities()

            async def run(  # type: ignore[override]
                self,
                command: str,
                *,
                cwd: str | None = None,
                env: dict[str, str] | None = None,
                timeout: float = 120.0,
                stdin: str | None = None,
            ) -> ExecHandle:
                return ExecHandle(handle_id="x", backend_name="minimal")

            async def stream(  # type: ignore[override]
                self, handle: ExecHandle
            ) -> AsyncIterator[bytes]:
                yield b""

            async def cancel(  # type: ignore[override]
                self, handle: ExecHandle, *, grace: float = 5.0
            ) -> None:
                pass

            async def close(self) -> None:
                pass

        assert isinstance(MinimalBackend(), ExecutorBackend)

    def test_object_missing_close_fails_protocol(self) -> None:
        """Missing close() means the class does NOT pass isinstance."""
        from collections.abc import AsyncIterator

        from arcrun.backends.base import ExecHandle

        class Incomplete:
            name = "broken"
            capabilities = BackendCapabilities()

            async def run(  # type: ignore[override]
                self, command: str, **kwargs: object
            ) -> ExecHandle:
                return ExecHandle(handle_id="x", backend_name="broken")

            async def stream(  # type: ignore[override]
                self, handle: ExecHandle
            ) -> AsyncIterator[bytes]:
                yield b""

            async def cancel(  # type: ignore[override]
                self, handle: ExecHandle, *, grace: float = 5.0
            ) -> None:
                pass

            # close() deliberately omitted

        # @runtime_checkable only checks for method presence, not signatures.
        # A class WITHOUT close() must NOT pass.
        assert not isinstance(Incomplete(), ExecutorBackend)


class TestCapabilitiesRoundtrip:
    """BackendCapabilities serialises and deserialises cleanly."""

    def test_local_capabilities_roundtrip(self) -> None:
        backend = LocalBackend()
        caps = backend.capabilities
        data = caps.model_dump()
        restored = BackendCapabilities.model_validate(data)
        assert restored == caps

    def test_docker_capabilities_roundtrip(self) -> None:
        backend = DockerBackend()
        caps = backend.capabilities
        data = caps.model_dump()
        restored = BackendCapabilities.model_validate(data)
        assert restored == caps

    def test_capabilities_json_roundtrip(self) -> None:
        caps = BackendCapabilities(
            supports_file_copy=True,
            supports_persistent_workspace=True,
            isolation="container",
            cold_start_budget_ms=800,
            max_stdout_bytes=65536,
        )
        restored = BackendCapabilities.model_validate_json(caps.model_dump_json())
        assert restored == caps
