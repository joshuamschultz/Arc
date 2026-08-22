"""Task-local runtime state for the optional knowledge capability module."""

from __future__ import annotations

from dataclasses import dataclass

from arcagent.knowledge import KnowledgeAccess, PersonalKnowledgePort, SharedKnowledgePort


@dataclass(frozen=True)
class _State:
    access: KnowledgeAccess
    personal: PersonalKnowledgePort | None
    shared: SharedKnowledgePort | None


_state: _State | None = None


def configure(
    *,
    personal_knowledge_port: PersonalKnowledgePort | None = None,
    shared_knowledge_port: SharedKnowledgePort | None = None,
    agent_did: str = "",
    identity: object | None = None,
    clearance: str = "UNCLASSIFIED",
) -> None:
    """Bind injected ports and authoritative identity for knowledge tools."""
    global _state
    authoritative_did = str(getattr(identity, "did", agent_did))
    identity_clearance = getattr(identity, "clearance", clearance)
    authoritative_clearance = str(getattr(identity_clearance, "name", identity_clearance))
    _state = _State(
        KnowledgeAccess(authoritative_did, authoritative_clearance),
        personal_knowledge_port,
        shared_knowledge_port,
    )


def state() -> _State:
    """Return configured state or fail closed when the module was not wired."""
    if _state is None:
        raise RuntimeError("knowledge module is not configured")
    return _state
