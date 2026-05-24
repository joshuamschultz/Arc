"""Tests for SessionRouter.dispatch_and_await — request/response primitive."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from arcgateway.executor import Delta, InboundEvent
from arcgateway.session import SessionRouter


def _make_event(
    *,
    session_key: str = "sess",
    user_did: str = "did:arc:user:alice",
) -> InboundEvent:
    return InboundEvent(
        platform="python",
        chat_id="abc",
        user_did=user_did,
        agent_did="did:arc:agent:bot",
        session_key=session_key,
        message="hi",
    )


class _ScriptedExecutor:
    """Yields whatever the test queued."""

    def __init__(self, deltas: list[Delta]) -> None:
        self._deltas = deltas

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Delta]:
        for d in self._deltas:
            yield d


@pytest.mark.asyncio
async def test_dispatch_and_await_yields_executor_deltas_in_order() -> None:
    executor = _ScriptedExecutor(
        [
            Delta(kind="token", content="hello", is_final=False),
            Delta(kind="token", content=" world", is_final=False),
            Delta(kind="done", content="", is_final=True),
        ]
    )
    router = SessionRouter(executor=executor, user_allowlist=None)

    seen = []
    async for delta in router.dispatch_and_await(_make_event()):
        seen.append(delta)

    assert [d.content for d in seen] == ["hello", " world", ""]
    assert seen[-1].is_final is True


@pytest.mark.asyncio
async def test_dispatch_and_await_blocks_unpaired_user() -> None:
    """When the pairing allowlist is restrictive, dispatch must refuse."""
    executor = _ScriptedExecutor(
        [Delta(kind="done", content="", is_final=True)]
    )
    # Restrict to a user that is NOT our event's user.
    router = SessionRouter(
        executor=executor,
        user_allowlist={"did:arc:user:somebody-else"},
    )

    with pytest.raises(PermissionError, match="pairing allowlist"):
        async for _ in router.dispatch_and_await(
            _make_event(user_did="did:arc:user:alice")
        ):
            pass


@pytest.mark.asyncio
async def test_dispatch_and_await_stops_at_final_delta() -> None:
    """Once a delta has is_final=True, iteration must end — even if the
    executor would yield more deltas after it (bug guard)."""
    executor = _ScriptedExecutor(
        [
            Delta(kind="done", content="", is_final=True),
            Delta(kind="token", content="leak", is_final=False),
        ]
    )
    router = SessionRouter(executor=executor, user_allowlist=None)

    contents = [d.content async for d in router.dispatch_and_await(_make_event())]
    assert contents == [""]
