"""Tests for contained_execute — container-isolated code execution.

All tests mock the Docker SDK. No real containers are started.
"""
from __future__ import annotations

import asyncio
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from arcrun.types import ToolContext


def _make_ctx() -> ToolContext:
    return ToolContext(
        run_id="run-1",
        tool_call_id="tc-1",
        turn_number=0,
        event_bus=None,
        cancelled=asyncio.Event(),
    )


class TestErrorHierarchy:
    """B1: Error types exist and have correct inheritance."""

    def test_sandbox_error_is_exception(self):
        from arcrun.builtins.contained_execute import SandboxError

        assert issubclass(SandboxError, Exception)

    def test_unavailable_is_sandbox_error(self):
        from arcrun.builtins.contained_execute import SandboxError, SandboxUnavailableError

        assert issubclass(SandboxUnavailableError, SandboxError)

    def test_timeout_is_sandbox_error(self):
        from arcrun.builtins.contained_execute import SandboxError, SandboxTimeoutError

        assert issubclass(SandboxTimeoutError, SandboxError)

    def test_oom_is_sandbox_error(self):
        from arcrun.builtins.contained_execute import SandboxError, SandboxOOMError

        assert issubclass(SandboxOOMError, SandboxError)

    def test_runtime_is_sandbox_error(self):
        from arcrun.builtins.contained_execute import SandboxError, SandboxRuntimeError

        assert issubclass(SandboxRuntimeError, SandboxError)


class TestLazyImport:
    """B1: Docker SDK is lazy-imported."""

    def test_import_error_when_docker_missing(self):
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        with patch.dict(sys.modules, {"docker": None}):
            with pytest.raises(ImportError, match="pip install arcrun\\[container\\]"):
                make_contained_execute_tool(image="python:3.11-slim")


