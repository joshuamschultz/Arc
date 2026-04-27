"""Unit tests for NATSExecutor stub.

NATSExecutor is a deferred implementation that raises NotImplementedError.
These tests lock in:
- NATSExecutor.run() raises NotImplementedError (not silently drops events)
- NATSExecutor is importable from arcgateway.executor (re-export contract)
- NATSExecutor is importable directly from arcgateway.executor_nats
"""

from __future__ import annotations

import pytest

from arcgateway.executor import InboundEvent, NATSExecutor


def _make_event() -> InboundEvent:
    return InboundEvent(
        platform="telegram",
        chat_id="12345",
        user_did="did:arc:telegram:42",
        agent_did="did:arc:agent:test",
        session_key="test-session",
        message="hello",
    )


class TestNATSExecutorStub:
    @pytest.mark.asyncio
    async def test_run_raises_not_implemented(self) -> None:
        """NATSExecutor.run() must raise NotImplementedError — deferred feature."""
        executor = NATSExecutor()
        with pytest.raises(NotImplementedError, match="multi-instance"):
            await executor.run(_make_event())

    def test_nats_executor_importable_from_executor(self) -> None:
        """NATSExecutor must be importable via the public re-export in executor.py."""
        # Verify the re-export works — import succeeds without error
        from arcgateway.executor import NATSExecutor as ExecutorNATS

        assert ExecutorNATS is NATSExecutor

    def test_nats_executor_importable_directly(self) -> None:
        """NATSExecutor must be importable directly from executor_nats."""
        from arcgateway.executor_nats import NATSExecutor as ExecutorNATSDirect

        assert ExecutorNATSDirect is not None

    def test_nats_executor_instantiates(self) -> None:
        """NATSExecutor can be instantiated without arguments."""
        executor = NATSExecutor()
        assert executor is not None
