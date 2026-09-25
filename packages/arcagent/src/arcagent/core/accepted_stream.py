"""Live transport projection around an injected accepted-run owner."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import datetime

import arcrun

from arcagent.core.run_contract import (
    AcceptedRunOwner,
    CanonicalRunRequest,
    RunAdmissionRefusedError,
)
from arcagent.streaming import DeliveryStreamEvent, DeliveryTerminalEvent, DeliveryTextEvent

_QUEUE_LIMIT = 32
_logger = logging.getLogger(__name__)
StreamFactory = Callable[[CanonicalRunRequest], AsyncIterator[arcrun.StreamEvent]]
Projector = Callable[[arcrun.StreamEvent], DeliveryStreamEvent | None]


async def stream_accepted_run(
    owner: AcceptedRunOwner,
    request: CanonicalRunRequest,
    *,
    signed_authorization: bytes,
    deadline: datetime,
    max_result_bytes: int,
    stream: StreamFactory,
    project: Projector,
) -> AsyncIterator[DeliveryStreamEvent]:
    """Yield live progress while the owner stores the terminal result first."""
    queue: asyncio.Queue[DeliveryStreamEvent | None] = asyncio.Queue(maxsize=_QUEUE_LIMIT)

    async def produce() -> None:
        sent_text = False
        last_sequence = 0

        async def invoke(accepted: CanonicalRunRequest) -> arcrun.RunResult:
            nonlocal sent_text, last_sequence

            async def observed() -> AsyncIterator[arcrun.StreamEvent]:
                nonlocal sent_text, last_sequence
                async for event in stream(accepted):
                    projection = project(event)
                    if isinstance(projection, DeliveryTextEvent):
                        sent_text = True
                    if projection is not None and not isinstance(
                        projection, DeliveryTerminalEvent
                    ):
                        last_sequence = max(last_sequence + 1, projection.sequence)
                        await queue.put(replace(projection, sequence=last_sequence))
                    yield event

            return await arcrun.collect(observed())

        try:
            result = await owner.execute(
                request,
                signed_authorization=signed_authorization,
                deadline=deadline,
                max_result_bytes=max_result_bytes,
                invoke=invoke,
            )
            if not sent_text and result.content:
                last_sequence += 1
                await queue.put(
                    DeliveryTextEvent(
                        run_id=request.run_id, sequence=last_sequence, text=result.content
                    )
                )
            last_sequence += 1
            await queue.put(
                DeliveryTerminalEvent(
                    run_id=request.run_id,
                    sequence=last_sequence,
                    status=(
                        "outcome_unknown" if result.outcome_unknown is not None else "completed"
                    ),
                )
            )
        except asyncio.CancelledError:
            raise
        except RunAdmissionRefusedError:
            last_sequence += 1
            await queue.put(
                DeliveryTerminalEvent(
                    run_id=request.run_id, sequence=last_sequence, status="failed"
                )
            )
        except Exception:
            _logger.exception("accepted run outcome unavailable: run_id=%s", request.run_id)
            last_sequence += 1
            await queue.put(
                DeliveryTerminalEvent(
                    run_id=request.run_id, sequence=last_sequence, status="outcome_unknown"
                )
            )
        finally:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    task = asyncio.create_task(produce(), name=f"accepted_stream:{request.run_id}")
    try:
        while item := await queue.get():
            yield item
            if isinstance(item, DeliveryTerminalEvent):
                break
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
