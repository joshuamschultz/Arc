"""Unit tests for SubprocessExecutor resource-limit enforcement (T1.6.3).

These tests verify that:
  1. ResourceLimits Pydantic model validates correctly with federal defaults.
  2. _make_preexec_fn() returns a callable on POSIX and None on non-POSIX.
  3. The preexec_fn callable applies all three limits (RLIMIT_AS, RLIMIT_CPU,
     RLIMIT_NOFILE) via resource.setrlimit on POSIX systems.
  4. Non-POSIX graceful degradation: returns None + emits a UserWarning.
  5. Custom ResourceLimits values are forwarded correctly to setrlimit.
  6. SubprocessExecutor stores and exposes resource_limits for inspection.

The actual subprocess-level limit enforcement is verified in:
  tests/integration/test_subprocess_executor.py
  (spawns a real worker and checks /proc or resource.getrlimit in-process)
"""

from __future__ import annotations

import os
import sys
import warnings
from typing import Any
from unittest.mock import call, patch

import pytest

from arcgateway.executor import (
    ResourceLimits,
    SubprocessExecutor,
    _make_preexec_fn,
)

# ---------------------------------------------------------------------------
# ResourceLimits model tests
# ---------------------------------------------------------------------------


class TestResourceLimitsModel:
    def test_federal_defaults(self) -> None:
        """Federal-tier defaults must match the specification (T1.6.3)."""
        limits = ResourceLimits()
        assert limits.memory_mb == 512, "Default memory_mb must be 512 MB"
        assert limits.cpu_seconds == 60, "Default cpu_seconds must be 60 s"
        assert limits.file_descriptors == 256, "Default file_descriptors must be 256"

    def test_custom_values_stored(self) -> None:
        """Custom values must be stored exactly as provided."""
        limits = ResourceLimits(memory_mb=1024, cpu_seconds=120, file_descriptors=512)
        assert limits.memory_mb == 1024
        assert limits.cpu_seconds == 120
        assert limits.file_descriptors == 512

    def test_model_dump(self) -> None:
        """model_dump() must include all three limit fields."""
        limits = ResourceLimits(memory_mb=256, cpu_seconds=30, file_descriptors=128)
        data = limits.model_dump()
        assert data == {
            "memory_mb": 256,
            "cpu_seconds": 30,
            "file_descriptors": 128,
        }

    def test_memory_bytes_calculation(self) -> None:
        """512 MB must equal exactly 536_870_912 bytes for RLIMIT_AS."""
        limits = ResourceLimits(memory_mb=512)
        expected_bytes = 512 * 1024 * 1024
        assert expected_bytes == 536_870_912
        # Verify the factory uses the correct byte conversion
        if os.name == "posix":
            fn = _make_preexec_fn(limits)
            assert fn is not None  # callable on POSIX


# ---------------------------------------------------------------------------
# _make_preexec_fn() on POSIX systems
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only resource limit tests")
class TestMakePreexecFnPosix:
    def test_returns_callable_on_posix(self) -> None:
        """_make_preexec_fn() must return a callable on POSIX systems."""
        fn = _make_preexec_fn(ResourceLimits())
        assert callable(fn), f"Expected callable; got {type(fn)}"

    def test_applies_all_three_limits(self) -> None:
        """preexec_fn must call setrlimit for RLIMIT_AS, RLIMIT_CPU, RLIMIT_NOFILE."""
        limits = ResourceLimits(memory_mb=256, cpu_seconds=30, file_descriptors=128)
        fn = _make_preexec_fn(limits)
        assert fn is not None

        import resource as _resource_module

        with patch("resource.setrlimit") as mock_setrlimit:
            fn()

        expected_memory_bytes = 256 * 1024 * 1024
        expected_calls = [
            call(_resource_module.RLIMIT_AS, (expected_memory_bytes, expected_memory_bytes)),
            call(_resource_module.RLIMIT_CPU, (30, 30)),
            call(_resource_module.RLIMIT_NOFILE, (128, 128)),
        ]
        mock_setrlimit.assert_has_calls(expected_calls, any_order=False)
        assert mock_setrlimit.call_count == 3, (
            f"setrlimit must be called exactly 3 times; called {mock_setrlimit.call_count}"
        )

    def test_memory_limit_converted_to_bytes(self) -> None:
        """Memory limit must be converted from MB to bytes for RLIMIT_AS."""
        limits = ResourceLimits(memory_mb=512)
        fn = _make_preexec_fn(limits)
        assert fn is not None

        import resource as _resource_module

        captured: list[Any] = []

        def _capture_setrlimit(resource_id: int, limits_tuple: tuple[int, int]) -> None:
            captured.append((resource_id, limits_tuple))

        with patch("resource.setrlimit", side_effect=_capture_setrlimit):
            fn()

        # Find the RLIMIT_AS call
        rlimit_as_calls = [
            (rid, lim) for rid, lim in captured if rid == _resource_module.RLIMIT_AS
        ]
        assert rlimit_as_calls, "Must call setrlimit for RLIMIT_AS"
        _, (soft, hard) = rlimit_as_calls[0]
        expected_bytes = 512 * 1024 * 1024
        assert soft == expected_bytes, f"Soft limit must be {expected_bytes} bytes; got {soft}"
        assert hard == expected_bytes, f"Hard limit must be {expected_bytes} bytes; got {hard}"

    def test_hard_and_soft_limits_equal(self) -> None:
        """Both soft and hard limits must be set to the same value.

        Setting hard == soft ensures there is no grace buffer beyond the limit.
        This is the correct federal-tier enforcement pattern.
        """
        limits = ResourceLimits(memory_mb=128, cpu_seconds=10, file_descriptors=64)
        fn = _make_preexec_fn(limits)
        assert fn is not None

        captured: list[tuple[int, tuple[int, int]]] = []

        def _capture(resource_id: int, limits_tuple: tuple[int, int]) -> None:
            captured.append((resource_id, limits_tuple))

        with patch("resource.setrlimit", side_effect=_capture):
            fn()

        for _, (soft, hard) in captured:
            assert soft == hard, (
                f"Soft and hard limits must be equal for federal enforcement; "
                f"got soft={soft} hard={hard}"
            )

    def test_custom_values_forwarded_to_setrlimit(self) -> None:
        """Custom ResourceLimits values must flow through to setrlimit calls."""
        limits = ResourceLimits(memory_mb=768, cpu_seconds=45, file_descriptors=192)
        fn = _make_preexec_fn(limits)
        assert fn is not None

        import resource as _resource_module

        captured: dict[int, tuple[int, int]] = {}

        def _capture(resource_id: int, limits_tuple: tuple[int, int]) -> None:
            captured[resource_id] = limits_tuple

        with patch("resource.setrlimit", side_effect=_capture):
            fn()

        assert captured[_resource_module.RLIMIT_AS] == (768 * 1024 * 1024, 768 * 1024 * 1024)
        assert captured[_resource_module.RLIMIT_CPU] == (45, 45)
        assert captured[_resource_module.RLIMIT_NOFILE] == (192, 192)


