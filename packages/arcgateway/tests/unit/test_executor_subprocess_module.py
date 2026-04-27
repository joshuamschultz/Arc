"""Unit tests for arcgateway.executor_subprocess module.

executor_subprocess.py was extracted from executor.py (ADR-004 / G1.6 LOC
budget) but executor.py still contains its own copy of ResourceLimits,
SubprocessExecutor, and _make_preexec_fn — meaning executor_subprocess.py
is currently unreachable from production code paths.

These tests:
1. Lock in the executor_subprocess module's own implementation so any future
   re-wiring does not silently break behaviour.
2. Document the architectural gap (executor.py has not yet been updated to
   re-export from executor_subprocess.py).

Cross-package note:
    executor.py and executor_subprocess.py contain duplicate implementations
    of ResourceLimits, SubprocessExecutor, and _make_preexec_fn. This is a
    design smell — executor.py should import from executor_subprocess.py per
    the module docstring. This gap is surfaced to the arcgateway owner for
    resolution (see final test report).
"""

from __future__ import annotations

import os
import sys
import warnings

import pytest

from arcgateway.executor import InboundEvent

# Import directly from executor_subprocess to exercise the module's own code.
from arcgateway.executor_subprocess import (
    ResourceLimits,
    SubprocessExecutor,
    _make_preexec_fn,
)


class TestExecutorSubprocessResourceLimits:
    def test_resource_limits_defaults(self) -> None:
        """ResourceLimits in executor_subprocess has the same federal defaults."""
        limits = ResourceLimits()
        assert limits.memory_mb == 512
        assert limits.cpu_seconds == 60
        assert limits.file_descriptors == 256

    def test_resource_limits_custom_values(self) -> None:
        """Custom values are stored correctly."""
        limits = ResourceLimits(memory_mb=1024, cpu_seconds=90, file_descriptors=512)
        assert limits.memory_mb == 1024
        assert limits.cpu_seconds == 90
        assert limits.file_descriptors == 512

    def test_resource_limits_model_dump(self) -> None:
        """model_dump() returns expected dict."""
        limits = ResourceLimits(memory_mb=256, cpu_seconds=30, file_descriptors=128)
        assert limits.model_dump() == {
            "memory_mb": 256,
            "cpu_seconds": 30,
            "file_descriptors": 128,
        }


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only resource limit tests")
class TestMakePreexecFnSubprocessModule:
    def test_returns_callable_on_posix(self) -> None:
        """_make_preexec_fn() from executor_subprocess returns callable on POSIX."""
        fn = _make_preexec_fn(ResourceLimits())
        assert callable(fn)

    def test_returns_none_and_warns_on_non_posix(self) -> None:
        """Verify non-POSIX path (simulate via os.name patch)."""
        from unittest.mock import patch

        with patch.object(os, "name", "nt"):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = _make_preexec_fn(ResourceLimits())
        assert result is None
        assert len(caught) >= 1


