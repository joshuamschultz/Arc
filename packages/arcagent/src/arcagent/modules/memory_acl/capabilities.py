"""Decorator-form memory_acl module — SPEC-021 unified capability surface.

Three ``@hook`` functions that mirror :class:`MemoryACLModule`'s
``startup`` registrations:

  * ``memory.read``    (priority 10) — ACL gate for cross-session reads.
  * ``memory.write``   (priority 10) — write requires ownership.
  * ``memory.search``  (priority 10) — ACL gate for searches.

State is shared via :mod:`arcagent.modules.memory_acl._runtime`. The
agent configures it once at startup; the hooks read state lazily.

The legacy :class:`MemoryACLModule` class is kept alongside this module
for tests and the per-turn capability issuance API
(``issue_capability`` / ``revoke_turn_capabilities`` / ``has_valid_capability``).
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.module_bus import EventContext
from arcagent.modules.memory_acl import _runtime
from arcagent.modules.memory_acl.acl import SessionACL, _extract_acl_from_session_data
from arcagent.tools._decorator import hook
from arcagent.utils.audit import safe_audit

_logger = logging.getLogger("arcagent.modules.memory_acl.capabilities")

# Priority 10 — runs before memory module (85) and all other subscribers.
_ACL_PRIORITY = 10


def _resolve_acl(data: dict[str, Any], owner_did: str) -> SessionACL:
    """Resolve the SessionACL from event data."""
    cfg = _runtime.state().config
    content = data.get("session_acl_content")
    if content and isinstance(content, str):
        return SessionACL.from_frontmatter(content, cfg, owner_did)
    acl_data = data.get("session_acl_data")
    if acl_data and isinstance(acl_data, dict):
        return _extract_acl_from_session_data(acl_data, cfg, owner_did)
    return SessionACL.default(cfg, owner_did)


async def _emit_veto_audit(
    *,
    event: str,
    caller_did: str,
    target_user_did: str,
    reason: str,
    classification: str,
) -> None:
    await safe_audit(
        _runtime.state().telemetry,
        "session.acl.veto",
        {
            "event": event,
            "caller_did": caller_did,
            "target_user_did": target_user_did,
            "classification": classification,
            "reason": reason,
        },
        logger=_logger,
    )


async def _emit_cross_session_read_audit(
    *,
    caller_did: str,
    target_user_did: str,
    classification: str,
) -> None:
    await safe_audit(
        _runtime.state().telemetry,
        "session.acl.cross_session_read",
        {
            "caller_did": caller_did,
            "target_user_did": target_user_did,
            "classification": classification,
        },
        logger=_logger,
    )


async def _check_and_veto(
    ctx: EventContext,
    operation: str,
    caller_did: str,
    target_user_did: str,
    acl: SessionACL,
) -> None:
    if caller_did and caller_did == target_user_did:
        return
    agent_did = ctx.agent_did
    if caller_did == agent_did and target_user_did == "":
        return
    if not acl.allows_read_by(caller_did, agent_did):
        reason = (
            f"ACL denied: session visibility='{acl.cross_session_visibility}' "
            f"does not permit access from caller='{caller_did}'"
        )
        await _emit_veto_audit(
            event=operation,
            caller_did=caller_did,
            target_user_did=target_user_did,
            reason=reason,
            classification=acl.classification,
        )
        ctx.veto(reason)
    else:
        await _emit_cross_session_read_audit(
            caller_did=caller_did,
            target_user_did=target_user_did,
            classification=acl.classification,
        )


@hook(event="memory.read", priority=_ACL_PRIORITY, name="memory_acl_read")
async def memory_acl_read(ctx: EventContext) -> None:
    """ACL gate for memory.read — vetoes unauthorized cross-session reads."""
    caller_did = ctx.data.get("caller_did", "")
    target_user_did = ctx.data.get("target_user_did", "")
    owner_did = target_user_did or ctx.data.get("owner_did", "")
    acl = _resolve_acl(ctx.data, owner_did)
    await _check_and_veto(ctx, "memory.read", caller_did, target_user_did, acl)


@hook(event="memory.write", priority=_ACL_PRIORITY, name="memory_acl_write")
async def memory_acl_write(ctx: EventContext) -> None:
    """ACL gate for memory.write — write requires ownership."""
    caller_did = ctx.data.get("caller_did", "")
    target_user_did = ctx.data.get("target_user_did", "")
    owner_did = target_user_did or ctx.data.get("owner_did", "")
    acl = _resolve_acl(ctx.data, owner_did)
    if caller_did != acl.owner_did and caller_did != ctx.agent_did:
        reason = (
            f"ACL denied: write requires ownership; "
            f"caller='{caller_did}' is not owner='{acl.owner_did}'"
        )
        await _emit_veto_audit(
            event="memory.write",
            caller_did=caller_did,
            target_user_did=target_user_did,
            reason=reason,
            classification=acl.classification,
        )
        ctx.veto(reason)


@hook(event="memory.search", priority=_ACL_PRIORITY, name="memory_acl_search")
async def memory_acl_search(ctx: EventContext) -> None:
    """ACL gate for memory.search — vetoes unauthorized cross-session searches."""
    caller_did = ctx.data.get("caller_did", "")
    target_user_did = ctx.data.get("target_user_did", "")
    owner_did = target_user_did or ctx.data.get("owner_did", "")
    acl = _resolve_acl(ctx.data, owner_did)
    await _check_and_veto(ctx, "memory.search", caller_did, target_user_did, acl)


__all__ = ["memory_acl_read", "memory_acl_search", "memory_acl_write"]
