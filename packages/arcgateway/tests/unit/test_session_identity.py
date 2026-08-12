"""Session identity: stable keys, rotation only via new-session (SPEC-065 REQ-304).

COMP-007 makes one module the sole owner of how a session key is derived and
rotated. These tests pin the *behaviour* that ownership is supposed to buy:

    * the same (agent, user) pair always resolves to the same key;
    * ordinary message handling never changes it;
    * only the explicit new-session command rotates it;
    * ``generation=0`` reproduces the pre-rotation digest byte for byte, so
      every session already on disk stays addressable.

That last one is a property of the stored DATA, not a compatibility shim: the
key is the filename under ``<workspace>/sessions/<key>.jsonl``, so a change to
the generation-0 formula orphans every existing conversation.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest

from arcgateway.executor import Delta, InboundEvent
from arcgateway.session import SessionRouter, build_session_key

AGENT = "did:arc:local:executor/abc"
USER = "did:arc:user:alice"


class _RecordingExecutor:
    """Executor that records the session_key of every event it is handed."""

    def __init__(self) -> None:
        self.seen_keys: list[str] = []

    async def run(self, event: InboundEvent) -> AsyncIterator[Delta]:
        self.seen_keys.append(event.session_key)

        async def _stream() -> AsyncIterator[Delta]:
            yield Delta(kind="done", is_final=True)

        return _stream()


class _StaticIdentityGraph:
    """Identity graph that maps every platform id onto one stable DID."""

    def __init__(self, resolved: str) -> None:
        self._resolved = resolved

    def resolve_user_identity(self, platform: str, platform_user_id: str) -> str:
        return self._resolved


def _event(session_key: str, message: str = "hello") -> InboundEvent:
    return InboundEvent(
        platform="web",
        chat_id="chat-1",
        user_did=USER,
        agent_did=AGENT,
        session_key=session_key,
        message=message,
    )


async def _drain(router: SessionRouter, event: InboundEvent) -> None:
    """Push an event through handle() and wait for its spawned turn to finish."""
    await router.handle(event)
    for _ in range(100):
        if router.active_session_count() == 0:
            return
        await asyncio.sleep(0)
    raise AssertionError("session turn did not complete")


# ---------------------------------------------------------------------------
# Stability
# ---------------------------------------------------------------------------


def test_same_pair_yields_the_same_key_on_every_call() -> None:
    """Derivation is a pure function of (agent, user) — no time, no counter."""
    keys = {build_session_key(AGENT, USER) for _ in range(10)}
    assert len(keys) == 1


def test_router_resolves_the_same_key_on_every_call() -> None:
    """``current_session_key`` is stable until something explicitly rotates."""
    router = SessionRouter(executor=cast(Any, _RecordingExecutor()))
    keys = {router.current_session_key(AGENT, USER) for _ in range(10)}
    assert keys == {build_session_key(AGENT, USER)}


def test_distinct_pairs_get_distinct_keys() -> None:
    """One conversation per (agent, user) — never a shared bucket."""
    assert build_session_key(AGENT, USER) != build_session_key(AGENT, "did:arc:user:bob")
    assert build_session_key(AGENT, USER) != build_session_key("did:arc:agent:other", USER)


# ---------------------------------------------------------------------------
# Data compatibility: generation 0 must reproduce the pre-existing digest
# ---------------------------------------------------------------------------


def test_generation_zero_reproduces_the_pre_existing_key_exactly() -> None:
    """The generation-0 digest is the on-disk contract for existing sessions.

    ``<workspace>/sessions/<key>.jsonl`` is named by this value. Recomputing
    it here from first principles means a change to the formula fails loudly
    rather than orphaning every stored conversation.
    """
    expected = hashlib.sha256(f"{AGENT}:{USER}".encode()).hexdigest()[:16]

    assert build_session_key(AGENT, USER) == expected
    assert build_session_key(AGENT, USER, generation=0) == expected
    assert len(expected) == 16


def test_generation_is_the_only_thing_that_changes_the_key() -> None:
    """A non-zero generation salts the seed; nothing else does."""
    plain = build_session_key(AGENT, USER)
    rotated = build_session_key(AGENT, USER, generation=1)

    assert rotated != plain
    assert rotated == hashlib.sha256(f"{AGENT}:{USER}:g1".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Rotation happens ONLY on the explicit new-session command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_message_handling_does_not_rotate_the_key() -> None:
    """Messages are not a rotation trigger — three turns, one session."""
    executor = _RecordingExecutor()
    router = SessionRouter(executor=cast(Any, executor))

    before = router.current_session_key(AGENT, USER)
    for i in range(3):
        await _drain(router, _event(before, message=f"m{i}"))

    assert router.current_session_key(AGENT, USER) == before
    assert executor.seen_keys == [before] * 3


@pytest.mark.asyncio
async def test_handle_rewrites_a_stale_key_rather_than_honouring_it() -> None:
    """A surface that supplies the wrong key does not get its own session.

    The router recomputes the canonical key before routing, which is what
    makes the owner authoritative even when an adapter guesses.
    """
    executor = _RecordingExecutor()
    router = SessionRouter(executor=cast(Any, executor))

    await _drain(router, _event("a-hand-rolled-key"))

    assert executor.seen_keys == [router.current_session_key(AGENT, USER)]


def test_new_session_is_what_rotates() -> None:
    """The explicit command changes the key; each call moves it again."""
    router = SessionRouter(executor=cast(Any, _RecordingExecutor()))

    first = router.current_session_key(AGENT, USER)
    second = router.new_session(AGENT, USER)
    third = router.new_session(AGENT, USER)

    assert router.current_session_key(AGENT, USER) == third
    assert len({first, second, third}) == 3


def test_rotation_is_scoped_to_the_pair_that_rotated() -> None:
    """One user's fresh start does not reset anyone else's conversation."""
    router = SessionRouter(executor=cast(Any, _RecordingExecutor()))
    other_before = router.current_session_key(AGENT, "did:arc:user:bob")

    router.new_session(AGENT, USER)

    assert router.current_session_key(AGENT, "did:arc:user:bob") == other_before


@pytest.mark.asyncio
async def test_messages_after_rotation_land_on_the_new_session() -> None:
    """Rotation is only real if the next turn actually writes somewhere new."""
    executor = _RecordingExecutor()
    router = SessionRouter(executor=cast(Any, executor))

    original = router.current_session_key(AGENT, USER)
    await _drain(router, _event(original))

    rotated = router.new_session(AGENT, USER)
    await _drain(router, _event(original, message="after"))

    assert executor.seen_keys == [original, rotated]


def test_rotation_survives_a_router_restart(tmp_path: Path) -> None:
    """A persisted generation must outlive the process, or /new un-rotates.

    The gateway restarts often; an in-memory-only counter would quietly send
    the user back to the conversation they just cleared.
    """
    db = tmp_path / "epochs.db"
    first = SessionRouter(executor=cast(Any, _RecordingExecutor()), session_epoch_db_path=db)
    rotated = first.new_session(AGENT, USER)

    second = SessionRouter(executor=cast(Any, _RecordingExecutor()), session_epoch_db_path=db)

    assert second.current_session_key(AGENT, USER) == rotated


# ---------------------------------------------------------------------------
# Every entry point must resolve the CURRENT key, not the generation-0 one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_and_await_uses_the_current_key_after_rotation() -> None:
    """Programmatic dispatch is a surface too — it must not re-derive.

    ``dispatch_and_await`` recomputes the session key when the identity graph
    resolves the user, and today it recomputes the *generation-0* key. After a
    ``/new`` that sends the turn back into the conversation the operator just
    cleared, because the rotation generation is skipped.
    """
    executor = _RecordingExecutor()
    router = SessionRouter(
        executor=cast(Any, executor),
        identity_graph=cast(Any, _StaticIdentityGraph(USER)),
    )
    router.add_approved_user(USER)

    rotated = router.new_session(AGENT, USER)

    event = _event(rotated).model_copy(update={"user_did": "did:arc:web:alice-raw"})
    async for _delta in router.dispatch_and_await(event):
        pass

    assert executor.seen_keys == [rotated]
