"""Durable, idempotent inbox projection for gateway and agent messaging.

The message bus remains the delivery transport.  This service writes the
operator-facing communication record into ArcStore before a caller returns, so
the inbox neither disappears on a process restart nor depends on session JSONL.
"""

from __future__ import annotations

from arcstore.inbox_projection import DurableInboxService, participant

__all__ = ["DurableInboxService", "participant"]
