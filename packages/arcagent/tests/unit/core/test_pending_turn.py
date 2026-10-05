"""A turn is live while it prepares: it takes messages and a cancel before any loop exists."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import arcrun
import pytest

from arcagent.core.session_coordination import SessionRunCoordinator


@pytest.mark.asyncio
async def test_preparing_turn_takes_messages_and_hands_them_to_the_loop() -> None:
    """A message held during Context prep lands on the loop's own queues, in order."""
    coordinator = SessionRunCoordinator()
    with coordinator.preparing("s", "run-1", interactive=True) as pending:
        target = coordinator.injection_target("s")
        assert target is pending
        await target.follow_up("did:arc:user:a", "first")
        await target.steer("did:arc:user:a", "second")
        handle = MagicMock(spec=arcrun.RunHandle)
        handle.state = MagicMock()
        handle.state.steer_queue = asyncio.Queue()
        handle.state.followup_queue = asyncio.Queue()

        coordinator.register("s", handle, interactive=True)

        assert coordinator.injection_target("s") is handle
        assert handle.state.followup_queue.get_nowait().message == "first"
        assert handle.state.steer_queue.get_nowait().message == "second"
        assert pending.state.followup_queue.empty()
    assert coordinator.active("s") is handle


def test_preparing_turn_that_fails_leaves_nothing_registered() -> None:
    coordinator = SessionRunCoordinator()
    with pytest.raises(RuntimeError), coordinator.preparing("s", "run-1", interactive=True):
        raise RuntimeError("prep failed")
    assert coordinator.active("s") is None
    assert coordinator.injection_target("s") is None


def test_background_preparing_turn_is_not_an_injection_target() -> None:
    coordinator = SessionRunCoordinator()
    with coordinator.preparing("s", "run-1", interactive=False) as pending:
        assert coordinator.active("s") is pending
        assert coordinator.injection_target("s") is None


@pytest.mark.asyncio
async def test_cancel_during_prep_is_replayed_onto_the_loop_that_follows() -> None:
    coordinator = SessionRunCoordinator()
    with coordinator.preparing("s", "run-1", interactive=True) as pending:
        await pending.follow_up("did:arc:user:a", "held")
        await pending.cancel("did:arc:operator", "stop")
        assert pending.cancelled
        assert pending.state.followup_queue.empty()

        handle = MagicMock(spec=arcrun.RunHandle)
        handle.state = MagicMock()
        handle.state.steer_queue = asyncio.Queue()
        handle.state.followup_queue = asyncio.Queue()
        handle.state.cancel_event = asyncio.Event()
        coordinator.register("s", handle, interactive=True)

        assert handle.state.cancel_event.is_set()
        assert handle.state.cancelled_by == "did:arc:operator"
        assert handle.state.cancel_reason == "stop"
        assert handle.state.followup_queue.empty()
