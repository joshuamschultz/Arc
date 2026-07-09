"""Checkpoint resume — the consumer that closes the crash-recovery loop (F5).

``SessionManager`` persists a signed :class:`~arcrun.LoopCheckpoint` at every
turn boundary (the WRITE path). This module is the missing READ path: given a
session key, it reloads the latest persisted checkpoint, **verifies its operator
signature** (fail-closed — F3), reconstructs the loop state from the checkpoint +
the durable transcript, and re-enters the REAL streaming loop with
``resume_from`` so an interrupted run continues to completion without redoing
completed work (REQ-003).

Lives outside ``arcagent/core`` (LOC budget). Drives the same
``arcrun.run_stream`` production path as :func:`dispatch_stream`, so resume is
gated, budgeted, checkpointed, and audited identically to a fresh run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from arcllm import Message
from arcrun import LoopCheckpoint, StreamEvent, TurnEndEvent
from arcrun import run_stream as arcrun_run_stream

from arcagent.core.agent_dispatch import build_run_context, maybe_compact
from arcagent.core.session_internal.capability_ledger import bind_session_id, reset_session_id
from arcagent.tools.approval_policy import build_loop_controls
from arcagent.tools.checkpoint_signing import verify_record

if TYPE_CHECKING:
    from arcagent.core.agent import ArcAgent


async def resume_stream(agent: ArcAgent, *, session_key: str) -> AsyncIterator[StreamEvent]:
    """Resume an incomplete persisted run from its latest signed checkpoint.

    Yields the resumed run's ``StreamEvent``s and commits the final assistant
    turn. A no-op (empty stream) when the session has no persisted checkpoint. A
    tampered or unsigned checkpoint raises fail-closed before the loop starts —
    an agent cannot reset its budget counters by editing the record (F3, LLM10).
    """
    agent._ensure_started()
    session = await agent.session(session_key)
    record = session.latest_checkpoint()
    if record is None:
        return  # nothing to resume — no persisted checkpoint

    signer = agent._operator_signer
    if signer is not None:
        verify_record(record, public_key=signer.public_key, algorithm=signer.algorithm)

    _telemetry, bus, model, provider, system_prompt, bridge = await build_run_context(agent, "")
    transcript = [Message(**m) for m in session.get_messages()]
    # apply_checkpoint (in arcrun) replaces the loop's message list with this one,
    # so the freshly-assembled system prompt must lead it — the transcript on disk
    # never carries the system message (it is rebuilt every run).
    cp = LoopCheckpoint.from_record(
        record, messages=[Message(role="system", content=system_prompt), *transcript]
    )
    transform = agent._context.transform_context if agent._context else None

    final_text = ""
    session_token = bind_session_id(session.session_id)
    try:
        raw_stream = await arcrun_run_stream(
            model=model,
            capabilities=provider,
            system_prompt=system_prompt,
            task="",
            messages=transcript,
            on_event=bridge,
            transform_context=transform,
            actor_did=agent._identity.did if agent._identity else None,
            store_raw_bodies=agent._config.telemetry.capture_tool_io,
            resume_from=cp,
            **build_loop_controls(agent, session),
        )
        async for event in raw_stream:
            if isinstance(event, TurnEndEvent):
                final_text = event.final_text
            yield event
    finally:
        reset_session_id(session_token)

    await session.append_message({"role": "assistant", "content": final_text})
    await maybe_compact(agent, session)
    if bus is not None:
        await bus.emit(
            "agent:post_respond",
            {
                "result": None,
                "messages": [{"role": "assistant", "content": final_text}],
                "session_id": session.session_id,
                "automated": True,
            },
        )


__all__ = ["resume_stream"]
