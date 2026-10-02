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

from arctrust import causal


@contextmanager
def llm_call_scope(turn: int | None = None) -> Iterator[None]:
    """Give one model call its own ``llm_call_id`` (and turn/step when known)."""
    if causal.current() is None:
        yield
        return
    with causal.refine(llm_call_id=uuid.uuid4().hex, turn=turn, step=turn):
        yield


@contextmanager
def tool_call_scope(tool_call_id: str, turn: int | None = None) -> Iterator[None]:
    """Attribute everything a tool does to its ``tool_call_id`` and turn."""
    if causal.current() is None:
        yield
        return
    with causal.refine(tool_call_id=tool_call_id, turn=turn, step=turn):
        yield
