"""What a connection card shows, assembled from stored facts only (P18-1).

A card used to answer "is it working?" by reaching for the credential and spawning
the vendor CLI on every page view: each tab mounted two queries that read every
declared field three times and ran ``gog``, and every one of those reads was a
signed WORM row attributed to the operator. This module is the replacement: it
reads one health record per connection and the durable sync rows, and nothing
else. No credential is read, no process is spawned, no provider is called, so a
page view is a database read.

The probe loop (:mod:`arcui.connection_health`) is what keeps the record true.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import arcagent
from arcstore.source_sync import ArcStoreSourceSyncStore, SourceSyncRow
from starlette.requests import Request

from arcui.schemas import (
    ConnectionDisplayStatus,
    ConnectionHealthView,
    ConnectKind,
    KnowledgeSyncRow,
    LastNoticeView,
)

logger = logging.getLogger("arcui.connection_view")


def connect_kind(entry: arcagent.CatalogEntry | None) -> ConnectKind:
    """How an operator reconnects this connection, from its manifest alone.

    No secret is read to decide: ``oauth`` is a declared flow, ``host_login`` is a
    declared host command, ``token`` is a declared sensitive
    secret. A bundle that is no longer on the search path is ``none``.
    """
    if entry is None or entry.error:
        return "none"
    if entry.oauth:
        return "oauth"
    if any(
        required.token_command or required.authorize_command for required in entry.host_requires
    ):
        return "host_login"
    if any(secret.sensitive for secret in entry.secrets):
        return "token"
    return "none"


def display_status(
    record: arcagent.ConnectionRecord | None, sync_rows: Sequence[SourceSyncRow], now: datetime
) -> ConnectionDisplayStatus:
    """``syncing`` is derived from live leases, never stored: a crash cannot stick it."""
    status = record.status if record is not None else "unknown"
    if status in ("needs_you", "error"):
        return status
    if any(row.is_live(now) for row in sync_rows):
        return "syncing"
    return status


def _app_first_hint(shown: ConnectionDisplayStatus, missing_app: str) -> dict[str, Any]:
    """What the card shows when nothing can connect until the sign-in app exists.

    ``missing_app`` is the OAuth provider whose deployment slot is empty (``""`` when
    it is set up, or the connection has no OAuth flow). A healthy card ignores it: the
    connection already holds a grant.
    """
    if not missing_app or shown == "healthy":
        return {"app_missing": False}
    label = missing_app.capitalize()
    return {
        "app_missing": True,
        "reason_text": f"Set up the {label} sign-in app first",
        # Still `reconnect`: the OAuth panel opens on the app form while the slot is
        # empty. Only the words change, so the button says what it really does.
        "action": "reconnect",
        "action_label": f"Set up {label} sign-in app",
    }


def health_view(
    record: arcagent.ConnectionRecord | None,
    sync_rows: Sequence[SourceSyncRow],
    *,
    provider: str,
    now: datetime,
    missing_app: str = "",
) -> dict[str, Any]:
    """The health fields of a card row, as keyword arguments for the schema."""
    shown = display_status(record, sync_rows, now)
    hint = _app_first_hint(shown, missing_app)
    if record is None:
        return {"display_status": shown, **hint}
    notice = record.last_notice
    fields: dict[str, Any] = {
        "status": record.status,
        "display_status": shown,
        "reason_code": record.reason_code,
        "reason_text": record.reason_text,
        "action": record.action,
        "action_label": arcagent.action_label(record.action, provider=provider),
        "action_url": record.action_url,
        "last_checked_at": record.last_checked_at,
        "last_success_at": record.last_success_at,
        "last_notice": (
            LastNoticeView(
                kind=notice.kind, delivered=notice.delivered, channel=notice.channel, at=notice.at
            )
            if notice is not None
            else None
        ),
    }
    return {**fields, **hint}


def probe_view(record: arcagent.ConnectionRecord, *, provider: str) -> ConnectionHealthView:
    """A record as the probe route answers it (the same fields a card row carries)."""
    return ConnectionHealthView(
        **health_view(record, (), provider=provider, now=datetime.now(UTC))
    )


def knowledge_rows(
    rows: Sequence[SourceSyncRow], agent_names: Mapping[str, str], now: datetime
) -> list[KnowledgeSyncRow]:
    return [
        KnowledgeSyncRow(
            agent=agent_names.get(row.agent_did, row.agent_did),
            source_id=row.source_id,
            state=row.status.value,
            running=row.is_live(now),
            last_synced_at=row.last_synced_at.isoformat() if row.last_synced_at else None,
            pages=row.pages,
            error_code=row.error_code,
        )
        for row in rows
    ]


async def per_agent_rows(
    rows: Sequence[SourceSyncRow], subscriptions: arcagent.KnowledgeSubscriptions
) -> list[SourceSyncRow]:
    """One row per agent, even where agents share one sync (P18-4).

    A connection several agents read is synced once, under a principal that is no
    agent; its single row is shown on each subscriber's line, and the agent's own
    leftover row for that connection (from before it subscribed) is not shown.
    """
    readers: dict[str, list[str]] = {}
    for row in rows:
        if arcagent.is_knowledge_principal(row.agent_did):
            found = await subscriptions.for_connection(row.source_id)
            readers[row.source_id] = [subscription.agent_did for subscription in found]
    shown: list[SourceSyncRow] = []
    for row in rows:
        if arcagent.is_knowledge_principal(row.agent_did):
            shown.extend(
                row.model_copy(update={"agent_did": agent}) for agent in readers[row.source_id]
            )
        elif row.agent_did not in readers.get(row.source_id, ()):
            shown.append(row)
    return shown


def agent_names(request: Request) -> dict[str, str]:
    """Agent DID to the name a person reads, from the roster the app already holds."""
    provider = getattr(request.app.state, "roster_provider", None)
    if provider is None:
        return {}
    return {entry.did: entry.agent_id for entry in provider() if entry.did}


class CardContext:
    """One page view's worth of stored facts: every record and every sync row, read once."""

    def __init__(
        self,
        records: Mapping[str, arcagent.ConnectionRecord],
        sync_rows: Mapping[str, list[SourceSyncRow]],
        names: Mapping[str, str],
        now: datetime,
        missing_apps: frozenset[str] = frozenset(),
    ) -> None:
        self.records = records
        self.sync_rows = sync_rows
        self.names = names
        self.now = now
        self.missing_apps = missing_apps

    def fields(self, instance: str, *, provider: str, oauth_provider: str = "") -> dict[str, Any]:
        """Health and knowledge fields for one card row."""
        rows = self.sync_rows.get(instance, [])
        return {
            **health_view(
                self.records.get(instance),
                rows,
                provider=provider,
                now=self.now,
                missing_app=oauth_provider if oauth_provider in self.missing_apps else "",
            ),
            "knowledge_sync": knowledge_rows(rows, self.names, self.now),
        }


