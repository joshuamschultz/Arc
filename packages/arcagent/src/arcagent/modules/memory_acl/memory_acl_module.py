"""MemoryACLModule — Module Bus veto guard for memory operations.

Subscribes to memory.read, memory.write, memory.search at priority 10
(runs BEFORE the existing memory module at 85/90).

Authorization flow per event:
1. Extract caller_did from event data (bound at transport layer).
2. Resolve the target session's ACL (from frontmatter or tier default).
3. If ACL denies the operation → ctx.veto(reason) + audit event.
4. If ACL allows → pass through; memory module at 85/90 proceeds.

Caller DID binding (ASI-03 / LLM-01 mitigation):
- The transport layer (_bind_caller_did in tool_registry.py) strips any
  LLM-supplied user_did or caller_did from tool call arguments and injects
  the real caller_did from RunState.
- This module trusts caller_did from event data because it was set by the
  transport layer, NOT by the LLM.

Defense in depth (SDD §3.6):
- Memory provider re-checks capability on every read (independent of bus).
- Bus veto is the first line; provider check is the second.
"""

from __future__ import annotations

import logging
from typing import Any

from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.modules.memory_acl.acl import SessionACL, _extract_acl_from_session_data
from arcagent.modules.memory_acl.capability_tokens import Capability, CapabilityStore
from arcagent.modules.memory_acl.config import MemoryACLConfig
from arcagent.utils.audit import safe_audit

_logger = logging.getLogger("arcagent.modules.memory_acl")

# Priority 10 — runs before memory module (85) and all other subscribers.
# No other module should claim priority 10 (see TX.1.5 architecture test).
_ACL_PRIORITY = 10


