"""Loop checkpoint — serializable resumable state (SPEC-043 REQ-001..004).

arcrun *emits* a :class:`LoopCheckpoint` at each turn boundary through an
injected hook; it never persists. The caller (arcagent) writes it durably
(``SessionManager`` JSONL + arcstore WORM at ent/fed). Resume reconstructs the
``RunState`` from the checkpoint and re-enters the loop at the saved turn —
because the message list already carries every completed turn, resume redoes no
work and re-executes no side effects ("replay" = deterministic resume, OQ-4).

Boundary: this module holds no persistence, no token/classification types, no
sibling import. It is pure loop state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arcrun.state import RunState

# Scalar keys that serialize durably. The message transcript is NOT among them:
# it is already the durable session content the caller persists separately, so
# duplicating it inline would bloat every checkpoint (REQ-005, OQ-4).
_RECORD_KEYS = (
    "run_id",
    "parent_run_id",
    "strategy_name",
    "turn_count",
    "tokens_used",
    "cost_usd",
    "tool_calls_made",
    "tool_names",
    "completion_payload",
    "completion_tool",
    "max_turns",
    "max_tokens",
    "max_cost_usd",
)


@dataclass(frozen=True)
class LoopCheckpoint:
    """Immutable snapshot of resumable loop state at a turn boundary.

    ``tool_names`` is the frozen registry surface, verified on resume: a changed
    tool set is a poisoned resume and is refused fail-closed (REQ-004, ASI06).
    ``messages`` carries the transcript for in-process resume; it is excluded
    from :meth:`to_record` because the caller already persists it.
    """

    run_id: str
    parent_run_id: str
    strategy_name: str
    turn_count: int
    tokens_used: dict[str, int]
    cost_usd: float
    tool_calls_made: int
    tool_names: list[str]
    completion_payload: dict[str, Any] | None
    completion_tool: str | None
    max_turns: int
    max_tokens: int | None
    max_cost_usd: float | None
    messages: list[Any] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        """Serialize the scalar metadata (no transcript) for durable persistence."""
        return {key: getattr(self, key) for key in _RECORD_KEYS}

    @classmethod
    def from_record(cls, record: dict[str, Any], *, messages: list[Any]) -> LoopCheckpoint:
        """Rebuild a checkpoint from a persisted record plus the resumed transcript."""
        return cls(messages=list(messages), **{key: record[key] for key in _RECORD_KEYS})


def to_checkpoint(state: RunState) -> LoopCheckpoint:
    """Capture the resumable state of ``state`` at a turn boundary (REQ-001)."""
    return LoopCheckpoint(
        run_id=state.run_id,
        parent_run_id=state.parent_run_id,
        strategy_name=state.strategy_name,
        turn_count=state.turn_count,
        tokens_used=dict(state.tokens_used),
        cost_usd=state.cost_usd,
        tool_calls_made=state.tool_calls_made,
        tool_names=state.registry.names(),
        completion_payload=(
            dict(state.completion_payload) if state.completion_payload is not None else None
        ),
        completion_tool=state.completion_tool,
        max_turns=state.max_turns,
        max_tokens=state.max_tokens,
        max_cost_usd=state.max_cost_usd,
        messages=list(state.messages),
    )


def apply_checkpoint(state: RunState, cp: LoopCheckpoint) -> RunState:
    """Restore ``cp`` onto a freshly built ``state``, verifying the tool set.

    Fail-closed (REQ-004): if the reconstructed registry's tool-name set differs
    from the checkpoint's, resume is refused — a changed tool surface is a
    poisoned resume (ASI06). On success the resumable fields are copied back and
    the loop re-enters at ``cp.turn_count`` (REQ-003).
    """
    if set(state.registry.names()) != set(cp.tool_names):
        raise ValueError(
            "checkpoint resume refused: tool-name set changed since checkpoint "
            f"(checkpoint={sorted(cp.tool_names)}, current={sorted(state.registry.names())})"
        )
    state.messages = list(cp.messages)
    state.turn_count = cp.turn_count
    state.tokens_used = dict(cp.tokens_used)
    state.cost_usd = cp.cost_usd
    state.tool_calls_made = cp.tool_calls_made
    state.strategy_name = cp.strategy_name
    state.parent_run_id = cp.parent_run_id
    state.completion_payload = (
        dict(cp.completion_payload) if cp.completion_payload is not None else None
    )
    state.completion_tool = cp.completion_tool
    return state


__all__ = ["LoopCheckpoint", "apply_checkpoint", "to_checkpoint"]
