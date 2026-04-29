"""Decorator-form user_profile module — SPEC-021.

Two ``@hook`` functions mirror :class:`UserProfileModule`'s ``startup``
bus registrations:

  * ``agent:post_respond`` (priority 120) — log durable-fact extraction
    hints emitted by the LLM.  Observation-only; never auto-writes
    (ASI09 — agent asks, doesn't act unilaterally).
  * ``user.forgotten``     (priority 120) — trigger the GDPR tombstone
    workflow for the user identified by ``user_did`` in the event data.

Three ``@tool`` functions expose the module's public read/write/tombstone
API surface to the LLM:

  * ``user_profile_read``      — read a user profile (read_only).
  * ``user_profile_write``     — write a section of a user profile
                                 (state_modifying).
  * ``user_profile_tombstone`` — apply GDPR erasure for a user DID
                                 (state_modifying).

State is shared via :mod:`arcagent.modules.user_profile._runtime`. The
agent configures it once at startup; the hooks and tools read it lazily.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from arcagent.modules.user_profile import _runtime
from arcagent.modules.user_profile.errors import ACLViolation, BodyOverflow, ProfileNotFound
from arcagent.modules.user_profile.tombstone import apply_tombstone
from arcagent.tools._decorator import hook, tool

_logger = logging.getLogger("arcagent.modules.user_profile.capabilities")

# Module bus priority — after memory_acl (10) and after default (100),
# mirroring UserProfileModule._MODULE_PRIORITY.
_MODULE_PRIORITY = 120


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


@hook(event="agent:post_respond", priority=_MODULE_PRIORITY)
async def user_profile_post_respond(ctx: Any) -> None:
    """Log durable-fact extraction hints from the LLM response.

    The LLM may include a ``durable_fact_hints`` key in the response data
    with a list of suggested facts for the current user.  This hook logs
    them for operator review; it does NOT auto-write them (ASI09 — agent
    observes, doesn't act unilaterally on PII).
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


@hook(event="user.forgotten", priority=_MODULE_PRIORITY)
async def user_profile_user_forgotten(ctx: Any) -> None:
    """Trigger the GDPR tombstone workflow for a forgotten user.

    Expected data keys:
        ``user_did`` (required): DID of the user to erase.

    The tombstone:
    1. Deletes the profile file.
    2. Redacts session JSONLs field-wise.
    3. Emits ``session.fts5.reindex_needed``.
    4. Persists a compliance record (hash only, not the raw DID).
    """
    user_did = ctx.data.get("user_did", "")
    if not user_did:
        _logger.warning("user.forgotten event missing user_did; skipping")
        return
    st = _runtime.state()
    _logger.info("user_profile.tombstone_triggered user_did=%s", user_did)
    apply_tombstone(
        user_did,
        workspace=st.workspace,
        config=st.config,
        telemetry=st.telemetry,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    name="user_profile_read",
    description=(
        "Read the stored profile for a user. "
        "Returns the full markdown profile (identity, preferences, durable "
        "facts, derived) merged with any per-agent annotation overlay."
    ),
    classification="read_only",
    capability_tags=["user_profile", "memory"],
    when_to_use=(
        "Recall a user's identity, preferences, or accumulated durable facts "
        "before tailoring a response."
    ),
)
async def user_profile_read(
    user_did: str,
    agent_did: str | None = None,
) -> str:
    """Return the markdown profile for *user_did*.

    Results are wrapped in ``<user-profile>`` boundary markers to prevent
    stored content from being interpreted as instructions (LLM-01
    prompt-injection mitigation).

    Args:
        user_did:   DID of the user whose profile to read.
        agent_did:  Optional calling-agent DID; when provided the per-agent
                    annotation overlay is merged in.

    Returns:
        XML-wrapped markdown profile string.

    Raises:
        ProfileNotFound: if no profile exists for *user_did*.
    """
    st = _runtime.state()
    _audit(st, "memory.user_profile.read", {"user_did": user_did})
    try:
        markdown = st.store.read_user_profile(user_did, agent_did=agent_did)
    except ProfileNotFound:
        return f"No profile found for user_did={user_did!r}."
    return f'<user-profile user_did="{user_did}">\n{markdown}\n</user-profile>'


@tool(
    name="user_profile_write",
    description=(
        "Write content to a section of a user profile. "
        "Valid sections: identity, preferences, durable_facts. "
        "The durable_facts section is append-only — existing facts are never "
        "removed. The identity and preferences sections replace their body on "
        "each call. The derived section is regeneratable and cannot be written "
        "directly."
    ),
    classification="state_modifying",
    capability_tags=["user_profile", "memory"],
    when_to_use=(
        "Persist a long-lived fact, preference, or identity note about a user "
        "that should survive across sessions."
    ),
)
async def user_profile_write(
    user_did: str,
    section: str,
    content: str,
    source_session_id: str = "",
) -> str:
    """Write *content* to *section* of the user profile for *user_did*.

    Args:
        user_did:          DID of the target user.
        section:           One of ``identity``, ``preferences``,
                           ``durable_facts``.
        content:           Text to write.
        source_session_id: Session ID to embed in durable-fact provenance.

    Returns:
        Confirmation message on success, error description on failure.
    """
    st = _runtime.state()
    section_lower = section.lower()

    if section_lower == "derived":
        return (
            "The Derived section is regeneratable and cannot be written directly. "
            "Use the dialectic pipeline to regenerate it."
        )

    valid = {"identity", "preferences", "durable_facts"}
    if section_lower not in valid:
        return f"Unknown section {section!r}. Valid sections: {', '.join(sorted(valid))}."

    try:
        if section_lower == "durable_facts":
            st.store.append_durable_fact(
                user_did,
                content=content,
                source_session_id=source_session_id,
                ts=datetime.now(tz=UTC),
            )
        elif section_lower == "identity":
            _overwrite_section(st, user_did, "identity", content)
        else:
            _overwrite_section(st, user_did, "preferences", content)
    except BodyOverflow as exc:
        return (
            f"Profile body cap exceeded ({exc.body_size} bytes > "
            f"{exc.cap_bytes} bytes). Spill excess content to the episodic store."
        )
    except ACLViolation as exc:
        return f"ACL violation: {exc}"

    _audit(
        st,
        "memory.user_profile.write",
        {"user_did": user_did, "section": section_lower},
    )
    return f"Profile section {section_lower!r} written for user_did={user_did!r}."


@tool(
    name="user_profile_tombstone",
    description=(
        "Apply a GDPR right-to-be-forgotten tombstone for a user. "
        "This deletes the profile file, redacts session JSONLs, triggers an "
        "FTS5 reindex, and writes a compliance record. Irreversible."
    ),
    classification="state_modifying",
    capability_tags=["user_profile", "gdpr", "compliance"],
    when_to_use=(
        "When a user explicitly invokes their right to erasure under GDPR "
        "Art. 17 and the request has been verified and approved."
    ),
)
async def user_profile_tombstone(user_did: str) -> str:
    """Apply the GDPR tombstone workflow for *user_did*.

    This is an irreversible, destructive operation. The raw DID is NOT
    stored in the compliance record — only its SHA-256 hash.

    Args:
        user_did: DID of the user to erase.

    Returns:
        Summary of the tombstone action (files deleted, sessions redacted).
    """
    st = _runtime.state()
    _logger.info("user_profile.tombstone_tool_called user_did=%s", user_did)
    event = apply_tombstone(
        user_did,
        workspace=st.workspace,
        config=st.config,
        telemetry=st.telemetry,
    )
    return (
        f"Tombstone applied for user_did={user_did!r}. "
        f"Sessions redacted: {event.sessions_redacted}. "
        f"Compliance record written (hash={event.user_did_hash[:12]}...)."
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _overwrite_section(
    st: _runtime._State,
    user_did: str,
    section: str,
    content: str,
) -> None:
    """Overwrite a mutable profile section (identity or preferences)."""
    if not st.store.exists(user_did):
        profile = st.store.create_default(user_did)
    else:
        profile = st.store.read(user_did)

    if section == "identity":
        profile.identity_section = content
    else:
        profile.preferences_section = content

    st.store.write(profile)


def _audit(st: _runtime._State, event_name: str, data: dict[str, Any]) -> None:
    """Emit an audit event via telemetry, swallowing errors gracefully."""
    if st.telemetry is None:
        return
    try:
        st.telemetry.emit_event(event_name, data)
    except Exception:
        _logger.exception("Failed to emit audit event %s", event_name)


__all__ = [
    "user_profile_post_respond",
    "user_profile_read",
    "user_profile_tombstone",
    "user_profile_user_forgotten",
    "user_profile_write",
]
