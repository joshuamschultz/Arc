"""arcstore — operational / observability data plane for Arc.

Ambient infrastructure: arcllm / arcrun / arcagent usage is auto-recorded to an
always-on local spool the moment it happens, independent of any running store,
server, DB, or UI. A later-started store layer backfills from the spool (and the
arctrust WORM) into a queryable backend.

This package depends on ``arctrust`` (for WORM verification in the store layer)
and on nothing else in Arc. Import direction never reverses:
``arctrust <- arcstore <- {arcllm, arcrun, arcagent, arcui}``.

Public surface (Phase 1 — spool)
--------------------------------
SpoolRecord     — flat, frozen Pydantic record written to the spool
record          — append one SpoolRecord to the spool (always-on, fail-open)
read            — iterate SpoolRecords from a spool file (skips corrupt lines)
spool_path      — default spool file path under the resolved Arc data dir
resolve_data_dir — env > default Arc data dir resolution (shared by all entry points)
"""

from __future__ import annotations

from arcstore.approval_dispatcher import (
    ApprovalDispatcherConfig,
    ApprovalNotification,
    ApprovalNotificationDispatcher,
)
from arcstore.config import ArcStoreConfig, resolve_data_dir
from arcstore.inbox import (
    Handoff,
    HandoffStatus,
    Inbox,
    InboxRepository,
    Message,
    MessagePage,
    PageInfo,
    Participant,
    ParticipantRole,
    ReadReceipt,
    ReadState,
    Thread,
    ThreadPage,
    TraceMetadata,
)
from arcstore.inbox_projection import DurableInboxService, InboxDeliveryPort
from arcstore.inbox_spool import InboxProjectionSpool, ProjectionEvent
from arcstore.records import SpoolRecord
from arcstore.source_sync import (
    ArcStoreSourceSyncStore,
    InMemorySourceSyncStore,
    SourceSyncBackend,
    SourceSyncLease,
    SourceSyncState,
    SourceSyncStatus,
)
from arcstore.spool import read, record, spool_path

__all__ = [
    "ApprovalDispatcherConfig",
    "ApprovalNotification",
    "ApprovalNotificationDispatcher",
    "ArcStoreConfig",
    "ArcStoreSourceSyncStore",
    "DurableInboxService",
    "Handoff",
    "HandoffStatus",
    "InMemorySourceSyncStore",
    "Inbox",
    "InboxDeliveryPort",
    "InboxProjectionSpool",
    "InboxRepository",
    "Message",
    "MessagePage",
    "PageInfo",
    "Participant",
    "ParticipantRole",
    "ProjectionEvent",
    "ReadReceipt",
    "ReadState",
    "SourceSyncBackend",
    "SourceSyncLease",
    "SourceSyncState",
    "SourceSyncStatus",
    "SpoolRecord",
    "Thread",
    "ThreadPage",
    "TraceMetadata",
    "read",
    "record",
    "resolve_data_dir",
    "spool_path",
]
