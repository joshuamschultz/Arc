"""Unit tests for NATSExecutor stub.

NATSExecutor is a deferred implementation that raises NotImplementedError.
These tests lock in:
- NATSExecutor.run() raises NotImplementedError (not silently drops events)
- arcgateway.executor_nats is its single definition site — arcgateway.executor
  must NOT carry a second copy (the two used to be independent classes)
"""

from __future__ import annotations

import pytest

from arcgateway.executor import InboundEvent
from arcgateway.executor_nats import NATSExecutor


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

    def test_executor_module_carries_no_second_copy(self) -> None:
        """``arcgateway.executor`` must NOT define its own NATSExecutor.

        Regression guard. The class was extracted into ``executor_nats`` for the
        core LOC budget, but the original was left behind — so two independent
        classes with the same name coexisted, and which one you got depended on
        the import path. ``executor_nats`` is the single definition site.
        """
        import arcgateway.executor as executor_mod

        assert not hasattr(executor_mod, "NATSExecutor")

    def test_nats_executor_importable_directly(self) -> None:
        """NATSExecutor is importable from its one home."""
        from arcgateway.executor_nats import NATSExecutor as ExecutorNATSDirect

        assert ExecutorNATSDirect is NATSExecutor

    def test_nats_executor_instantiates(self) -> None:
        """NATSExecutor can be instantiated without arguments."""
        executor = NATSExecutor()
        assert executor is not None
