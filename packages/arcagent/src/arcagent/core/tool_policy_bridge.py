"""Caller-DID binding bridge — ASI03 / LLM01 defence helpers.

Sibling of ``arcagent.core.tool_registry``. Owns the small set of
helpers that strip LLM-supplied identity fields from memory-tool
arguments and inject the agent's authoritative DID before execution.

Re-exported through ``arcagent.core.tool_registry`` so existing imports
(``from arcagent.core.tool_registry import _bind_caller_did,
   _is_memory_tool, _MEMORY_TOOL_PREFIXES, _IDENTITY_ARG_NAMES``) keep
working unchanged.
"""

from __future__ import annotations

from typing import Any

# Tool name prefixes that gate access to identity-scoped memory stores.
# Any tool whose name starts with one of these prefixes is subject to
# caller-DID binding: the transport layer strips any identity field the
# LLM may have supplied and injects the real agent DID from RunState.
_MEMORY_TOOL_PREFIXES: tuple[str, ...] = (
    "memory",
    "session",
    "user_profile",
)

# Argument names that an LLM could inject to impersonate another identity.
# These are stripped from memory tool arguments before execution and
# replaced with a single ``caller_did`` field set to the real agent DID.
_IDENTITY_ARG_NAMES: frozenset[str] = frozenset(
    {
        "caller_did",
        "user_did",
        "owner_did",
    }
)


def _is_memory_tool(tool_name: str) -> bool:
    """Return True if *tool_name* is an identity-scoped memory tool.

    Matches by prefix (``memory``, ``session``, ``user_profile``) using
    both dot-separated and underscore-separated conventions so callers
    don't need to normalise the name first.

    Examples::

        _is_memory_tool("memory.read")    → True
        _is_memory_tool("memory_search")  → True
        _is_memory_tool("session_search") → True
        _is_memory_tool("bash")           → False
    """
    for prefix in _MEMORY_TOOL_PREFIXES:
        # Accept both "prefix." and "prefix_" separators
        if (
            tool_name == prefix
            or tool_name.startswith(prefix + ".")
            or tool_name.startswith(prefix + "_")
        ):
            return True
    return False


def _bind_caller_did(
    tool_name: str,
    args: dict[str, Any],
    real_did: str,
    *,
    declared: frozenset[str] = frozenset(),
    telemetry: Any,
) -> dict[str, Any]:
    """Strip UNDECLARED identity fields and inject the real agent DID.

    This is the transport-layer defence against ASI03 (Identity & Privilege
    Abuse) and LLM01 (Prompt Injection via identity fields).

    For memory tools only:
    - Any field in ``_IDENTITY_ARG_NAMES`` that the tool does NOT declare in
      its schema is removed from the args copy. A declared ``user_did`` /
      ``owner_did`` is the tool's legitimate contract (e.g.
      ``user_profile_read(user_did=...)``), not an impersonation attempt, so it
      is preserved — stripping it would break the tool's required argument.
    - ``caller_did`` is set to *real_did* (the caller then drops it again for
      tools whose schema does not declare it).
    - If any undeclared identity field was stripped, a
      ``security.caller_did_override_attempt`` audit event is emitted so
      operators can detect injection probes.

    For non-memory tools the args dict is returned unchanged (no ``caller_did``
    injection) because most tools don't have an identity contract.

    Args:
        tool_name: Name of the tool being called.
        args: Original arguments dict (NOT mutated).
        real_did: The agent's authoritative DID from RunState/identity.
        declared: Property names declared in the tool's input schema. Identity
            fields in this set are the tool's real contract and are kept.
        telemetry: AgentTelemetry instance for audit events, or None.

    Returns:
        A new dict safe to pass to the tool executor.
    """
    if not _is_memory_tool(tool_name):
        # Non-memory tools: return a copy but do not inject caller_did —
        # most tools don't have an identity contract.
        return dict(args)

    # Strip only identity fields the tool does NOT legitimately declare.
    strip = _IDENTITY_ARG_NAMES - declared
    cleaned = {k: v for k, v in args.items() if k not in strip}

    # Detect injection attempt: did the LLM supply an undeclared identity field?
    stripped = [k for k in strip if k in args]
    if stripped and telemetry is not None:
        telemetry.audit_event(
            "security.caller_did_override_attempt",
            {
                "tool": tool_name,
                "stripped_fields": stripped,
                "injected_did": args.get("caller_did")
                or args.get("user_did")
                or args.get("owner_did"),
            },
        )

    # Always inject the real DID — even when the LLM didn't try to override.
    cleaned["caller_did"] = real_did
    return cleaned
