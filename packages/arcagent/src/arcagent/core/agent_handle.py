"""AgentHandle — control interface for a running agent execution.

Sibling of ``arcagent.core.agent``. Wraps ArcRun's RunHandle to add
agent-level lifecycle: bus events (``agent:post_respond``), audit
trail, and session management are deferred until ``result()`` is
awaited.

Re-exported through ``arcagent.core.agent`` so existing imports
(``from arcagent.core.agent import AgentHandle``) keep working unchanged.
"""

from __future__ import annotations

from typing import Any

from arcllm import Message
from arcrun import RunHandle

from arcagent.core.module_bus import ModuleBus
from arcagent.core.session_internal import SessionManager
from arcagent.core.telemetry import AgentTelemetry

_MAX_STEERING_MESSAGE_LEN = 32_768  # 32 KiB — generous but bounded


def _validate_steering_message(message: str) -> None:
    """Validate a steer/follow_up message.

    Prevents empty or oversized payloads from reaching the queue.
    """
    if not message or not message.strip():
        msg = "Steering message must not be empty"
        raise ValueError(msg)
    if len(message) > _MAX_STEERING_MESSAGE_LEN:
        msg = f"Steering message exceeds {_MAX_STEERING_MESSAGE_LEN} character limit"
        raise ValueError(msg)


def _build_messages_dict(
    task: str, result: Any, messages: list[Any] | None
) -> list[dict[str, Any]]:
    """Serialize messages for agent:post_respond bus event.

    Uses model_dump() for Pydantic models; falls back to raw dict.
    Synthesizes a minimal exchange when no message history exists
    so memory modules can process single-turn run() calls.
    """
    if messages:
        return [m.model_dump() if hasattr(m, "model_dump") else m for m in messages]
    response_text = getattr(result, "content", None) or ""
    return [
        {"role": "user", "content": task},
        {"role": "assistant", "content": response_text},
    ]


class AgentHandle:
    """Control interface for a running agent execution.

    Wraps ArcRun's RunHandle to add agent-level lifecycle:
    bus events (agent:post_respond), audit trail, and session
    management are deferred until result() is awaited.

    Args:
        handle: The underlying ArcRun RunHandle.
        bus: Module bus for emitting agent-layer events.
        telemetry: Telemetry instance for audit events.
        session_id: Current session identifier (empty for run_async).
        task: The original task string.
        messages: Session message history, or None for single-turn.
        session: SessionManager for committing results (chat_async only).
    """

    def __init__(
        self,
        handle: RunHandle,
        bus: ModuleBus,
        telemetry: AgentTelemetry,
        session_id: str,
        task: str,
        messages: list[Message] | None,
        session: SessionManager | None = None,
        automated: bool = False,
    ) -> None:
        self._handle = handle
        self._bus = bus
        self._telemetry = telemetry
        self._session_id = session_id
        self._task = task
        self._messages = messages
        self._session = session
        self._automated = automated
        self._result_consumed = False
        self._completed = False

    async def steer(self, message: str) -> None:
        """Interrupt current execution with new direction."""
        self._check_not_completed("steer")
        _validate_steering_message(message)
        await self._handle.steer(message)
        self._telemetry.audit_event(
            "agent.steer",
            {"session_id": self._session_id, "message_len": len(message)},
        )

    async def follow_up(self, message: str) -> None:
        """Queue a follow-up message for end of current turn."""
        self._check_not_completed("follow_up")
        _validate_steering_message(message)
        await self._handle.follow_up(message)
        self._telemetry.audit_event(
            "agent.follow_up",
            {"session_id": self._session_id, "message_len": len(message)},
        )

    async def cancel(self) -> None:
        """Cancel execution. Returns partial result via result()."""
        self._check_not_completed("cancel")
        await self._handle.cancel()
        self._telemetry.audit_event(
            "agent.cancel",
            {"session_id": self._session_id},
        )

    async def result(self) -> Any:
        """Await completion and emit agent:post_respond.

        May only be called once. Raises RuntimeError on repeat calls
        to prevent duplicate bus events and session side effects.
        Commits assistant message and runs compaction when a session
        is attached (chat_async path).
        """
        if self._result_consumed:
            msg = "AgentHandle.result() has already been awaited"
            raise RuntimeError(msg)
        self._result_consumed = True

        loop_result = await self._handle.result()
        self._completed = True

        messages_dict = _build_messages_dict(self._task, loop_result, self._messages)
        await self._bus.emit(
            "agent:post_respond",
            {
                "result": loop_result,
                "messages": messages_dict,
                "session_id": self._session_id,
                "automated": self._automated,
            },
        )

        # Commit assistant response to session (mirrors chat() blocking path)
        if self._session is not None:
            response_text = getattr(loop_result, "content", None) or ""
            await self._session.append_message({"role": "assistant", "content": response_text})

        return loop_result

    @property
    def state(self) -> Any:
        """Read-only access to RunState."""
        return self._handle.state

    def _check_not_completed(self, method: str) -> None:
        """Raise if execution has already completed."""
        if self._completed:
            msg = f"Cannot call {method}() after result() has been awaited"
            raise RuntimeError(msg)
