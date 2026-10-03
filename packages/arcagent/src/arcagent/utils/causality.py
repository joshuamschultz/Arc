"""The causal scopes the agent binds at its own boundaries (item 20).

:mod:`arctrust.causal` owns the context; this module decides what the agent's
boundaries bind into it:

* :func:`turn_root` — one agent turn is a fresh root performed by the agent on
  behalf of the principal that caused it. The principal comes from the signed
  request envelope (the channel user, the operator who approved a schedule, the
  teammate whose mail woke the agent) or the root its caller bound — never from
  tool arguments or message text (LLM01, ASI03).
* :func:`agent_scope` — a sub-run (compaction, a one-shot decision) stays inside
  the turn that started it, or roots itself when nothing is bound.
* :func:`correlate` — add correlation ids (a task, a connection) to the bound
  context, or root a loud fallback when nothing is bound.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from arctrust import causal

_CARRIED = ("workflow_run_id", "node_id", "task_id", "connection_id")
"""Ids a new turn inherits from the scope that dispatched it (a task, a workflow node)."""


def principal_of(bound: causal.CausalContext | None, agent_did: str) -> str | None:
    """Whom an act by ``agent_did`` serves, given what its caller bound."""
    if bound is None:
        return None
    if bound.initiator_id != agent_did:
        return bound.initiator_id
    return bound.on_behalf_of


@contextlib.contextmanager
def turn_root(
    agent_did: str, run_id: str, *, on_behalf_of: str | None = None
) -> Iterator[causal.CausalContext]:
    """Bind a fresh root for one agent turn.

    A new ``request_id`` per turn: two turns never share a root, however their
    callers' tasks interleave. Task and workflow ids of the dispatching scope
    carry over; run, step and call ids never do — this is a new run.
    """
    bound = causal.current()
    carried: dict[str, Any] = {}
    if bound is not None:
        carried = {key: getattr(bound, key) for key in _CARRIED if getattr(bound, key)}
    root = causal.root(
        "agent",
        agent_did,
        on_behalf_of=on_behalf_of or principal_of(bound, agent_did),
        run_id=run_id,
        **carried,
    )
    with causal.bind(root) as ctx:
        yield ctx


@contextlib.contextmanager
def agent_scope(agent_did: str, run_id: str) -> Iterator[causal.CausalContext]:
    """Enter a sub-run: refined into the agent's own turn, else its own root."""
    bound = causal.current()
    if bound is not None and bound.initiator == "agent" and bound.initiator_id == agent_did:
        with causal.refine(run_id=run_id) as ctx:
            yield ctx
        return
    with turn_root(agent_did, run_id) as ctx:
        yield ctx


@contextlib.contextmanager
def correlate(
    *, fallback: tuple[causal.Initiator, str], **ids: Any
) -> Iterator[causal.CausalContext]:
    """Refine ``ids`` onto the bound context, or root ``fallback`` with them.

    The fallback names who acts when no boundary bound anything (the agent for
    its own connector call); an act is never left without an initiator.
    """
    if causal.current() is None:
        initiator, initiator_id = fallback
        with causal.bind(causal.root(initiator, initiator_id, **ids)) as ctx:
            yield ctx
        return
    with causal.refine(**ids) as ctx:
        yield ctx


__all__ = ["agent_scope", "correlate", "principal_of", "turn_root"]
