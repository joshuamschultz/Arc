"""Task-local runtime state for the optional knowledge capability module."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from arcagent.knowledge import KnowledgeAccess, PersonalKnowledgePort, SharedKnowledgePort


@dataclass(frozen=True)
class _State:
    access: KnowledgeAccess
    personal: PersonalKnowledgePort | None
    shared: SharedKnowledgePort | None


_registry: dict[str, _State] = {}
_current_did: contextvars.ContextVar[str] = contextvars.ContextVar(
    "arcagent_knowledge_current_did", default=""
)


def configure(
    *,
    personal_knowledge_port: PersonalKnowledgePort | None = None,
    shared_knowledge_port: SharedKnowledgePort | None = None,
    agent_did: str = "",
    identity: object | None = None,
    clearance: str = "UNCLASSIFIED",
) -> None:
    """Bind injected ports and authoritative identity for knowledge tools."""
    authoritative_did = str(getattr(identity, "did", agent_did))
    identity_clearance = getattr(identity, "clearance", clearance)
    authoritative_clearance = str(getattr(identity_clearance, "name", identity_clearance))
    new_state = _State(
        KnowledgeAccess(authoritative_did, authoritative_clearance),
        personal_knowledge_port,
        shared_knowledge_port,
    )
    if not authoritative_did:
        raise RuntimeError("knowledge module requires an agent DID")
    _registry[authoritative_did] = new_state
    _current_did.set(authoritative_did)


def state() -> _State:
    """Return configured state or fail closed when the module was not wired."""
    did = _current_did.get()
    configured = _registry.get(did)
    if not did or configured is None or configured.access.caller_did != did:
        raise RuntimeError("knowledge module has no state for the running agent")
    return configured


def bind(state_obj: _State) -> None:
    """Rebind this agent's immutable state for its dispatch task."""
    _registry[state_obj.access.caller_did] = state_obj
    _current_did.set(state_obj.access.caller_did)


def reset() -> None:
    """Clear module state for isolated tests."""
    _registry.clear()
    _current_did.set("")
