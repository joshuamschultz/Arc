"""The operator approval the running dispatch already holds — so nobody asks twice.

A connector egress crosses two gates in one dispatch: the registry's trifecta
gate, then the connection's own outbound gate (SPEC-062 ``ApprovalBinding``).
Before 2026-10-03 each asked the operator separately for the SAME call, so one
Dropbox upload cost two clicks.

When the trifecta gate admits a call on an operator's word (a one-shot Approve or
a standing "Always allow", both verified and pinned to the deployment operator),
the registry binds that fact here for the duration of that one execution. The
connection gate honours it only for the very same verb and the very same
arguments — the router may strip its selector argument, so the connection's
arguments must be a subset with identical values; it can never add one.

A ContextVar, set and reset by the registry around one execution, so concurrent
dispatches never see each other's approval.
"""

from __future__ import annotations

import contextvars
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApprovedDispatch:
    """One call the operator already approved in this dispatch."""

    tool_name: str
    arguments: Mapping[str, Any]
    #: ``one_shot`` or ``standing:<grant id>`` — recorded on the audit event.
    basis: str


_approved: contextvars.ContextVar[ApprovedDispatch | None] = contextvars.ContextVar(
    "arc_approved_dispatch", default=None
)


def bind_approved_dispatch(
    approved: ApprovedDispatch,
) -> contextvars.Token[ApprovedDispatch | None]:
    """Mark the running execution as operator-approved; returns the reset token."""
    return _approved.set(approved)


def reset_approved_dispatch(token: contextvars.Token[ApprovedDispatch | None]) -> None:
    """Restore the previous binding."""
    _approved.reset(token)


def approved_dispatch_for(tool_name: str, arguments: Mapping[str, Any]) -> ApprovedDispatch | None:
    """The approval covering exactly this call in the running dispatch, or None."""
    approved = _approved.get()
    if approved is None or approved.tool_name != tool_name:
        return None
    for name, value in arguments.items():
        if name not in approved.arguments or approved.arguments[name] != value:
            return None
    return approved


__all__ = [
    "ApprovedDispatch",
    "approved_dispatch_for",
    "bind_approved_dispatch",
    "reset_approved_dispatch",
]