class MemoryACLModule:
    """Module Bus subscriber that vetoes unauthorized memory operations.

    Subscribes at priority 10 to ``memory.read``, ``memory.write``, and
    ``memory.search`` events. Vetoes any event that would violate the
    session ACL, then emits an audit telemetry event.

    The module is purely a gatekeeper — it does not perform any memory I/O.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        telemetry: Any = None,
        identity: Any = None,
    ) -> None:
        """Initialise module.

        Args:
            config: Optional dict matching MemoryACLConfig fields.
            telemetry: AgentTelemetry instance (or compatible duck-type).
            identity: AgentIdentity instance for signing capabilities.
        """
        self._config = MemoryACLConfig(**(config or {}))
        self._telemetry = telemetry
        self._capability_store = CapabilityStore(identity=identity)

    @property
    def name(self) -> str:
        return "memory_acl"

    async def startup(self, ctx: ModuleContext) -> None:
        """Register event handlers on the module bus at priority 10."""
        bus = ctx.bus

        bus.subscribe(
            "memory.read",
            self._on_memory_read,
            priority=_ACL_PRIORITY,
            module_name="memory_acl",
        )
        bus.subscribe(
            "memory.write",
            self._on_memory_write,
            priority=_ACL_PRIORITY,
            module_name="memory_acl",
        )
        bus.subscribe(
            "memory.search",
            self._on_memory_search,
            priority=_ACL_PRIORITY,
            module_name="memory_acl",
        )
        _logger.info(
            "MemoryACLModule started (tier=%s, priority=%d)",
            self._config.tier,
            _ACL_PRIORITY,
        )

    async def shutdown(self) -> None:
        """Clean up capability store on shutdown."""
        self._capability_store.clear_expired()
        _logger.info("MemoryACLModule shut down")

    # -- Internal helpers --

    def _resolve_acl(self, data: dict[str, Any], owner_did: str) -> SessionACL:
        """Resolve the SessionACL from event data.

        Event data may include:
        - ``session_acl_content``: raw markdown with frontmatter
        - ``session_acl_data``: pre-parsed dict with acl sub-dict
        - Neither: fall back to tier default
        """
        content = data.get("session_acl_content")
        if content and isinstance(content, str):
            return SessionACL.from_frontmatter(content, self._config, owner_did)

        acl_data = data.get("session_acl_data")
        if acl_data and isinstance(acl_data, dict):
            return _extract_acl_from_session_data(acl_data, self._config, owner_did)

        return SessionACL.default(self._config, owner_did)

    async def _emit_veto_audit(
        self,
        event: str,
        caller_did: str,
        target_user_did: str,
        reason: str,
        classification: str,
    ) -> None:
        """Emit session.acl.veto audit event (SDD §4.2).

        Delegates to the shared ``safe_audit`` helper so audit-path
        failures cannot break a policy-critical veto decision.
        """
        await safe_audit(
            self._telemetry,
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
        self,
        caller_did: str,
        target_user_did: str,
        classification: str,
    ) -> None:
        """Emit session.acl.cross_session_read audit event (SDD §4.2)."""
        await safe_audit(
            self._telemetry,
            "session.acl.cross_session_read",
            {
                "caller_did": caller_did,
                "target_user_did": target_user_did,
                "classification": classification,
            },
            logger=_logger,
        )

    async def _check_and_veto(
        self,
        ctx: EventContext,
        operation: str,
        caller_did: str,
        target_user_did: str,
        acl: SessionACL,
    ) -> None:
        """Apply ACL check; veto if denied.

        Same-session reads (caller_did == target_user_did or caller is owner)
        are always allowed. Cross-session reads are governed by the ACL.
        """
        # Same-user access: always allowed (no cross-session boundary crossed)
        if caller_did and caller_did == target_user_did:
            return

        # Agent reading its own sessions: always allowed
        agent_did = ctx.agent_did
        if caller_did == agent_did and target_user_did == "":
            return

        # Cross-session or agent-DID mismatch: consult ACL
        if not acl.allows_read_by(caller_did, agent_did):
            reason = (
                f"ACL denied: session visibility='{acl.cross_session_visibility}' "
                f"does not permit access from caller='{caller_did}'"
            )
            await self._emit_veto_audit(
                event=operation,
                caller_did=caller_did,
                target_user_did=target_user_did,
                reason=reason,
                classification=acl.classification,
            )
            ctx.veto(reason)
        else:
            # Allowed cross-session read — still audit it
            await self._emit_cross_session_read_audit(
                caller_did=caller_did,
                target_user_did=target_user_did,
                classification=acl.classification,
            )

    async def _on_memory_read(self, ctx: EventContext) -> None:
        """Handle memory.read — check ACL, veto if unauthorized."""
        caller_did: str = ctx.data.get("caller_did", "")
        target_user_did: str = ctx.data.get("target_user_did", "")
        owner_did: str = target_user_did or ctx.data.get("owner_did", "")
        acl = self._resolve_acl(ctx.data, owner_did)
        await self._check_and_veto(ctx, "memory.read", caller_did, target_user_did, acl)

    async def _on_memory_write(self, ctx: EventContext) -> None:
        """Handle memory.write — check ACL, veto if unauthorized.

        Write operations are more restrictive: only the owner may write
        to their session memory, regardless of visibility setting.
        """
        caller_did: str = ctx.data.get("caller_did", "")
        target_user_did: str = ctx.data.get("target_user_did", "")
        owner_did: str = target_user_did or ctx.data.get("owner_did", "")
        acl = self._resolve_acl(ctx.data, owner_did)

        # Writes require ownership — no cross-session writes regardless of ACL
        if caller_did != acl.owner_did and caller_did != ctx.agent_did:
            reason = (
                f"ACL denied: write requires ownership; "
                f"caller='{caller_did}' is not owner='{acl.owner_did}'"
            )
            await self._emit_veto_audit(
                event="memory.write",
                caller_did=caller_did,
                target_user_did=target_user_did,
                reason=reason,
                classification=acl.classification,
            )
            ctx.veto(reason)

    async def _on_memory_search(self, ctx: EventContext) -> None:
        """Handle memory.search — check ACL, veto if unauthorized."""
        caller_did: str = ctx.data.get("caller_did", "")
        target_user_did: str = ctx.data.get("target_user_did", "")
        owner_did: str = target_user_did or ctx.data.get("owner_did", "")
        acl = self._resolve_acl(ctx.data, owner_did)
        await self._check_and_veto(ctx, "memory.search", caller_did, target_user_did, acl)

    # -- Capability management API (T2.3) --

    def issue_capability(
        self,
        *,
        caller_module: str,
        target_resource: str,
        allowed_actions: list[str],
        turn_id: str,
        ttl_seconds: float = 3600.0,
    ) -> Capability:
        """Issue a short-lived signed capability for a turn.

        Called by the orchestrator at the start of each turn to grant the
        memory module permission to read a specific user's profile.
        """
        return self._capability_store.issue(
            caller_module=caller_module,
            target_resource=target_resource,
            allowed_actions=allowed_actions,
            turn_id=turn_id,
            ttl_seconds=ttl_seconds,
        )

    def revoke_turn_capabilities(self, turn_id: str) -> int:
        """Revoke all capabilities issued for a completed turn.

        Returns the count of capabilities revoked.
        """
        count = self._capability_store.revoke_turn(turn_id)
        _logger.debug("Revoked %d capabilities for completed turn %s", count, turn_id)
        return count

    def has_valid_capability(
        self,
        *,
        caller_module: str,
        target_resource: str,
        action: str,
        turn_id: str,
    ) -> bool:
        """Defense-in-depth check: does a valid capability exist for this access?

        Called by memory providers to independently re-verify authorization,
        separate from the bus veto. If this returns False, the provider should
        refuse the operation even if the bus veto was not triggered.
        """
        return self._capability_store.has_valid_capability(
            caller_module=caller_module,
            target_resource=target_resource,
            action=action,
            turn_id=turn_id,
        )

    @property
    def capability_store(self) -> CapabilityStore:
        """Expose the store for testing and orchestrator wiring."""
        return self._capability_store

    @property
    def priority(self) -> int:
        """The module-bus priority this module subscribes at."""
        return _ACL_PRIORITY
