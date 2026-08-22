"""Typed authority carried into an ArcFlow runner mutation.

The lease owner obtains a :class:`RunnerFence`; mutable-plane writes supplied
with that fence must validate it while they hold the same lock/transaction that
performs the write.  A caller must never turn this into a read-then-write
preflight check: a replacement runner can acquire the next token in between.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

RUNNER_LEASE_COLLECTION = "workflow_runner_leases"
RUNNER_LEASE_KEY = "default"


@dataclass(frozen=True)
class RunnerFence:
    """The monotonic authority granted to one workflow-runner owner."""

    owner_id: str
    token: int
    expires_at: datetime


class MutationFenceRejectedError(RuntimeError):
    """A runner tried to write after its lease token stopped being current."""


__all__ = [
    "RUNNER_LEASE_COLLECTION",
    "RUNNER_LEASE_KEY",
    "MutationFenceRejectedError",
    "RunnerFence",
]