async def load_card_context(
    request: Request, instances: Sequence[str], *, missing_apps: frozenset[str] = frozenset()
) -> CardContext:
    """Read the health records (one list) and each connection's sync rows (one list each).

    An unreadable store degrades to "unknown" for every card rather than a 500: a
    status page that cannot read its status must still render.
    """
    backend = getattr(request.app.state, "arcstore_backend", None)
    records: dict[str, arcagent.ConnectionRecord] = {}
    sync_rows: dict[str, list[SourceSyncRow]] = {}
    if backend is not None:
        try:
            records = {
                r.connection: r for r in await arcagent.ConnectionStateStore(backend).list()
            }
            sync_store = ArcStoreSourceSyncStore(backend)
            subscriptions = arcagent.KnowledgeSubscriptions(backend, actor_did="did:arc:operator")
            for instance in instances:
                rows = await sync_store.list_for_connection(instance)
                sync_rows[instance] = await per_agent_rows(rows, subscriptions)
        except Exception:  # reason: the page must render with unknown statuses, not 500
            logger.exception("connection view: could not read health state")
    return CardContext(records, sync_rows, agent_names(request), datetime.now(UTC), missing_apps)


__all__ = [
    "CardContext",
    "agent_names",
    "connect_kind",
    "display_status",
    "health_view",
    "knowledge_rows",
    "load_card_context",
    "per_agent_rows",
    "probe_view",
]
