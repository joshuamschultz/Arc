"""Tests for LocalBackend.run_separated() — separate stdout/stderr streams.

Verifies that run_separated() correctly isolates stdout from stderr,
preserves real exit codes, and handles timeouts — all without merging
the two channels together.

This test file is the proof-of-requirement for Goal A of the M3 gap-close:
LocalBackend can be used by execute.py while preserving the
test_stderr_capture / test_exit_code_on_failure semantics.
"""

from __future__ import annotations

import pytest

from arcrun.backends.local import LocalBackend, SeparatedResult


@pytest.mark.asyncio
async def test_separated_stdout_and_stderr_are_distinct() -> None:
    """Stdout and stderr content arrive in separate fields, not mixed."""
    backend = LocalBackend()
    result = await backend.run_separated("bash -c 'echo OUT_MARKER; echo ERR_MARKER >&2'")
    assert isinstance(result, SeparatedResult)
    assert b"OUT_MARKER" in result.stdout
    assert b"ERR_MARKER" in result.stderr
    # stdout must NOT contain the stderr marker and vice versa
    assert b"ERR_MARKER" not in result.stdout
    assert b"OUT_MARKER" not in result.stderr


@pytest.mark.asyncio
async def test_separated_exit_code_zero_on_success() -> None:
    backend = LocalBackend()
    result = await backend.run_separated("true")
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_separated_exit_code_nonzero_on_failure() -> None:
    """Non-zero exit code is preserved, matching test_exit_code_on_failure contract."""
    backend = LocalBackend()
    result = await backend.run_separated("false")
    assert result.exit_code != 0


@pytest.mark.asyncio
async def test_separated_stderr_captured_without_stdout_contamination() -> None:
    """Mirrors test_stderr_capture: stderr written by a script appears in stderr only."""
    backend = LocalBackend()
    # Use python to write to stderr
    result = await backend.run_separated("python3 -c \"import sys; sys.stderr.write('oops\\n')\"")
    assert b"oops" in result.stderr
    # stdout must be empty or whitespace only
    assert result.stdout.strip() == b""


@pytest.mark.asyncio
async def test_separated_stdout_not_in_stderr() -> None:
    """Stdout written by script appears in stdout only."""
    backend = LocalBackend()
    result = await backend.run_separated("python3 -c \"print('hello_stdout')\"")
    assert b"hello_stdout" in result.stdout
    assert b"hello_stdout" not in result.stderr


@pytest.mark.asyncio
async def test_separated_exit_code_from_python_raise() -> None:
    """Python script that raises should produce nonzero exit — matches test_exit_code_on_failure."""
    backend = LocalBackend()
    result = await backend.run_separated("python3 -c \"raise ValueError('bad')\"")
    assert result.exit_code != 0
    # Traceback lands on stderr
    assert b"ValueError" in result.stderr


@pytest.mark.asyncio
async def test_separated_timeout_returns_minus_one_exit_code() -> None:
    """Timed-out execution yields exit_code=-1 and a sentinel in stderr."""
    backend = LocalBackend()
    result = await backend.run_separated("sleep 60", timeout=0.5)
    assert result.exit_code == -1
    assert b"timed out" in result.stderr


@pytest.mark.asyncio
async def test_separated_stdout_cap_respected() -> None:
    """Output exceeding max_stdout_bytes is truncated."""
    backend = LocalBackend(max_stdout_bytes=20)
    result = await backend.run_separated("python3 -c \"print('x' * 1000)\"")
    assert len(result.stdout) <= 20


@pytest.mark.asyncio
async def test_separated_both_streams_empty_on_no_output() -> None:
    backend = LocalBackend()
    result = await backend.run_separated("true")
    assert result.stdout == b""
    assert result.stderr == b""
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_capabilities_supports_separated_streams() -> None:
    """BackendCapabilities.supports_separated_streams must be True for LocalBackend."""
    backend = LocalBackend()
    assert backend.capabilities.supports_separated_streams is True


@pytest.mark.asyncio
async def test_separated_result_is_dataclass() -> None:
    """SeparatedResult exposes stdout, stderr, exit_code attributes."""
    backend = LocalBackend()
    result = await backend.run_separated("echo hi")
    assert hasattr(result, "stdout")
    assert hasattr(result, "stderr")
    assert hasattr(result, "exit_code")
    assert isinstance(result.stdout, bytes)
    assert isinstance(result.stderr, bytes)
    assert isinstance(result.exit_code, int)
