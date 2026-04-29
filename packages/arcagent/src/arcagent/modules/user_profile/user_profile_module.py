"""UserProfileModule — per-user profile management module.

Responsibilities:
- Expose ``read_user_profile`` and ``write_user_profile`` methods for
  use by the agent and other modules.
- Subscribe to ``agent:post_respond`` to log durable-fact extraction
  hints emitted by the LLM (optional; gated by config).
- Subscribe to ``user.forgotten`` to trigger the GDPR tombstone workflow.
- Emit audit events on every read/write/tombstone via telemetry.

Boundary rules (SDD §3.6):
- This module does NOT touch ``bio_memory`` or ``memory`` — different layers.
- GDPR FTS5 reindex is triggered via event emission only (no direct import
  of SessionIndex).
- ACL enforcement lives in ``memory_acl`` module (priority 10); this
  module trusts that vetoes have already been applied by the time its
  handlers run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.modules.user_profile.config import UserProfileConfig
from arcagent.modules.user_profile.models import UserProfile
from arcagent.modules.user_profile.store import ProfileStore
from arcagent.modules.user_profile.tombstone import apply_tombstone

_logger = logging.getLogger("arcagent.modules.user_profile")

# Module bus priority — after memory_acl (10) and after default (100)
_MODULE_PRIORITY = 120


class UserProfileModule:
    """Module that manages per-user markdown profiles.

    Lifecycle:
        1. ``startup(ctx)`` — resolves workspace, creates ProfileStore,
           registers bus handlers.
        2. Event handlers fire on relevant bus events.
        3. ``shutdown()`` — no-op (no background tasks).

    Public methods (called by agent or other modules):
        ``read_user_profile(user_did)``
        ``write_user_profile(user_did, section, content, *, source_session_id)``
        ``apply_tombstone(user_did)``
    """

    name = "user_profile"

    def __init__(
        self,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._config = UserProfileConfig(**(config or {}))
        self._store: ProfileStore | None = None
        self._workspace: Path | None = None
        self._telemetry: Any | None = None

    # ------------------------------------------------------------------
    # Module Protocol
    # ------------------------------------------------------------------

    async def startup(self, ctx: ModuleContext) -> None:
        """Wire into the module bus and initialise the profile store."""
        self._workspace = ctx.workspace
        self._telemetry = ctx.telemetry
        self._store = ProfileStore(ctx.workspace, self._config, telemetry=ctx.telemetry)

        # Subscribe: optional durable-fact extraction hint on each response
        ctx.bus.subscribe(
            "agent:post_respond",
            self._on_post_respond,
            priority=_MODULE_PRIORITY,
            module_name=self.name,
        )

        # Subscribe: GDPR tombstone workflow
        ctx.bus.subscribe(
            "user.forgotten",
            self._on_user_forgotten,
            priority=_MODULE_PRIORITY,
            module_name=self.name,
        )

        _logger.info(
            "user_profile module started workspace=%s profile_dir=%s",
            ctx.workspace,
            self._config.profile_dir,
        )

    async def shutdown(self) -> None:
        """Clean up — nothing to do (no background tasks or open handles)."""
        _logger.debug("user_profile module shutdown")

    # ------------------------------------------------------------------
    # Public read/write API
    # ------------------------------------------------------------------

    def read_user_profile(self, user_did: str) -> UserProfile:
        """Read and return the profile for *user_did*.

        Raises:
            ProfileNotFound: if no profile exists.
            RuntimeError: if the module has not been started.
        """
        store = self._require_store()
        profile = store.read(user_did)
        self._audit("memory.user_profile.read", {"user_did": user_did})
        return profile

    def write_user_profile(
        self,
        user_did: str,
        section: str,
        content: str,
        *,
        source_session_id: str = "",
    ) -> UserProfile:
        """Write *content* to the given *section* of the user profile.

        Section rules (per SDD §3.6):
        - ``identity`` / ``preferences``: replace-OK (overwrite section body).
        - ``durable_facts``: append-only — content is added as a new fact;
          existing facts are never removed.
        - ``derived``: regeneratable — not directly writable via this method;
          raises ValueError.

        Raises:
            ValueError: invalid section or attempt to write Derived section.
            BodyOverflow: body cap exceeded; caller must spill to episodic.
        """
        store = self._require_store()
        section_lower = section.lower()

        if section_lower == "derived":
            raise ValueError(
                "The Derived section is regeneratable and cannot be written directly."
                " Use the dialectic pipeline to regenerate it."
            )

        if section_lower == "durable_facts":
            profile = store.append_durable_fact(
                user_did,
                content=content,
                source_session_id=source_session_id,
                ts=datetime.now(tz=UTC),
            )
        elif section_lower == "identity":
            profile = self._overwrite_section(store, user_did, "identity", content)
        elif section_lower == "preferences":
            profile = self._overwrite_section(store, user_did, "preferences", content)
        else:
            raise ValueError(
                f"Unknown section '{section}'. "
                "Valid sections: identity, preferences, durable_facts"
            )

        self._audit(
            "memory.user_profile.write",
            {"user_did": user_did, "section": section},
        )
        return profile

    def tombstone_user(self, user_did: str) -> None:
        """Apply GDPR tombstone for *user_did*.

        Orchestrates the full erasure workflow:
        - Deletes the profile file.
        - Redacts session JSONLs field-wise.
        - Emits ``session.fts5.reindex_needed``.
        - Persists a compliance tombstone record.

        Raises:
            RuntimeError: if the module has not been started.
        """
        if self._workspace is None:
            raise RuntimeError("UserProfileModule has not been started")
        apply_tombstone(
            user_did,
            workspace=self._workspace,
            config=self._config,
            telemetry=self._telemetry,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_post_respond(self, ctx: EventContext) -> None:
        """Optional hook for durable-fact extraction hints.

        The LLM may emit a ``durable_fact_hint`` key in the response data
        containing a list of suggested facts for the current user.  This
        handler logs them for operator review; it does NOT auto-write them
        (ASI09 — agent asks, doesn't act unilaterally).
        """
        hints = ctx.data.get("durable_fact_hints", [])
        if not hints:
            return
        user_did = ctx.data.get("user_did", "")
        _logger.info(
            "user_profile.durable_fact_hints user_did=%s hints=%r",
            user_did,
            hints,
        )

    async def _on_user_forgotten(self, ctx: EventContext) -> None:
        """Handle the ``user.forgotten`` event by applying the tombstone.

        Expected data keys:
            ``user_did`` (required): the DID of the user to erase.
        """
        user_did = ctx.data.get("user_did", "")
        if not user_did:
            _logger.warning("user.forgotten event missing user_did; skipping")
            return
        _logger.info("user_profile.tombstone_triggered user_did=%s", user_did)
        self.tombstone_user(user_did)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _require_store(self) -> ProfileStore:
        if self._store is None:
            raise RuntimeError("UserProfileModule has not been started; call startup() first.")
        return self._store

    def _overwrite_section(
        self,
        store: ProfileStore,
        user_did: str,
        section: str,
        content: str,
    ) -> UserProfile:
        """Overwrite a mutable section (identity or preferences)."""
        if not store.exists(user_did):
            profile = store.create_default(user_did)
        else:
            profile = store.read(user_did)

        if section == "identity":
            profile.identity_section = content
        else:
            profile.preferences_section = content

        store.write(profile)
        return profile

    def _audit(self, event_name: str, data: dict[str, Any]) -> None:
        """Emit an audit event via telemetry, swallowing errors."""
        if self._telemetry is None:
            return
        try:
            self._telemetry.emit_event(event_name, data)
        except Exception:
            _logger.exception("Failed to emit audit event %s", event_name)
