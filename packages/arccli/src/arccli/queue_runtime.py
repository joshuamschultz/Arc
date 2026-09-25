"""Compose one broker-fenced durable queue before exposing agents or controls."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

import arcrun
import arctrust

_EPOCH = re.compile(r"[1-9][0-9]{0,18}")
_UNFINISHED = frozenset({"queued", "running", "cancel_requested"})


class QueueRuntimeUnavailableError(RuntimeError):
    """The durable queue cannot prove an authoritative startup boundary."""


@dataclass(frozen=True, slots=True)
class QueueRuntime:
    """One initialized queue and the broker identity all consumers must reuse."""

    coordinator: arcrun.CallQueueCoordinator
    tenant_id: str
    owner_epoch: str
    recovered: int


async def _pending_epochs(store: arcrun.CallQueueStore, tenant_id: str) -> set[str]:
    epochs: set[str] = set()
    cursor: str | None = None
    while True:
        page = await store.recovery_page(cursor=cursor, limit=100)
        for job in page.jobs:
            if job.tenant_id != tenant_id:
                raise QueueRuntimeUnavailableError("queue recovery tenant mismatch")
            if job.state not in _UNFINISHED:
                continue
            epoch, separator, run_id = job.owner_id.partition(":")
            if not separator or not run_id or _EPOCH.fullmatch(epoch) is None:
                raise QueueRuntimeUnavailableError("queue recovery owner epoch invalid")
            epochs.add(epoch)
        if page.next_cursor is None:
            return epochs
        cursor = page.next_cursor


async def open_durable_queue(
    anchor: arctrust.QueueBrokerAnchor,
    *,
    journal_path: Path | None = None,
    limits: arcrun.QueueLimits | None = None,
    recovery_deadline_seconds: float = 300.0,
) -> QueueRuntime:
    """Open, initialize and fence all orphaned calls before returning readiness.

    The broker-issued lease anchors both root CAS and scoped record encryption.
    This function never creates local key material or an in-memory queue
    fallback. Recovery marks running calls uncertain; it never reissues calls.
    """
    if (
        _EPOCH.fullmatch(anchor.owner_epoch) is None
        or (
            anchor.fenced_through_epoch != "0"
            and _EPOCH.fullmatch(anchor.fenced_through_epoch) is None
        )
        or int(anchor.fenced_through_epoch) >= int(anchor.owner_epoch)
    ):
        raise ValueError("queue broker owner fence invalid")
    if not 0 < recovery_deadline_seconds <= 3600:
        raise ValueError("queue recovery deadline invalid")
    cipher = arctrust.VaultRecordCipher(
        arctrust.BrokerQueueByteCipher(anchor),
        tenant_id=anchor.tenant_id,
        journal_scope=anchor.scope,
        purpose="queue.metadata",
    )
    selected_limits = limits or arcrun.QueueLimits()
    path = journal_path if journal_path is not None else arctrust.queue_journal_file()
    try:
        async with asyncio.timeout(recovery_deadline_seconds):
            store = await asyncio.to_thread(
                arcrun.create_queue_journal,
                path,
                cipher,
                anchor,
                history_limit=selected_limits.history_limit,
                recovery_authority=anchor,
                tenant_scope=anchor.tenant_id,
            )
            coordinator = arcrun.CallQueueCoordinator(
                store=store, limits=selected_limits, tenant_scope=anchor.tenant_id
            )
            await coordinator.initialize()
            pending = await _pending_epochs(store, anchor.tenant_id)
            fenced = int(anchor.fenced_through_epoch)
            if any(int(epoch) > fenced for epoch in pending):
                raise QueueRuntimeUnavailableError("queue has an unfenced live owner")
            recovered = 0
            for epoch in sorted(pending, key=int):
                proof = await asyncio.to_thread(anchor.recovery_proof, epoch)
                recovered += await coordinator.recover(
                    owned_epoch=epoch,
                    recovery_proofs={anchor.tenant_id: proof},
                )
            if await _pending_epochs(store, anchor.tenant_id):
                raise QueueRuntimeUnavailableError("queue recovery remains unfinished")
            return QueueRuntime(coordinator, anchor.tenant_id, anchor.owner_epoch, recovered)
    except TimeoutError as exc:
        raise QueueRuntimeUnavailableError("queue recovery deadline expired") from exc
