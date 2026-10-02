"""Causal attribution — who caused an audited act, and inside what.

Every audit record answers "who did this". Before this module the answer was
whoever held the signing key: the operator DID stood in for every UI page view,
connector probe and scheduled job. :class:`CausalContext` separates the two.
The **initiator** is the principal that caused the act (an agent, a browser
session, the scheduler, ...); the key that signs the WORM record is recorded
separately as the record's ``signer``.

One :class:`~contextvars.ContextVar` carries the context:

* :func:`bind` installs a root at a boundary — an HTTP request, an agent
  dispatch, a scheduler tick.
* :func:`refine` adds correlation ids (run, step, LLM call, tool call, workflow
  node, task, connection) inside it and can never change the initiator.
* :func:`delegate` hands the act to another principal acting on behalf of the
  current one (an agent running a tool for a browser session).
* :func:`spawn_detached` starts background work as its OWN root; it never
  inherits the request that happened to start it.

The context is set only by code at those boundaries. Nothing parses it from
tool arguments, model output or HTTP headers (LLM01, ASI03). An event built
with no binding records :data:`UNATTRIBUTED` as its actor so a missing binding
is loud in the ledger rather than silently borrowing someone's identity.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import uuid
from collections.abc import Coroutine, Iterator
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Initiator = Literal[
    "operator",
    "agent",
    "scheduler",
    "workflow",
    "ui_session",
    "connector_probe",
    "system",
]
"""The kinds of principal that can cause an audited act."""

UNATTRIBUTED = "did:arc:system:unattributed"
"""Actor recorded when no causal context is bound — a path that forgot to bind."""

_Id = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\x7f]+$"),
]

_T = TypeVar("_T")


class CausalContext(BaseModel):
    """Frozen causal chain for one act: the initiator plus correlation ids."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    initiator: Initiator
    initiator_id: _Id
    """DID (or pseudo-DID such as ``did:arc:ui:session:<sid>``) of the initiator."""

    on_behalf_of: _Id | None = None
    """The principal the initiator is acting for, when it is delegated work."""

    request_id: _Id
    """Minted per root binding: one HTTP request, one dispatch, one job tick."""

    run_id: _Id | None = None
    turn: int | None = Field(default=None, ge=0)
    step: int | None = Field(default=None, ge=0)
    llm_call_id: _Id | None = None
    tool_call_id: _Id | None = None
    workflow_run_id: _Id | None = None
    node_id: _Id | None = None
    task_id: _Id | None = None
    connection_id: _Id | None = None


_PRINCIPAL_FIELDS = frozenset({"initiator", "initiator_id", "on_behalf_of", "request_id"})

_causal: contextvars.ContextVar[CausalContext | None] = contextvars.ContextVar(
    "arctrust_causal", default=None
)


def current() -> CausalContext | None:
    """The causal context bound on this task, or ``None``."""
    return _causal.get()


def actor_did() -> str:
    """The bound initiator's id, or :data:`UNATTRIBUTED` when nothing is bound."""
    ctx = _causal.get()
    return ctx.initiator_id if ctx is not None else UNATTRIBUTED


def root(
    initiator: Initiator,
    initiator_id: str,
    *,
    on_behalf_of: str | None = None,
    **ids: Any,
) -> CausalContext:
    """Build a new root context with a freshly minted ``request_id``."""
    return CausalContext(
        initiator=initiator,
        initiator_id=initiator_id,
        on_behalf_of=on_behalf_of,
        request_id=uuid.uuid4().hex,
        **ids,
    )


@contextlib.contextmanager
def bind(ctx: CausalContext) -> Iterator[CausalContext]:
    """Install ``ctx`` for the duration of the block; restore the previous one after."""
    token = _causal.set(ctx)
    try:
        yield ctx
    finally:
        _causal.reset(token)


@contextlib.contextmanager
def refine(**ids: Any) -> Iterator[CausalContext]:
    """Add correlation ids to the bound context. Never changes who initiated.

    Raises:
        TypeError: An id tried to change the initiator, its id, the principal
            it acts for, or the request id.
        LookupError: Nothing is bound — refining nothing would invent a root.
    """
    forbidden = _PRINCIPAL_FIELDS.intersection(ids)
    if forbidden:
        raise TypeError(f"refine cannot change {sorted(forbidden)}; use delegate()")
    parent = _causal.get()
    if parent is None:
        raise LookupError("refine() needs a bound causal context; bind() a root first")
    with bind(_derive(parent, ids)) as child:
        yield child


@contextlib.contextmanager
def delegate(initiator: Initiator, initiator_id: str, **ids: Any) -> Iterator[CausalContext]:
    """Hand the act to ``initiator_id`` acting on behalf of the current initiator.

    Correlation ids and the request id carry over; the delegate becomes the
    actor. With nothing bound this is a plain root.
    """
    parent = _causal.get()
    if parent is None:
        with bind(root(initiator, initiator_id, **ids)) as ctx:
            yield ctx
        return
    behalf = parent.initiator_id if parent.initiator_id != initiator_id else parent.on_behalf_of
    principal = {"initiator": initiator, "initiator_id": initiator_id, "on_behalf_of": behalf}
    with bind(_derive(parent, {**ids, **principal})) as ctx:
        yield ctx


def spawn_detached(
    coro: Coroutine[Any, Any, _T],
    *,
    initiator: Initiator = "system",
    initiator_id: str,
    name: str | None = None,
) -> asyncio.Task[_T]:
    """Start ``coro`` as its own causal root; it never inherits the caller's context.

    ``asyncio.create_task`` copies the caller's context, so background work
    started while serving a request would otherwise be attributed to — and
    correlated with — that request.
    """
    detached = contextvars.copy_context()
    detached.run(_causal.set, root(initiator, initiator_id))
    return asyncio.get_running_loop().create_task(coro, name=name, context=detached)


def _derive(parent: CausalContext, update: dict[str, Any]) -> CausalContext:
    # Validate rather than model_copy(update=...): an update must pass the same
    # field constraints a fresh context does.
    return CausalContext.model_validate({**parent.model_dump(), **update})


__all__ = [
    "UNATTRIBUTED",
    "CausalContext",
    "Initiator",
    "actor_did",
    "bind",
    "current",
    "delegate",
    "refine",
    "root",
    "spawn_detached",
]