class TestSubprocessExecutorFromSubprocessModule:
    def test_instantiates_with_defaults(self) -> None:
        """SubprocessExecutor from executor_subprocess instantiates correctly."""
        executor = SubprocessExecutor()
        assert executor._worker_cmd == ["arc-agent-worker"]
        assert executor._resource_limits.memory_mb == 512

    def test_custom_worker_cmd(self) -> None:
        """Custom worker_cmd is stored."""
        cmd = [sys.executable, "-m", "worker"]
        executor = SubprocessExecutor(worker_cmd=cmd)
        assert executor._worker_cmd == cmd

    def test_custom_resource_limits(self) -> None:
        """Custom resource limits are stored."""
        limits = ResourceLimits(memory_mb=768, cpu_seconds=45, file_descriptors=192)
        executor = SubprocessExecutor(resource_limits=limits)
        assert executor._resource_limits.memory_mb == 768

    @pytest.mark.asyncio
    async def test_run_emits_audit_event(self) -> None:
        """SubprocessExecutor.run() from executor_subprocess logs audit event."""
        import logging

        executor = SubprocessExecutor(
            worker_cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
        )
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:subprocess-module-test",
            session_key="subprocess_module_session",
            message="audit test",
        )

        log_records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, r: logging.LogRecord) -> None:
                log_records.append(r)

        handler = _Cap()
        logger = logging.getLogger("arcgateway.executor")
        logger.addHandler(handler)
        original_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            stream = await executor.run(event)
            try:
                async for _ in stream:
                    pass
            except Exception:  # noqa: S110
                pass  # test cleanup — subprocess may exit non-zero; that's fine
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)

        relevant = [
            r for r in log_records
            if "executor_choice" in r.getMessage() or "SubprocessExecutor" in r.getMessage()
        ]
        assert relevant, (
            "executor_subprocess.SubprocessExecutor.run() must log audit event. "
            f"Messages: {[r.getMessage() for r in log_records[:5]]}"
        )

    @pytest.mark.asyncio
    async def test_spawn_proc_raises_runtime_error_on_missing_command(self) -> None:
        """_spawn_proc() raises RuntimeError when worker command is not found."""
        executor = SubprocessExecutor(worker_cmd=["arc-nonexistent-worker-xyz"])
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:test",
            session_key="spawn_fail_session",
            message="test",
        )
        with pytest.raises(RuntimeError, match="worker command not found"):
            stream = await executor.run(event)
            # The RuntimeError is raised inside the async generator; consume it.
            async for _ in stream:
                pass

    @pytest.mark.asyncio
    async def test_read_deltas_handles_malformed_json(self) -> None:
        """_read_deltas() produces an error Delta (not an exception) for malformed JSON."""
        import asyncio

        executor = SubprocessExecutor()

        # Build a fake StreamReader that yields a malformed JSON line
        malformed_line = b"not-valid-json\n"
        reader = asyncio.StreamReader()
        reader.feed_data(malformed_line)
        reader.feed_eof()

        deltas = []
        async for delta in executor._read_deltas(reader, "test-session", 12345):
            deltas.append(delta)

        assert len(deltas) == 1
        assert "malformed" in deltas[0].content.lower() or "error" in deltas[0].content.lower()

    @pytest.mark.asyncio
    async def test_read_deltas_done_sentinel_stops_iteration(self) -> None:
        """_read_deltas() stops iteration on is_final=True (worker done sentinel)."""
        import asyncio
        import json

        executor = SubprocessExecutor()

        # A done sentinel followed by more data that should NOT be yielded
        done_line = json.dumps({"kind": "done", "content": "", "is_final": True, "turn_id": "s"})
        extra_line = json.dumps({"kind": "token", "content": "should not appear", "is_final": False, "turn_id": "s"})
        data = (done_line + "\n" + extra_line + "\n").encode()

        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()

        deltas = []
        async for delta in executor._read_deltas(reader, "test-session", 12345):
            deltas.append(delta)

        # Done sentinel breaks the loop — no deltas yielded (sentinel itself not yielded)
        assert all(
            "should not appear" not in d.content for d in deltas
        ), "Content after done sentinel must not be yielded"

    @pytest.mark.asyncio
    async def test_read_deltas_invalid_delta_schema_skipped(self) -> None:
        """_read_deltas() skips lines that parse as JSON but fail Delta validation."""
        import asyncio
        import json

        executor = SubprocessExecutor()

        # Valid JSON but missing required Delta fields
        bad_delta = json.dumps({"not_a_delta_field": "xyz"})
        reader = asyncio.StreamReader()
        reader.feed_data((bad_delta + "\n").encode())
        reader.feed_eof()

        deltas = []
        async for delta in executor._read_deltas(reader, "test-session", 12345):
            deltas.append(delta)

        # Malformed Delta is skipped, no error propagated, no deltas yielded
        assert deltas == [], "Invalid Delta must be skipped, not raised or yielded"


class TestMakePreexecFnInnerFunction:
    """Test the _apply_limits inner closure returned by _make_preexec_fn().

    This exercises the function body (lines 108-124) that runs inside the
    child process after fork(). We call it directly with patched resource.setrlimit.
    """

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
    def test_apply_limits_calls_setrlimit_three_times(self) -> None:
        """The _apply_limits closure must call setrlimit exactly three times."""
        from unittest.mock import patch

        limits = ResourceLimits(memory_mb=256, cpu_seconds=30, file_descriptors=128)
        fn = _make_preexec_fn(limits)
        assert fn is not None

        with patch("resource.setrlimit") as mock_rl:
            fn()

        assert mock_rl.call_count == 3

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
    def test_apply_limits_handles_value_error_gracefully(self) -> None:
        """_apply_limits must not propagate ValueError from setrlimit (e.g. macOS RLIMIT_AS)."""
        from unittest.mock import patch

        limits = ResourceLimits()
        fn = _make_preexec_fn(limits)
        assert fn is not None

        def _raises_on_as(resource_id: int, _limits: tuple[int, int]) -> None:
            import resource as _r
            if resource_id == _r.RLIMIT_AS:
                raise ValueError("macOS: RLIM_INFINITY cannot be lowered")

        with patch("resource.setrlimit", side_effect=_raises_on_as):
            # Must NOT raise — ValueError must be caught and written to stderr
            fn()

    @pytest.mark.skipif(os.name != "posix", reason="POSIX-only")
    def test_apply_limits_writes_to_stderr_on_failure(self) -> None:
        """_apply_limits writes a warning to stderr when a limit cannot be set."""
        import io
        from unittest.mock import patch

        limits = ResourceLimits()
        fn = _make_preexec_fn(limits)
        assert fn is not None

        stderr_buf = io.StringIO()
        with (
            patch("resource.setrlimit", side_effect=ValueError("test error")),
            patch("sys.stderr", stderr_buf),
        ):
            fn()

        output = stderr_buf.getvalue()
        assert "arc-agent-worker" in output or "could not set" in output