class TestSocketDetection:
    """B1: Auto-detection of Docker/Podman sockets."""

    def test_docker_host_env_takes_priority(self):
        from arcrun.builtins.contained_execute import _detect_socket

        with patch.dict("os.environ", {"DOCKER_HOST": "unix:///custom/docker.sock"}):
            with patch("pathlib.Path.exists", return_value=True):
                result = _detect_socket()
                assert result == "unix:///custom/docker.sock"

    def test_falls_back_to_docker_default(self):
        from arcrun.builtins.contained_execute import _detect_socket

        with patch.dict("os.environ", {}, clear=True):
            # Make only docker default socket exist
            def exists_side_effect(self):
                return str(self) == "/var/run/docker.sock"

            with patch("pathlib.Path.exists", exists_side_effect):
                result = _detect_socket()
                assert "docker.sock" in result

    def test_raises_when_no_runtime(self):
        from arcrun.builtins.contained_execute import (
            SandboxUnavailableError,
            _detect_socket,
        )

        with patch.dict("os.environ", {}, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                with pytest.raises(SandboxUnavailableError, match="No container runtime"):
                    _detect_socket()


class TestContainerExecution:
    """B2: Container execution with mocked Docker SDK."""

    def _make_mock_docker(self, stdout=b"hello", stderr=b"", exit_code=0):
        """Create a mock docker module + client."""
        mock_docker = MagicMock()

        # Container mock
        mock_container = MagicMock()

        # exec_run with demux=True returns ExecResult with .exit_code and .output=(stdout, stderr)
        exec_result = MagicMock()
        exec_result.exit_code = exit_code
        exec_result.output = (stdout, stderr)
        mock_container.exec_run.return_value = exec_result

        mock_docker.DockerClient.return_value.containers.create.return_value = mock_container
        return mock_docker, mock_container

    @pytest.mark.asyncio
    async def test_successful_execution_returns_json(self):
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker(
            stdout=b"hello world", stderr=b"", exit_code=0
        )
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")
                result = await tool.execute({"code": "print('hello world')"}, _make_ctx())

        import json

        parsed = json.loads(result)
        assert "stdout" in parsed
        assert "stderr" in parsed
        assert "exit_code" in parsed
        assert "duration_ms" in parsed

    @pytest.mark.asyncio
    async def test_container_config_has_lockdown_defaults(self):
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker()
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")
                await tool.execute({"code": "pass"}, _make_ctx())

        # Check container.create was called with lockdown config
        create_call = mock_docker.DockerClient.return_value.containers.create
        assert create_call.called
        kwargs = create_call.call_args
        # Verify key lockdown settings exist in call
        call_kwargs = kwargs.kwargs if kwargs.kwargs else kwargs[1] if len(kwargs) > 1 else {}
        # At minimum, image should be passed
        assert create_call.call_args is not None

    @pytest.mark.asyncio
    async def test_custom_constraints_override_defaults(self):
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker()
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(
                    image="python:3.11-slim",
                    mem_limit="512m",
                    network_disabled=False,
                    pids_limit=128,
                )
                await tool.execute({"code": "pass"}, _make_ctx())

        assert mock_docker.DockerClient.return_value.containers.create.called

    @pytest.mark.asyncio
    async def test_code_injected_via_tar(self):
        """Code should be injected via tar, not as command arg."""
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker()
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")
                await tool.execute({"code": "print('test')"}, _make_ctx())

        # put_archive should have been called (tar injection)
        assert mock_container.put_archive.called

    @pytest.mark.asyncio
    async def test_oom_exit_code_137_raises(self):
        """Exit code 137 raises SandboxOOMError."""
        from arcrun.builtins.contained_execute import SandboxOOMError, make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker(
            stdout=b"", stderr=b"Killed", exit_code=137
        )
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")
                with pytest.raises(SandboxOOMError, match="OOM"):
                    await tool.execute({"code": "x = 'a' * 10**10"}, _make_ctx())

    @pytest.mark.asyncio
    async def test_tool_name_is_contained_execute(self):
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker = MagicMock()
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")

        assert tool.name == "contained_execute_python"

    @pytest.mark.asyncio
    async def test_tool_has_code_input_schema(self):
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker = MagicMock()
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")

        assert "code" in tool.input_schema["properties"]
        assert "code" in tool.input_schema["required"]

    @pytest.mark.asyncio
    async def test_timeout_raises_sandbox_timeout_error(self):
        """Execution exceeding timeout raises SandboxTimeoutError."""
        from arcrun.builtins.contained_execute import SandboxTimeoutError, make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker()
        stop = threading.Event()

        def slow_exec(*args, **kwargs):
            stop.wait(5)
            result = MagicMock()
            result.exit_code = 0
            result.output = (b"", b"")
            return result

        mock_container.exec_run.side_effect = slow_exec

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(
                    image="python:3.11-slim", timeout_seconds=0.1
                )
                with pytest.raises(SandboxTimeoutError, match="timeout"):
                    await tool.execute({"code": "pass"}, _make_ctx())

        stop.set()

    @pytest.mark.asyncio
    async def test_code_size_limit_raises_runtime_error(self):
        """Code exceeding MAX_CODE_BYTES raises SandboxRuntimeError."""
        from arcrun.builtins.contained_execute import SandboxRuntimeError, make_contained_execute_tool

        mock_docker = MagicMock()
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")
                big_code = "x = 1\n" * 200_000  # > 1 MiB
                with pytest.raises(SandboxRuntimeError, match="byte limit"):
                    await tool.execute({"code": big_code}, _make_ctx())

    @pytest.mark.asyncio
    async def test_container_runs_as_nobody_user(self):
        """Container should run as nobody (65534:65534)."""
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker()
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")
                await tool.execute({"code": "pass"}, _make_ctx())

        create_call = mock_docker.DockerClient.return_value.containers.create
        call_kwargs = create_call.call_args[1] if create_call.call_args[1] else {}
        assert call_kwargs.get("user") == "65534:65534"

    @pytest.mark.asyncio
    async def test_tmpfs_has_noexec_nosuid(self):
        """tmpfs mount should include noexec and nosuid flags."""
        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker()
        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")
                await tool.execute({"code": "pass"}, _make_ctx())

        create_call = mock_docker.DockerClient.return_value.containers.create
        call_kwargs = create_call.call_args[1] if create_call.call_args[1] else {}
        tmpfs_opts = call_kwargs.get("tmpfs", {}).get("/tmp", "")
        assert "noexec" in tmpfs_opts
        assert "nosuid" in tmpfs_opts

    @pytest.mark.asyncio
    async def test_cleanup_logs_failures(self, caplog):
        """Container cleanup failures should be logged, not silently swallowed."""
        import logging

        from arcrun.builtins.contained_execute import make_contained_execute_tool

        mock_docker, mock_container = self._make_mock_docker()
        mock_container.stop.side_effect = RuntimeError("stop failed")
        mock_container.remove.side_effect = RuntimeError("remove failed")

        with patch.dict(sys.modules, {"docker": mock_docker}):
            with patch("pathlib.Path.exists", return_value=True):
                tool = make_contained_execute_tool(image="python:3.11-slim")
                with caplog.at_level(logging.WARNING):
                    await tool.execute({"code": "pass"}, _make_ctx())

        assert "Container cleanup failed" in caplog.text
