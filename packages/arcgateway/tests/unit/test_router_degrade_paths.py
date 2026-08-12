"""Two fail-open paths that were reachable, load-bearing and untested.

SPEC-065 review. Both are places the gateway deliberately keeps going after
something went wrong, which is the right call — and exactly why they need
tests. A fail-open branch that stops doing its cleanup looks identical to a
healthy run right up until a consumer hangs forever or a reply disappears.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arcgateway.adapters.in_process import PythonAdapter
from arcgateway.executor import Delta, InboundEvent
from arcgateway.session import SessionRouter


def _event(chat_id: str = "c1") -> InboundEvent:
    return InboundEvent(
        platform="in_process",
        chat_id=chat_id,
        user_did="did:arc:user",
        agent_did="did:arc:alpha",
        message="hello",
        session_key=f"in_process:{chat_id}",
    )


class _ExplodingRouter:
    async def handle(self, _event: InboundEvent) -> None:
        raise RuntimeError("routing blew up before the executor ran")


# --- the consumer is never left waiting on a stream nobody will finish ------


async def test_a_routing_failure_terminates_the_stream_it_opened() -> None:
    """The cleanup is the whole reason a consumer can await this safely.

    Without the synthetic final delta the caller's ``DeltaStream`` waits for a
    ``done`` that no longer has a producer — a hang, not an error, and one that
    only ends on the inter-delta timeout if the caller set one.
    """
    adapter = PythonAdapter()

    with pytest.raises(RuntimeError, match="routing blew up"):
        await adapter.dispatch(_event(), router=_ExplodingRouter())  # type: ignore[arg-type]

    assert "c1" not in adapter._streams, (
        "the failed dispatch left its queue registered — the next dispatch for "
        "this chat_id would inherit a stream carrying another turn's deltas"
    )


# --- a reply with nowhere to go is reported, not silently swallowed ---------


async def test_a_reply_for_an_unregistered_platform_is_logged_not_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Command replies and pairing DMs both travel this path.

    Dropping one silently is how an operator ends up believing a slash command
    did nothing, when what actually happened is the answer had no adapter to
    ride home on.
    """
    router = SessionRouter(executor=_NullExecutor())  # type: ignore[arg-type]

    with caplog.at_level("WARNING"):
        await router._send_reply(_event(), "the answer nobody will hear")

    assert any(
        "no outbound adapter" in record.message for record in caplog.records
    ), "a reply was dropped with nothing in the log to say so"


class _NullExecutor:
    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        return None
