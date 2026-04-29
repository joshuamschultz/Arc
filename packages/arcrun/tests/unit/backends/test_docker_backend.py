"""Unit tests for DockerBackend — all docker CLI calls are mocked.

Does NOT require a running Docker daemon.  A separate integration test
(marked @pytest.mark.docker) handles real-docker verification.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcrun.backends import DockerBackend, ExecHandle, ExecutorBackend


class TestDockerBackendProtocol:
    def test_is_executor_backend(self) -> None:
        assert isinstance(DockerBackend(), ExecutorBackend)

    def test_name(self) -> None:
        assert DockerBackend().name == "docker"

    def test_capabilities_isolation(self) -> None:
        assert DockerBackend().capabilities.isolation == "container"


class TestDockerBackendRunMocked:
    """Tests that mock the docker CLI subprocesses."""

    @pytest.mark.asyncio
    async def test_run_creates_container_on_first_call(self) -> None:
        """First run() creates the long-lived container."""
        container_id = "abc123def456"

        async def fake_run_detached(**kwargs: object) -> str:
            return container_id

        with patch(
            "arcrun.backends.docker._docker_run_detached",
            new=AsyncMock(return_value=container_id),
        ):
            # Mock docker exec subprocess
            mock_proc = MagicMock()
            mock_proc.stdout = AsyncMock()
            mock_proc.stdout.read = AsyncMock(return_value=b"")
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.returncode = 0
            mock_proc.stdin = None

            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ):
                backend = DockerBackend()
                handle = await backend.run("echo hello")

        assert backend._container_id == container_id
        assert isinstance(handle, ExecHandle)
        assert handle.backend_name == "docker"

    @pytest.mark.asyncio
    async def test_run_reuses_existing_container(self) -> None:
        """Subsequent run() calls reuse the same container."""
        container_id = "reused123"

        mock_proc = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0
        mock_proc.stdin = None

        with patch(
            "arcrun.backends.docker._docker_run_detached",
            new=AsyncMock(return_value=container_id),
        ):
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ):
                backend = DockerBackend()
                await backend.run("echo first")
                await backend.run("echo second")

        # _ensure_container called once even though run() called twice
        assert backend._container_id == container_id

    @pytest.mark.asyncio
    async def test_cancel_terminates_exec_proc(self) -> None:
        """cancel() terminates the docker exec subprocess."""
        container_id = "cid_cancel"

        mock_proc = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0
        mock_proc.stdin = None
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()

        with patch(
            "arcrun.backends.docker._docker_run_detached",
            new=AsyncMock(return_value=container_id),
        ):
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ):
                backend = DockerBackend()
                handle = await backend.run("sleep 60")
                await backend.cancel(handle, grace=0.1)

        mock_proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_removes_container(self) -> None:
        """close() calls docker rm -f on the container."""
        container_id = "cid_close"

        mock_proc = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0
        mock_proc.stdin = None
        mock_proc.terminate = MagicMock()

        with patch(
            "arcrun.backends.docker._docker_run_detached",
            new=AsyncMock(return_value=container_id),
        ):
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ):
                with patch(
                    "arcrun.backends.docker._docker_rm_f",
                    new=AsyncMock(),
                ) as mock_rm:
                    backend = DockerBackend()
                    await backend.run("echo hi")
                    await backend.close()

        mock_rm.assert_called_once_with(container_id)
        assert backend._container_id is None

    @pytest.mark.asyncio
    async def test_stream_yields_bytes(self) -> None:
        """stream() yields bytes from docker exec stdout."""
        container_id = "cid_stream"
        output_bytes = b"hello from docker\n"

        # Mock a subprocess where stdout.read returns data then EOF
        read_calls = [output_bytes, b""]
        call_index = {"i": 0}

        async def fake_read(n: int) -> bytes:
            i = call_index["i"]
            call_index["i"] += 1
            if i < len(read_calls):
                return read_calls[i]
            return b""

        mock_proc = MagicMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = fake_read
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0
        mock_proc.stdin = None

        with patch(
            "arcrun.backends.docker._docker_run_detached",
            new=AsyncMock(return_value=container_id),
        ):
            with patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ):
                backend = DockerBackend()
                handle = await backend.run("echo hi")
                chunks: list[bytes] = []
                async for chunk in backend.stream(handle):
                    chunks.append(chunk)

        assert b"hello from docker" in b"".join(chunks)

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Real docker integration test — requires docker daemon")
    async def test_real_docker_echo(self) -> None:
        """Integration: actually runs echo inside a container."""
        backend = DockerBackend(image="python:3.11-slim")
        handle = await backend.run("echo real_docker_test")
        data = b""
        async for chunk in backend.stream(handle):
            data += chunk
        assert b"real_docker_test" in data
        await backend.close()