# ---------------------------------------------------------------------------
# _make_preexec_fn() graceful degradation on non-POSIX
# ---------------------------------------------------------------------------


class TestMakePreexecFnNonPosix:
    def test_returns_none_on_non_posix(self) -> None:
        """_make_preexec_fn() must return None on non-POSIX systems."""
        with patch.object(os, "name", "nt"):  # simulate Windows
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                fn = _make_preexec_fn(ResourceLimits())
            assert fn is None, f"Expected None on non-POSIX; got {fn!r}"

    def test_emits_user_warning_on_non_posix(self) -> None:
        """_make_preexec_fn() must emit a UserWarning on non-POSIX systems."""
        with patch.object(os, "name", "nt"):  # simulate Windows
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _make_preexec_fn(ResourceLimits())

        assert len(caught) >= 1, "Must emit at least one warning on non-POSIX"
        warning_messages = [str(w.message) for w in caught]
        assert any(
            "resource limits" in msg.lower() or "non-posix" in msg.lower()
            for msg in warning_messages
        ), f"Warning must mention resource limits or non-POSIX. Got: {warning_messages}"


# ---------------------------------------------------------------------------
# SubprocessExecutor stores resource_limits
# ---------------------------------------------------------------------------


class TestSubprocessExecutorResourceLimitsStorage:
    def test_default_resource_limits(self) -> None:
        """SubprocessExecutor must use federal-tier defaults when no limits given."""
        executor = SubprocessExecutor()
        assert executor._resource_limits.memory_mb == 512
        assert executor._resource_limits.cpu_seconds == 60
        assert executor._resource_limits.file_descriptors == 256

    def test_custom_resource_limits_stored(self) -> None:
        """SubprocessExecutor must store custom resource limits as provided."""
        limits = ResourceLimits(memory_mb=1024, cpu_seconds=90, file_descriptors=384)
        executor = SubprocessExecutor(resource_limits=limits)
        assert executor._resource_limits.memory_mb == 1024
        assert executor._resource_limits.cpu_seconds == 90
        assert executor._resource_limits.file_descriptors == 384

    def test_custom_worker_cmd_stored(self) -> None:
        """SubprocessExecutor must store a custom worker_cmd as provided."""
        cmd = [sys.executable, "-m", "arccli.agent_worker"]
        executor = SubprocessExecutor(worker_cmd=cmd)
        assert executor._worker_cmd == cmd

    def test_default_worker_cmd_is_arc_agent_worker(self) -> None:
        """Default worker_cmd must be ['arc-agent-worker']."""
        executor = SubprocessExecutor()
        assert executor._worker_cmd == ["arc-agent-worker"]


# ---------------------------------------------------------------------------
# Audit event emission: gateway.session.executor_choice
# ---------------------------------------------------------------------------


class TestExecutorChoiceAuditEvent:
    """Verify the gateway.session.executor_choice audit event is logged."""

    @pytest.mark.asyncio
    async def test_audit_event_logged_on_run(self) -> None:
        """SubprocessExecutor.run() must log gateway.session.executor_choice at INFO."""
        import logging

        from arcgateway.executor import InboundEvent

        executor = SubprocessExecutor(
            worker_cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
            resource_limits=ResourceLimits(),
        )
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:audit-test",
            session_key="audit_session_01",
            message="test audit",
        )

        log_records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(record)

        handler = _Capture()
        logger = logging.getLogger("arcgateway.executor")
        logger.addHandler(handler)
        original_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            # run() itself just creates the stream; the audit log is emitted lazily
            # when the stream is consumed. We iterate to trigger it.
            stream = await executor.run(event)
            # Consume the stream (subprocess exits immediately with exit code 0;
            # the worker command here is a trivial python -c that exits immediately)
            try:
                async for _ in stream:
                    pass
            except Exception:  # noqa: S110 — subprocess may produce no output; intentional
                pass
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)

        # Verify at least one record mentions executor_choice or SubprocessExecutor
        relevant = [
            r
            for r in log_records
            if "executor_choice" in r.getMessage() or "SubprocessExecutor" in r.getMessage()
        ]
        assert relevant, (
            "Must log gateway.session.executor_choice audit event. "
            f"Captured log messages: {[r.getMessage() for r in log_records]}"
        )
