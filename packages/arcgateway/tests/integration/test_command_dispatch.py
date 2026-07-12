"""Integration tests: /new dispatch through the full SessionRouter.handle path.

Pins the crux guarantees:
  * /new rotates the session and does NOT spawn an agent task.
  * The command reply is delivered via the outbound adapter.
  * The NEXT normal message lands in the rotated (new, empty) session key.
  * An unpaired user gets the pairing flow, not a rotation (pairing runs first).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta, InboundEvent
from arcgateway.session import SessionRouter, build_session_key

_AGENT = "did:arc:agent:bot"
_USER = "did:arc:user:alice"


class _RecordingExecutor:
    """Records the session_key of every turn it is asked to run."""

    def __init__(self) -> None:
        self.seen_keys: list[str] = []

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        self.seen_keys.append(event.session_key)
        return self._stream(event)

    async def _stream(self, event: InboundEvent) -> AsyncIterator[Delta]:
        yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)


class _CapturingAdapter:
    name = "web"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, target: DeliveryTarget, message: str) -> None:
        self.sent.append((target.chat_id, message))


def _event(message: str, *, user_did: str = _USER) -> InboundEvent:
    return InboundEvent(
        platform="web",
        chat_id="chat-1",
        user_did=user_did,
        agent_did=_AGENT,
        session_key=build_session_key(_AGENT, user_did),
        message=message,
    )


@pytest.mark.asyncio
async def test_new_command_rotates_without_spawning_a_turn() -> None:
    executor = _RecordingExecutor()
    adapter = _CapturingAdapter()
    router = SessionRouter(executor=executor, adapter=adapter)

    before = router.current_session_key(_AGENT, _USER)
    await router.handle(_event("/new"))

    # No agent turn ran, and a confirmation was delivered.
    assert executor.seen_keys == []
    assert router.agent_tasks_spawned == {}
    assert adapter.sent and "fresh session" in adapter.sent[-1][1].lower()

    # The session rotated.
    after = router.current_session_key(_AGENT, _USER)
    assert after != before


@pytest.mark.asyncio
async def test_message_after_new_lands_in_the_rotated_session() -> None:
    executor = _RecordingExecutor()
    adapter = _CapturingAdapter()
    router = SessionRouter(executor=executor, adapter=adapter)

    rotated_key = router.current_session_key(_AGENT, _USER)
    await router.handle(_event("/new"))
    rotated_key = router.current_session_key(_AGENT, _USER)

    await router.handle(_event("hello in the fresh session"))
    # give the spawned session task a tick to start
    import asyncio

    await asyncio.sleep(0.05)

    assert executor.seen_keys == [rotated_key]


@pytest.mark.asyncio
async def test_help_command_lists_new_and_does_not_rotate() -> None:
    router = SessionRouter(executor=_RecordingExecutor(), adapter=_CapturingAdapter())
    adapter: Any = router._adapters["web"]

    before = router.current_session_key(_AGENT, _USER)
    await router.handle(_event("/help"))

    assert adapter.sent and "/new" in adapter.sent[-1][1]
    assert router.current_session_key(_AGENT, _USER) == before


@pytest.mark.asyncio
async def test_unpaired_user_gets_pairing_not_rotation() -> None:
    """Pairing runs before command dispatch: a stranger cannot rotate.

    Uses a non-trusted platform (telegram); web is token-authorized and exempt
    from pairing. An allowlist that excludes the user activates enforcement.
    """
    executor = _RecordingExecutor()
    adapter = _CapturingAdapter()
    router = SessionRouter(
        executor=executor,
        adapter=adapter,
        user_allowlist={"did:arc:user:someone-else"},
    )

    stranger = "did:arc:user:stranger"
    event = InboundEvent(
        platform="telegram",
        chat_id="chat-1",
        user_did=stranger,
        agent_did=_AGENT,
        session_key=build_session_key(_AGENT, stranger),
        message="/new",
    )
    before = router.current_session_key(_AGENT, stranger)
    await router.handle(event)

    # Rotation never happened — the command was gated by pairing.
    assert router.current_session_key(_AGENT, stranger) == before
    assert executor.seen_keys == []
