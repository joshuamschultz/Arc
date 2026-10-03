"""Causal scopes for arcrun: one place that refines run, turn, LLM-call and tool ids.

arcrun never decides WHO initiated a run — the caller binds that root
(:mod:`arctrust.causal`). It only adds the correlation ids it owns. With no
root bound the scopes are no-ops, so the missing binding stays visible as an
unattributed record instead of arcrun inventing an initiator.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from arctrust import causal

if TYPE_CHECKING:
    from arcrun.state import RunState


@contextmanager
def llm_call_scope(state: RunState | None = None) -> Iterator[None]:
    """Give one model call its own ``llm_call_id`` (and the run's turn when known).

    The id is also recorded on ``state`` so the tool calls this model call asks
    for can name it after this scope has closed.
    """
    if causal.current() is None:
        yield
        return
    llm_call_id = uuid.uuid4().hex
    turn = state.turn_count if state is not None else None
    if state is not None:
        state.llm_call_id = llm_call_id
    with causal.refine(llm_call_id=llm_call_id, turn=turn, step=turn):
        yield


@contextmanager
def tool_call_scope(tool_call_id: str, state: RunState) -> Iterator[None]:
    """Attribute everything a tool does to its ``tool_call_id``, turn and requesting model call."""
    if causal.current() is None:
        yield
        return
    turn = state.turn_count
    with causal.refine(
        tool_call_id=tool_call_id, llm_call_id=state.llm_call_id, turn=turn, step=turn
    ):
        yield
