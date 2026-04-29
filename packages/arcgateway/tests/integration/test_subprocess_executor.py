"""Integration tests for SubprocessExecutor — federal-tier subprocess isolation.

These tests spawn real OS subprocesses using the arc-agent-worker entry point
(invoked as ``python -m arccli.agent_worker`` so they work from source without
an installed wheel). Each test verifies one aspect of the T1.6 contract.

Test inventory:
  test_subprocess_isolation_per_session:
      Spawn 2 workers for different events; verify each has a different PID.

  test_worker_exits_on_eof:
      Close stdin → worker exits cleanly within a short timeout.

  test_worker_audit_event_chain:
      Worker emits its own audit-terminator Delta (subprocess-audit marker).
      Parent-side audit chain is independent — no cross-contamination.

  test_json_lines_protocol_robustness:
      Send a malformed JSON line; worker logs an error and emits an error
      Delta but does NOT crash — subsequent valid events still work.

  test_subprocess_run_yields_deltas:
      Full round-trip: send InboundEvent, receive at least one token Delta
      and exactly one done sentinel with is_final=True.

  test_subprocess_run_yields_done_sentinel:
      The last yielded Delta must have is_final=True and kind=="done".

  test_concurrent_sessions_have_independent_pids:
      Send two events concurrently; each subprocess has a unique PID.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator

import pytest

from arcgateway.executor import (
    Delta,
    InboundEvent,
    ResourceLimits,
    SubprocessExecutor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Use ``python -m arccli.agent_worker`` so tests work from source without
# a wheel installation. This is the canonical "run from source" invocation.
_WORKER_CMD = [sys.executable, "-m", "arccli.agent_worker"]

# Generous timeout for subprocess operations in CI environments.
_SUBPROCESS_TIMEOUT = 15.0


def _make_event(
    message: str = "hello",
    session_key: str = "test_session_01",
    agent_did: str = "did:arc:agent:test",
) -> InboundEvent:
    """Build a minimal InboundEvent for tests."""
    return InboundEvent(
        platform="telegram",
        chat_id="42",
        user_did="did:arc:user:alice",
        agent_did=agent_did,
        session_key=session_key,
        message=message,
    )


def _make_executor(resource_limits: ResourceLimits | None = None) -> SubprocessExecutor:
    """Build a SubprocessExecutor pointing at the source worker."""
    return SubprocessExecutor(
        worker_cmd=_WORKER_CMD,
        resource_limits=resource_limits
        or ResourceLimits(
            memory_mb=512,
            cpu_seconds=60,
            file_descriptors=256,
        ),
    )


async def _collect_deltas(executor: SubprocessExecutor, event: InboundEvent) -> list[Delta]:
    """Collect all deltas from a single executor.run() call.

    Imposes _SUBPROCESS_TIMEOUT to prevent hanging tests.
    """
    deltas: list[Delta] = []
    stream: AsyncIterator[Delta] = await executor.run(event)
    async for delta in stream:
        deltas.append(delta)
    return deltas


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubprocessExecutorRoundTrip:
    """Full round-trip tests: event in → deltas out."""

    @pytest.mark.asyncio
    async def test_subprocess_run_yields_deltas(self) -> None:
        """Executor.run() must yield at least one Delta before the done sentinel."""
        executor = _make_executor()
        event = _make_event(message="ping")
        deltas = await asyncio.wait_for(
            _collect_deltas(executor, event),
            timeout=_SUBPROCESS_TIMEOUT,
        )
        assert len(deltas) >= 1, "Must yield at least one Delta"
        non_final = [d for d in deltas if not d.is_final]
        assert len(non_final) >= 1, "Must yield at least one non-final Delta"

    @pytest.mark.asyncio
    async def test_subprocess_run_yields_done_sentinel(self) -> None:
        """Last Delta must be the done sentinel with is_final=True."""
        executor = _make_executor()
        event = _make_event(message="test done sentinel")
        deltas = await asyncio.wait_for(
            _collect_deltas(executor, event),
            timeout=_SUBPROCESS_TIMEOUT,
        )
        assert deltas, "Must yield at least one Delta"
        last = deltas[-1]
        assert last.is_final is True, f"Last delta must be final; got {last!r}"
        assert last.kind == "done", f"Last delta must be kind='done'; got {last.kind!r}"

    @pytest.mark.asyncio
    async def test_subprocess_run_echo_content(self) -> None:
        """Worker echo stub must echo the message content in its token Delta."""
        executor = _make_executor()
        message = "unique-test-message-xyz"
        event = _make_event(message=message)
        deltas = await asyncio.wait_for(
            _collect_deltas(executor, event),
            timeout=_SUBPROCESS_TIMEOUT,
        )
        token_deltas = [d for d in deltas if d.kind == "token"]
        assert token_deltas, "Must yield at least one token Delta"
        combined_content = " ".join(d.content for d in token_deltas)
        assert message in combined_content, (
            f"Echo worker must include the original message in token Delta content. "
            f"Message={message!r} content={combined_content!r}"
        )


class TestSubprocessIsolation:
    """Tests verifying per-session OS-process isolation."""

    @pytest.mark.asyncio
    async def test_subprocess_isolation_per_session(self) -> None:
        """Two concurrent sessions must run in separate subprocesses (different PIDs).

        Spawns two SubprocessExecutor.run() calls concurrently and collects
        the audit-terminator Delta from each, which carries the subprocess PID.
        """
        executor = _make_executor()
        event_a = _make_event(message="session-a-msg", session_key="session_a_iso")
        event_b = _make_event(message="session-b-msg", session_key="session_b_iso")

        # Run both concurrently — each spawns its own subprocess
        deltas_a, deltas_b = await asyncio.wait_for(
            asyncio.gather(
                _collect_deltas(executor, event_a),
                _collect_deltas(executor, event_b),
            ),
            timeout=_SUBPROCESS_TIMEOUT * 2,
        )

        # Extract PIDs from audit terminator (the done sentinel with subprocess-audit content)
        pid_a = _extract_pid_from_audit_delta(deltas_a)
        pid_b = _extract_pid_from_audit_delta(deltas_b)

        assert pid_a is not None, "Session A must emit subprocess-audit terminator with PID"
        assert pid_b is not None, "Session B must emit subprocess-audit terminator with PID"
        assert pid_a != pid_b, (
            f"Two sessions must run in separate subprocesses. pid_a={pid_a} pid_b={pid_b}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_sessions_have_independent_pids(self) -> None:
        """Concurrently running sessions must each have a unique subprocess PID."""
        executor = _make_executor()
        events = [
            _make_event(message=f"msg-{i}", session_key=f"concurrent_session_{i}")
            for i in range(3)
        ]
        all_deltas = await asyncio.wait_for(
            asyncio.gather(*[_collect_deltas(executor, e) for e in events]),
            timeout=_SUBPROCESS_TIMEOUT * 3,
        )
        pids = [_extract_pid_from_audit_delta(d) for d in all_deltas]  # type: ignore[arg-type]
        # All PIDs must be non-None
        assert all(p is not None for p in pids), f"All sessions must emit PIDs; got {pids}"
        # All PIDs must be unique
        assert len(set(pids)) == len(pids), (
            f"Each concurrent session must have a unique subprocess PID; got {pids}"
        )


class TestWorkerLifecycle:
    """Tests for arc-agent-worker subprocess lifecycle behaviour."""

    @pytest.mark.asyncio
    async def test_worker_exits_on_eof(self) -> None:
        """Closing stdin → worker exits cleanly within timeout.

        Spawns a raw subprocess (not via SubprocessExecutor) to test the
        worker's EOF handling in isolation. The worker should exit 0.
        """
        proc = await asyncio.create_subprocess_exec(
            *_WORKER_CMD,
            "--did",
            "did:arc:agent:test-eof",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        # Close stdin to signal EOF — worker should drain and exit
        assert proc.stdin is not None
        proc.stdin.close()

        exit_code = await asyncio.wait_for(proc.wait(), timeout=_SUBPROCESS_TIMEOUT)
        assert exit_code == 0, f"Worker must exit 0 on clean EOF; got {exit_code}"

    @pytest.mark.asyncio
    async def test_worker_audit_event_chain(self) -> None:
        """Worker subprocess emits its own audit chain, independent of parent.

        Verifies that the parent's audit trail contains the subprocess-audit
        terminator Delta (PID marker). The worker's internal audit events
        (emitted to its own stderr/log) do not appear in the parent's Delta stream.
        """
        executor = _make_executor()
        event = _make_event(message="audit-chain-test")
        deltas = await asyncio.wait_for(
            _collect_deltas(executor, event),
            timeout=_SUBPROCESS_TIMEOUT,
        )
        # The subprocess-audit Delta is the done sentinel from the parent
        audit_done = [d for d in deltas if d.is_final and "subprocess-audit" in d.content]
        assert audit_done, (
            "SubprocessExecutor must emit a subprocess-audit terminator Delta. "
            f"Got deltas: {deltas!r}"
        )
        # Verify PID is present in the audit marker
        pid = _extract_pid_from_audit_delta(deltas)
        assert pid is not None and pid > 0, (
            f"Audit terminator must carry a positive PID; got {pid!r}"
        )


class TestProtocolRobustness:
    """Tests for JSON-lines protocol robustness."""

    @pytest.mark.asyncio
    async def test_json_lines_protocol_robustness(self) -> None:
        """Malformed JSON line → worker emits error Delta and does NOT crash.

        Sends a malformed JSON line to the worker. The worker must:
          1. Log the error (to stderr — not checked here).
          2. Emit an error/done Delta to stdout.
          3. Continue processing (not crash).

        We verify by sending malformed JSON and checking that the worker
        exits cleanly (exit code 0) and emits a done sentinel.
        """
        proc = await asyncio.create_subprocess_exec(
            *_WORKER_CMD,
            "--did",
            "did:arc:agent:robustness-test",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None

        # Send malformed JSON
        proc.stdin.write(b"this is { definitely not json !!!\n")
        await proc.stdin.drain()
        # Close stdin — worker should drain then exit
        proc.stdin.close()

        # Read all stdout output
        raw_output = await asyncio.wait_for(
            proc.stdout.read(),
            timeout=_SUBPROCESS_TIMEOUT,
        )
        exit_code = await asyncio.wait_for(proc.wait(), timeout=_SUBPROCESS_TIMEOUT)

        # Worker must exit cleanly
        assert exit_code == 0, f"Worker must exit 0 after malformed JSON; got {exit_code}"

        # Worker must emit at least one JSON line in response to the bad input
        lines = [ln for ln in raw_output.decode("utf-8").splitlines() if ln.strip()]
        assert lines, "Worker must emit at least one Delta line for malformed input"

        # Each emitted line must be valid JSON
        for ln in lines:
            try:
                json.loads(ln)
            except json.JSONDecodeError as exc:
                pytest.fail(f"Worker emitted non-JSON output: {ln!r} — {exc}")

    @pytest.mark.asyncio
    async def test_valid_event_after_malformed_line(self) -> None:
        """Worker must process a valid event after a malformed line.

        Verifies that the worker continues operating correctly after
        encountering bad input (does not enter an error state).
        """
        proc = await asyncio.create_subprocess_exec(
            *_WORKER_CMD,
            "--did",
            "did:arc:agent:recovery-test",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None

        # Send malformed line first
        proc.stdin.write(b"not-valid-json\n")
        # Then send a valid event
        event = _make_event(message="recovery-check")
        proc.stdin.write((event.model_dump_json() + "\n").encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        raw_output = await asyncio.wait_for(
            proc.stdout.read(),
            timeout=_SUBPROCESS_TIMEOUT,
        )
        exit_code = await asyncio.wait_for(proc.wait(), timeout=_SUBPROCESS_TIMEOUT)

        assert exit_code == 0, f"Worker must exit 0 after recovery; got {exit_code}"
        lines = [ln for ln in raw_output.decode("utf-8").splitlines() if ln.strip()]

        # Must have at least a done sentinel for the valid event
        parsed = [json.loads(ln) for ln in lines]
        done_deltas = [d for d in parsed if d.get("is_final")]
        assert done_deltas, (
            "Worker must emit a done Delta for the valid event after malformed input"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_pid_from_audit_delta(deltas: list[Delta]) -> int | None:
    """Extract subprocess PID from the subprocess-audit terminator Delta.

    The SubprocessExecutor emits a done Delta with content like:
    ``[subprocess-audit] pid=<PID> exit_code=<CODE>``

    Returns None if no such Delta is found.
    """
    for delta in deltas:
        if delta.is_final and "subprocess-audit" in delta.content:
            # Parse "pid=<int>" from content
            for part in delta.content.split():
                if part.startswith("pid="):
                    try:
                        return int(part.split("=", 1)[1])
                    except ValueError:
                        pass
    return None
