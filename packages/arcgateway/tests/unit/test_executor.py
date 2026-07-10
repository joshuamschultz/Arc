"""Unit tests for Executor Protocol implementations.

Covers:
- AsyncioExecutor: smoke test, yields delta and done sentinel, Protocol compliance
- SubprocessExecutor: T1.6 implemented — unit-level checks (run() returns iterator,
  default config, Protocol compliance). Full subprocess round-trip tested in
  tests/integration/test_subprocess_executor.py.
- NATSExecutor: raises NotImplementedError as documented
- InboundEvent and Delta Pydantic model validation
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcgateway.executor import (
    AsyncioExecutor,
    Delta,
    Executor,
    InboundEvent,
    NATSExecutor,
    ResourceLimits,
    SubprocessExecutor,
)

# ---------------------------------------------------------------------------
# InboundEvent model validation
# ---------------------------------------------------------------------------


class TestInboundEvent:
    def test_required_fields(self) -> None:
        """InboundEvent requires all core fields."""
        event = InboundEvent(
            platform="telegram",
            chat_id="12345",
            user_did="did:arc:user:alice",
            agent_did="did:arc:agent:bot",
            session_key="abc123def456789a",
            message="Hello!",
        )
        assert event.platform == "telegram"
        assert event.message == "Hello!"
        assert event.raw_payload == {}

    def test_optional_thread_id(self) -> None:
        """thread_id defaults to None."""
        event = InboundEvent(
            platform="slack",
            chat_id="C123",
            user_did="did:arc:user:bob",
            agent_did="did:arc:agent:bot",
            session_key="key",
            message="hi",
        )
        assert event.thread_id is None

    def test_raw_payload_preserved(self) -> None:
        """raw_payload stores arbitrary dict for audit purposes."""
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:y",
            session_key="k",
            message="m",
            raw_payload={"update_id": 99, "message": {"text": "m"}},
        )
        assert event.raw_payload["update_id"] == 99


# ---------------------------------------------------------------------------
# Delta model validation
# ---------------------------------------------------------------------------


class TestDelta:
    def test_token_delta(self) -> None:
        """Token deltas carry content and are not final."""
        delta = Delta(kind="token", content="Hello", is_final=False, turn_id="t1")
        assert delta.kind == "token"
        assert delta.content == "Hello"
        assert delta.is_final is False

    def test_done_delta(self) -> None:
        """Done sentinel has is_final=True."""
        delta = Delta(kind="done", is_final=True, turn_id="t1")
        assert delta.is_final is True
        assert delta.content == ""


# ---------------------------------------------------------------------------
# AsyncioExecutor smoke test
# ---------------------------------------------------------------------------


class TestAsyncioExecutor:
    @pytest.mark.asyncio
    async def test_run_yields_done_sentinel(self) -> None:
        """AsyncioExecutor.run() must yield exactly one is_final=True delta."""
        executor = AsyncioExecutor()
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:alice",
            agent_did="did:arc:agent:bot",
            session_key="session123",
            message="test message",
        )

        deltas = []
        delta_stream = await executor.run(event)
        async for delta in delta_stream:
            deltas.append(delta)

        assert len(deltas) >= 1, "Must yield at least one delta"
        assert deltas[-1].is_final is True, "Last delta must be the done sentinel"
        assert deltas[-1].kind == "done"

    @pytest.mark.asyncio
    async def test_run_yields_token_before_done(self) -> None:
        """AsyncioExecutor skeleton yields at least one token before the done sentinel."""
        executor = AsyncioExecutor()
        event = InboundEvent(
            platform="slack",
            chat_id="C123",
            user_did="did:arc:user:bob",
            agent_did="did:arc:agent:bot",
            session_key="s1",
            message="ping",
        )

        deltas = []
        delta_stream = await executor.run(event)
        async for delta in delta_stream:
            deltas.append(delta)

        non_final = [d for d in deltas if not d.is_final]
        assert len(non_final) >= 1, "Should yield at least one token delta"

    @pytest.mark.asyncio
    async def test_executor_protocol_compliance(self) -> None:
        """AsyncioExecutor must satisfy the Executor Protocol."""
        executor = AsyncioExecutor()
        # runtime_checkable Protocol check
        assert isinstance(executor, Executor)

    @pytest.mark.asyncio
    async def test_turn_id_matches_session_key(self) -> None:
        """Stub implementation uses session_key as turn_id for traceability."""
        executor = AsyncioExecutor()
        session_key = "trace_key_abc"
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:y",
            session_key=session_key,
            message="trace test",
        )

        delta_stream = await executor.run(event)
        async for delta in delta_stream:
            assert delta.turn_id == session_key


# ---------------------------------------------------------------------------
# SubprocessExecutor — T1.6 unit-level tests
# (full round-trip subprocess tests live in tests/integration/test_subprocess_executor.py)
# ---------------------------------------------------------------------------


class TestSubprocessExecutor:
    def test_instantiates_with_defaults(self) -> None:
        """SubprocessExecutor instantiates with federal-tier defaults (T1.6 implemented)."""
        executor = SubprocessExecutor()
        assert executor._worker_cmd == ["arc-agent-worker"]
        assert executor._resource_limits.memory_mb == 512
        assert executor._resource_limits.cpu_seconds == 60
        assert executor._resource_limits.file_descriptors == 256

    def test_protocol_compliance(self) -> None:
        """SubprocessExecutor must satisfy the Executor Protocol."""
        executor = SubprocessExecutor()
        assert isinstance(executor, Executor)

    @pytest.mark.asyncio
    async def test_run_returns_async_iterator(self) -> None:
        """SubprocessExecutor.run() returns an AsyncIterator (not NotImplementedError).

        T1.6 is fully implemented. run() returns an async generator — we verify
        it does not immediately raise. Full round-trip is in integration tests.
        """
        executor = SubprocessExecutor(
            worker_cmd=[sys.executable, "-m", "arccli.agent_worker"],
            resource_limits=ResourceLimits(),
        )
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:y",
            session_key="k",
            message="m",
        )
        # run() must return an AsyncIterator without raising
        result = await executor.run(event)
        assert hasattr(result, "__aiter__"), "run() must return an AsyncIterator"

    def test_custom_worker_cmd_and_limits(self) -> None:
        """Custom worker_cmd and ResourceLimits are stored correctly."""
        cmd = [sys.executable, "-m", "arccli.agent_worker"]
        limits = ResourceLimits(memory_mb=1024, cpu_seconds=90, file_descriptors=512)
        executor = SubprocessExecutor(worker_cmd=cmd, resource_limits=limits)
        assert executor._worker_cmd == cmd
        assert executor._resource_limits.memory_mb == 1024


class TestSubprocessExecutorTeamRoot:
    """Task 26 — SubprocessExecutor threads --team-root into the spawned
    worker so arc-agent-worker can resolve --did via a DID index instead of
    a fixed, agent-agnostic search path (the DID-blindness bug)."""

    def _fake_proc(self) -> Any:
        """Minimal asyncio.subprocess.Process double for _stream()'s happy path."""
        proc = MagicMock()
        proc.pid = 4242
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.stdin.close = MagicMock()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_eof()
        proc.wait = AsyncMock(return_value=0)
        return proc

    @pytest.mark.asyncio
    async def test_team_root_appended_to_spawned_cmd(self, tmp_path: Path) -> None:
        executor = SubprocessExecutor(
            worker_cmd=[sys.executable, "-m", "arccli.agent_worker"],
            team_root=tmp_path,
        )
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:y",
            session_key="k",
            message="m",
        )
        with patch.object(
            executor, "_spawn_proc", AsyncMock(return_value=self._fake_proc())
        ) as spawn_mock:
            deltas = [d async for d in await executor.run(event)]

        assert deltas  # sanity — the fake proc round-trip completed
        spawned_cmd = spawn_mock.call_args[0][0]
        assert "--team-root" in spawned_cmd
        assert str(tmp_path) in spawned_cmd

    @pytest.mark.asyncio
    async def test_no_team_root_omits_flag_entirely(self) -> None:
        """Backward compatibility: an executor built without team_root (the
        pre-task-26 default, and from_config's scaffold path) must not add
        the flag — arc-agent-worker's --team-root has a None default and
        falls back to its legacy fixed search paths."""
        executor = SubprocessExecutor(worker_cmd=[sys.executable, "-m", "arccli.agent_worker"])
        event = InboundEvent(
            platform="telegram",
            chat_id="1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:y",
            session_key="k",
            message="m",
        )
        with patch.object(
            executor, "_spawn_proc", AsyncMock(return_value=self._fake_proc())
        ) as spawn_mock:
            [d async for d in await executor.run(event)]

        spawned_cmd = spawn_mock.call_args[0][0]
        assert "--team-root" not in spawned_cmd


# ---------------------------------------------------------------------------
# NATSExecutor — stub tests
# ---------------------------------------------------------------------------


class TestNATSExecutor:
    @pytest.mark.asyncio
    async def test_raises_not_implemented(self) -> None:
        """NATSExecutor must raise NotImplementedError."""
        executor = NATSExecutor()
        event = InboundEvent(
            platform="slack",
            chat_id="C1",
            user_did="did:arc:user:x",
            agent_did="did:arc:agent:y",
            session_key="k",
            message="m",
        )
        with pytest.raises(NotImplementedError, match="SPEC-018"):
            await executor.run(event)
