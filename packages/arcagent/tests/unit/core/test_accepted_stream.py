"""Live progress waits for durable terminal ownership before success."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import arcrun

from arcagent.core.accepted_stream import stream_accepted_run
from arcagent.core.run_contract import CanonicalRunRequest, RunAdmissionRefusedError, RunInvoker
from arcagent.streaming import DeliveryTerminalEvent, DeliveryTextEvent


async def test_live_token_precedes_persisted_terminal_confirmation() -> None:
    release = asyncio.Event()
    committed = False
    request = CanonicalRunRequest(
        run_id="run-1",
        session_key="session-1",
        input_text="hello",
        purpose="message",
        occurrence_id="message-1",
    )

    class Owner:
        async def execute(
            self,
            accepted: CanonicalRunRequest,
            *,
            signed_authorization: bytes,
            deadline: datetime,
            max_result_bytes: int,
            invoke: RunInvoker,
        ) -> arcrun.RunResult:
            nonlocal committed
            assert signed_authorization == b"proof"
            result = await invoke(accepted)
            await release.wait()
            committed = True
            return result

    async def source(_: CanonicalRunRequest) -> AsyncIterator[arcrun.StreamEvent]:
        yield arcrun.TokenEvent(text="answer", run_id="run-1", sequence=1)
        yield arcrun.TurnEndEvent(final_text="answer", run_id="run-1", sequence=2)

    def project(event: arcrun.StreamEvent) -> DeliveryTextEvent | DeliveryTerminalEvent | None:
        if isinstance(event, arcrun.TokenEvent):
            return DeliveryTextEvent(run_id=event.run_id, sequence=event.sequence, text=event.text)
        if isinstance(event, arcrun.TurnEndEvent):
            return DeliveryTerminalEvent(
                run_id=event.run_id, sequence=event.sequence, status="completed"
            )
        return None

    stream = stream_accepted_run(
        Owner(),
        request,
        signed_authorization=b"proof",
        deadline=datetime(2030, 1, 1, tzinfo=UTC),
        max_result_bytes=100,
        stream=source,
        project=project,
    )
    first = await stream.__anext__()
    assert isinstance(first, DeliveryTextEvent) and first.text == "answer"
    assert not committed
    terminal = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    assert not terminal.done()
    release.set()
    final = await terminal
    assert committed
    assert isinstance(final, DeliveryTerminalEvent) and final.status == "completed"
    assert final.sequence > first.sequence
    await stream.aclose()


async def test_many_live_tokens_have_ordered_terminal_sequence() -> None:
    request = CanonicalRunRequest(
        run_id="run-ordered",
        session_key="session-1",
        input_text="hello",
        purpose="message",
        occurrence_id="message-1",
    )

    class Owner:
        async def execute(
            self,
            accepted: CanonicalRunRequest,
            *,
            signed_authorization: bytes,
            deadline: datetime,
            max_result_bytes: int,
            invoke: RunInvoker,
        ) -> arcrun.RunResult:
            return await invoke(accepted)

    async def source(_: CanonicalRunRequest) -> AsyncIterator[arcrun.StreamEvent]:
        for index in range(4):
            yield arcrun.TokenEvent(text="x", run_id=request.run_id, sequence=index + 1)
        yield arcrun.TurnEndEvent(final_text="xxxx", run_id=request.run_id, sequence=5)

    def project(event: arcrun.StreamEvent) -> DeliveryTextEvent | DeliveryTerminalEvent | None:
        if isinstance(event, arcrun.TokenEvent):
            return DeliveryTextEvent(run_id=event.run_id, sequence=event.sequence, text=event.text)
        return None

    events = [
        event
        async for event in stream_accepted_run(
            Owner(),
            request,
            signed_authorization=b"proof",
            deadline=datetime(2030, 1, 1, tzinfo=UTC),
            max_result_bytes=100,
            stream=source,
            project=project,
        )
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert isinstance(events[-1], DeliveryTerminalEvent)


async def test_rejected_signature_emits_failed_terminal_without_effect() -> None:
    request = CanonicalRunRequest(
        run_id="run-refused",
        session_key="session-1",
        input_text="hello",
        purpose="message",
        occurrence_id="message-1",
    )
    invoked = False

    class Owner:
        async def execute(
            self,
            accepted: CanonicalRunRequest,
            *,
            signed_authorization: bytes,
            deadline: datetime,
            max_result_bytes: int,
            invoke: RunInvoker,
        ) -> arcrun.RunResult:
            raise RunAdmissionRefusedError("signature rejected")

    async def source(_: CanonicalRunRequest) -> AsyncIterator[arcrun.StreamEvent]:
        nonlocal invoked
        invoked = True
        yield arcrun.TokenEvent(text="unsafe", run_id=request.run_id, sequence=1)

    events = [
        event
        async for event in stream_accepted_run(
            Owner(),
            request,
            signed_authorization=b"forged",
            deadline=datetime(2030, 1, 1, tzinfo=UTC),
            max_result_bytes=100,
            stream=source,
            project=lambda _event: None,
        )
    ]
    assert not invoked
    assert len(events) == 1
    assert isinstance(events[0], DeliveryTerminalEvent)
    assert events[0].status == "failed"
