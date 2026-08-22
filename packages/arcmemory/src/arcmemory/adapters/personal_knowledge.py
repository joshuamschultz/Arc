"""Direct-workspace adapter for personal, curated OKF knowledge documents."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from arctrust.classification import dominates, parse_classification
from arcmemory.mdfile import atomic_write_text


class _Draft(Protocol):
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]
    document_type: str


class _Access(Protocol):
    caller_did: str
    clearance: str


@dataclass(frozen=True)
class _Reference:
    scope: str
    identifier: str
    digest: str


@dataclass(frozen=True)
class _Document:
    reference: _Reference
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class _Hit:
    reference: _Reference
    title: str
    excerpt: str


@dataclass(frozen=True)
class _PromotionSource:
    reference: _Reference
    digest: str
    content: str
    classification: str


class PersonalKnowledgeAdapter:
    """Persist personal curated knowledge under one agent-owned workspace only."""

    def __init__(self, workspace: Path, agent_did: str) -> None:
        self._root = workspace.resolve() / "knowledge"
        self._agent_did = agent_did

    def _authorize(self, access: _Access, classification: str = "UNCLASSIFIED") -> None:
        if access.caller_did != self._agent_did:
            raise PermissionError("personal knowledge belongs to a different agent")
        if not dominates(
            parse_classification(access.clearance, strict=True),
            parse_classification(classification, strict=True),
        ):
            raise PermissionError("knowledge classification exceeds caller clearance")

    @staticmethod
    def _validate(draft: _Draft) -> None:
        if not draft.title.strip():
            raise ValueError("OKF title is required")
        if not draft.content.strip():
            raise ValueError("OKF content is required")
        if "[[" in draft.content or "]]" in draft.content:
            raise ValueError("OKF requires standard Markdown links")
        parse_classification(draft.classification, strict=True)

    def _path(self, identifier: str) -> Path:
        if not identifier.isalnum():
            raise ValueError("invalid knowledge identifier")
        return self._root / f"{identifier}.md"

    def _save(self, draft: _Draft, access: _Access) -> _Reference:
        self._authorize(access, draft.classification)
        self._validate(draft)
        digest = hashlib.sha256(draft.content.encode()).hexdigest()
        identifier = hashlib.sha256(f"{draft.title}\0{digest}".encode()).hexdigest()[:16]
        metadata = {
            "type": draft.document_type,
            "title": draft.title,
            "tags": list(draft.tags),
            "arc_classification": draft.classification,
            "arc_owner_did": self._agent_did,
            "arc_content_sha256": f"sha256:{digest}",
        }
        self._root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path(identifier),
            f"---\n{yaml.safe_dump(metadata, sort_keys=True)}---\n\n{draft.content}\n",
        )
        return _Reference("personal", identifier, f"sha256:{digest}")

    async def save(self, draft: _Draft, access: _Access) -> _Reference:
        """Persist directly through a worker thread, never the event loop."""
        return await asyncio.to_thread(self._save, draft, access)

    def _read(self, reference: str, access: _Access) -> _Document:
        path = self._path(reference)
        try:
            raw = path.read_text()
            _, front, content = raw.split("---", 2)
            metadata = yaml.safe_load(front)
        except (KeyError, OSError, TypeError, ValueError, yaml.YAMLError) as error:
            raise ValueError("malformed personal knowledge document") from error
        if not isinstance(metadata, dict):
            raise ValueError("malformed personal knowledge frontmatter")
        classification = str(metadata["arc_classification"])
        if metadata.get("arc_owner_did") != self._agent_did:
            raise ValueError("personal knowledge ownership was tampered")
        self._authorize(access, classification)
        digest = hashlib.sha256(content.strip().encode()).hexdigest()
        if metadata.get("arc_content_sha256") != f"sha256:{digest}":
            raise ValueError("personal knowledge content digest was tampered")
        return _Document(
            _Reference("personal", reference, f"sha256:{digest}"),
            str(metadata["title"]),
            content.strip(),
            classification,
            tuple(metadata["tags"]),
        )

    async def read(self, reference: str, access: _Access) -> _Document:
        """Read and integrity-check through a worker thread."""
        return await asyncio.to_thread(self._read, reference, access)

    async def search(self, query: str, access: _Access) -> list[_Hit]:
        self._authorize(access)
        if not self._root.exists():
            return []
        result: list[_Hit] = []
        paths = await asyncio.to_thread(lambda: tuple(self._root.glob("*.md")))
        for path in paths:
            document = await self.read(path.stem, access)
            if query.casefold() in f"{document.title}\n{document.content}".casefold():
                result.append(_Hit(document.reference, document.title, document.content[:160]))
        return result

    async def export_for_promotion(self, reference: str, access: _Access) -> _PromotionSource:
        document = await self.read(reference, access)
        return _PromotionSource(document.reference, document.reference.digest, document.content, document.classification)
