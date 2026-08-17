"""What each agent publishes about what it holds — pointers, never contents.

An agent's memory is private and invisible to its teammates, which is why asking
each one *"is this message relevant to you?"* could never work: the agent holding
the answer had nothing to consult and answered from nothing (ADR-032).

A digest is the one artifact that crosses that boundary. It is a list of
pointers — a title, the proper nouns in it, the project tags — one line per
thing the agent filed. It carries no contents, each agent decides what it
publishes, and it is written when the artifact is filed rather than when someone
asks. The agent that holds the NNL requirements becomes findable because it
published a pointer at ingest, not because it guessed correctly about itself.

Storage rides the same ``StorageBackend`` seam as the entity registry, so a
digest is durable wherever the team's registry already is.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from arcteam.storage import StorageBackend

DIGEST_COLLECTION = "messages/digests"

# A digest is a routing index, not an archive. Old pointers are dropped rather
# than accumulated: a digest that grows without bound stops being cheap to rank
# and starts being a second copy of the memory it only meant to point at.
MAX_ENTRIES = 200

_TITLE_MAX = 200
_LINE_BREAK = re.compile(r"[\r\n]+")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest_key(agent_did: str) -> str:
    """Flatten a DID into a storage key, as the registry does for entities."""
    return agent_did.replace(":", "_").replace("/", "_")


class DigestEntry(BaseModel):
    """One published pointer to one artifact an agent holds.

    ``title`` is the artifact's name as its owner filed it. ``entities`` are the
    proper nouns and identifiers that name it — the tokens a person would
    actually type when looking for it, and the ones dense retrieval is worst at.
    ``tags`` are the project or domain labels its owner attached.
    """

    artifact_id: str
    title: str = ""
    entities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    kind: str = "artifact"
    updated: str = ""

    def as_document(self) -> str:
        """The single line a ranker indexes for this pointer."""
        parts = [self.title, " ".join(self.entities), " ".join(self.tags)]
        return " ".join(part for part in parts if part).strip()


class AgentDigest(BaseModel):
    """One agent's published index of what its private memory holds."""

    agent_did: str
    handle: str = ""
    entries: list[DigestEntry] = Field(default_factory=list)
    updated: str = ""

    def as_document(self) -> str:
        """Every pointer this agent published, as one document to rank."""
        return "\n".join(entry.as_document() for entry in self.entries)

    def with_entry(self, entry: DigestEntry) -> AgentDigest:
        """This digest plus *entry*, newest first, replacing any same artifact.

        Re-filing an artifact must update its pointer rather than add a second
        one — a digest that lists the same document twice ranks its owner higher
        for having repeated itself.
        """
        kept = [existing for existing in self.entries if existing.artifact_id != entry.artifact_id]
        return AgentDigest(
            agent_did=self.agent_did,
            handle=self.handle,
            entries=[entry, *kept][:MAX_ENTRIES],
            updated=_now(),
        )


def summarize_artifact(text: str, *, artifact_id: str, kind: str = "artifact") -> DigestEntry:
    """Derive a publishable pointer from an artifact's text.

    Deliberately mechanical — no model call. This runs on every ingest, so it
    must cost nothing, and a title plus the proper nouns already in the text is
    enough for a lexical ranker to find the right owner. Anything cleverer
    belongs in the agent's own hands: it can publish a better entry itself.
    """
    stripped = text.strip()
    first_line = _LINE_BREAK.split(stripped, maxsplit=1)[0] if stripped else ""
    return DigestEntry(
        artifact_id=artifact_id,
        title=first_line[:_TITLE_MAX],
        entities=extract_entities(stripped),
        kind=kind,
        updated=_now(),
    )


_ENTITY = re.compile(r"\b(?:[A-Z]{2,}\d*|[A-Z][a-z]+(?:[A-Z][a-z]+)+|[A-Z][a-z]{2,})\b")
_MAX_ENTITIES = 24


def extract_entities(text: str) -> list[str]:
    """Acronyms, CamelCase names and capitalised words, in first-seen order.

    ``NNL`` is the shape that matters: a rare all-caps token is the strongest
    signal a document has about what it is, and it is exactly what dense
    embeddings smear into a neighbourhood of similar-sounding projects.
    """
    seen: dict[str, None] = {}
    for match in _ENTITY.finditer(text):
        seen.setdefault(match.group(0), None)
        if len(seen) >= _MAX_ENTITIES:
            break
    return list(seen)


class DigestStore:
    """Reads and writes published digests over the team's storage backend."""

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    async def get(self, agent_did: str) -> AgentDigest | None:
        record: dict[str, Any] | None = await self._backend.read(
            DIGEST_COLLECTION, _digest_key(agent_did)
        )
        return AgentDigest.model_validate(record) if record else None

    async def publish(self, digest: AgentDigest) -> None:
        """Replace this agent's published digest.

        An agent may only ever write its own: the key is derived from the DID on
        the digest, so publishing on another agent's behalf is not expressible.
        """
        stamped = digest.model_copy(update={"updated": digest.updated or _now()})
        await self._backend.write(
            DIGEST_COLLECTION, _digest_key(digest.agent_did), stamped.model_dump()
        )

    async def add_entry(self, agent_did: str, handle: str, entry: DigestEntry) -> AgentDigest:
        """Fold one new pointer into this agent's digest and publish the result."""
        current = await self.get(agent_did) or AgentDigest(agent_did=agent_did, handle=handle)
        updated = current.with_entry(entry)
        await self.publish(updated)
        return updated

    async def list_digests(self) -> list[AgentDigest]:
        """Every published digest, which is the whole candidate set to rank."""
        records = await self._backend.query(DIGEST_COLLECTION)
        return [AgentDigest.model_validate(record) for record in records]


__all__ = [
    "DIGEST_COLLECTION",
    "MAX_ENTRIES",
    "AgentDigest",
    "DigestEntry",
    "DigestStore",
    "extract_entities",
    "summarize_artifact",
]
