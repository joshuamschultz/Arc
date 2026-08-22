"""Signed, filesystem-backed shared knowledge for an Arc fleet.

This adapter deliberately has no ``arcagent`` dependency.  Its only fleet root
comes from :func:`arctrust.paths.arc_team`; callers cannot supply a document
path, and every persisted identifier is a digest-derived safe component.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from arcokf import Document, OKFValidationError, parse, render
from arctrust import canonical_json, verify_signature
from arctrust.classification import dominates, parse_classification
from arctrust.identity import did_matches_pubkey
from arctrust.paths import arc_team

from arcmemory.adapters.shared_knowledge import SharedKnowledgeDraft
from arcmemory.mdfile import atomic_write_text

_IDENTIFIER_RE = re.compile(r"^[a-f0-9]{16}$")


class _Access(Protocol):
    caller_did: str
    clearance: str


@dataclass(frozen=True)
class SharedKnowledgeReference:
    """Structural implementation of ArcAgent's injected knowledge reference."""

    scope: str
    identifier: str
    digest: str


@dataclass(frozen=True)
class SharedKnowledgeDocument:
    """One verified shared knowledge document."""

    reference: SharedKnowledgeReference
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class SharedKnowledgeHit:
    """A clearance-filtered lexical search result."""

    reference: SharedKnowledgeReference
    title: str
    excerpt: str


class FleetSharedKnowledgeBackend:
    """Persist signed OKF documents below ``arc_team()/shared/knowledge``.

    The signer registry is trust-on-first-use: a DID's first valid signature
    records its public key atomically; any later key substitution fails closed.
    Revocations are durable tombstones, so a stale file cannot reappear in read
    or search results merely because an index is rebuilt.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._documents = self._root / "documents"
        self._revocations = self._root / "revocations"
        self._audit = self._root / "audit"
        self._trust = self._root / "trusted-signers.json"

    @classmethod
    def for_arc_team(cls, base: Path | str | None = None) -> FleetSharedKnowledgeBackend:
        """Create the backend at Arc's canonical fleet-shared knowledge path."""
        return cls(arc_team(base=base) / "shared" / "knowledge")

    @property
    def root(self) -> Path:
        """Return the canonical shared-knowledge root for diagnostics/tests."""
        return self._root

    async def save(self, draft: SharedKnowledgeDraft, access: _Access) -> SharedKnowledgeReference:
        """Verify authority, signature, TOFU pinning, then atomically write valid OKF."""
        return await asyncio.to_thread(self._save, draft, access)

    async def read(self, reference: str, access: _Access) -> SharedKnowledgeDocument:
        """Read a non-revoked, signature-verified document within caller clearance."""
        return await asyncio.to_thread(self._read, reference, access)

    async def search(self, query: str, access: _Access) -> list[SharedKnowledgeHit]:
        """Search readable, non-revoked documents without revealing denied records."""
        return await asyncio.to_thread(self._search, query, access)

    async def revoke(self, reference: str, access: _Access) -> None:
        """Write an atomic owner-authorized revocation tombstone."""
        await asyncio.to_thread(self._revoke, reference, access)

    def _save(self, draft: SharedKnowledgeDraft, access: _Access) -> SharedKnowledgeReference:
        self._authorize_write(draft, access)
        self._verify_signature(draft)
        self._pin_signer(draft.owner_did, draft.public_key)
        identifier = self._identifier(draft)
        metadata = {
            "type": draft.document_type,
            "title": draft.title,
            "tags": list(draft.tags),
            "arc_classification": draft.classification,
            "arc_owner_did": draft.owner_did,
            "arc_content_sha256": draft.digest,
            "arc_signature": draft.signature,
            "arc_public_key": draft.public_key,
            "arc_signature_algorithm": draft.algorithm,
        }
        try:
            encoded = render(Document(metadata, draft.content))
            parse(encoded)
        except OKFValidationError as error:
            raise ValueError("invalid shared OKF knowledge document") from error
        atomic_write_text(self._document_path(identifier), encoded + "\n")
        self._audit_event("knowledge.saved", identifier, access.caller_did, draft.digest)
        return SharedKnowledgeReference("shared", identifier, draft.digest)

    def _read(self, reference: str, access: _Access) -> SharedKnowledgeDocument:
        identifier = self._validated_identifier(reference)
        if self._revocation_path(identifier).exists():
            raise FileNotFoundError("shared knowledge document is revoked")
        path = self._document_path(identifier)
        try:
            document = parse(path.read_bytes(), path=path.as_posix())
        except (OSError, OKFValidationError) as error:
            raise FileNotFoundError("shared knowledge document is unavailable") from error
        metadata = document.metadata
        try:
            title = str(metadata["title"])
            content = document.body.strip()
            classification = str(metadata["arc_classification"])
            owner_did = str(metadata["arc_owner_did"])
            digest = str(metadata["arc_content_sha256"])
            tags = tuple(str(value) for value in metadata["tags"])
            draft = SharedKnowledgeDraft(
                title=title,
                content=content,
                classification=classification,
                tags=tags,
                document_type=str(metadata["type"]),
                digest=digest,
                owner_did=owner_did,
                signature=str(metadata["arc_signature"]),
                public_key=str(metadata["arc_public_key"]),
                algorithm=str(metadata["arc_signature_algorithm"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("malformed shared knowledge frontmatter") from error
        self._verify_signature(draft)
        self._require_pinned_signer(draft.owner_did, draft.public_key)
        caller_clearance = parse_classification(access.clearance, strict=True)
        if not dominates(caller_clearance, parse_classification(classification, strict=True)):
            raise PermissionError("knowledge classification exceeds caller clearance")
        return SharedKnowledgeDocument(
            SharedKnowledgeReference("shared", identifier, digest),
            title,
            content,
            classification,
            tags,
        )

    def _search(self, query: str, access: _Access) -> list[SharedKnowledgeHit]:
        if not query.strip() or not self._documents.exists():
            return []
        matches: list[SharedKnowledgeHit] = []
        for path in sorted(self._documents.glob("*.md")):
            try:
                document = self._read(path.stem, access)
            except (FileNotFoundError, PermissionError, ValueError):
                continue
            if query.casefold() not in f"{document.title}\n{document.content}".casefold():
                continue
            matches.append(
                SharedKnowledgeHit(document.reference, document.title, document.content[:160])
            )
        return matches

    def _revoke(self, reference: str, access: _Access) -> None:
        document = self._read(reference, access)
        path = self._document_path(document.reference.identifier)
        parsed = parse(path.read_bytes(), path=path.as_posix())
        owner_did = str(parsed.metadata.get("arc_owner_did", ""))
        if access.caller_did != owner_did:
            raise PermissionError("only the shared knowledge owner can revoke it")
        payload = {
            "identifier": document.reference.identifier,
            "digest": document.reference.digest,
            "owner_did": owner_did,
            "revoked_by": access.caller_did,
            "revoked_at": datetime.now(UTC).isoformat(),
        }
        atomic_write_text(
            self._revocation_path(document.reference.identifier), json.dumps(payload) + "\n"
        )
        self._audit_event(
            "knowledge.revoked",
            document.reference.identifier,
            access.caller_did,
            document.reference.digest,
        )

    def _authorize_write(self, draft: SharedKnowledgeDraft, access: _Access) -> None:
        if access.caller_did != draft.owner_did:
            raise PermissionError("shared knowledge owner does not match caller")
        caller_clearance = parse_classification(access.clearance, strict=True)
        document_classification = parse_classification(draft.classification, strict=True)
        if not dominates(caller_clearance, document_classification):
            raise PermissionError("knowledge classification exceeds caller clearance")
        if not dominates(document_classification, caller_clearance):
            raise PermissionError("no-write-down forbids lower-classification shared knowledge")

    def _verify_signature(self, draft: SharedKnowledgeDraft) -> None:
        content_digest = "sha256:" + hashlib.sha256(draft.content.encode()).hexdigest()
        if content_digest != draft.digest:
            raise ValueError("shared knowledge content digest mismatch")
        try:
            public_key = base64.b64decode(draft.public_key, validate=True)
            signature = bytes.fromhex(draft.signature)
        except (ValueError, TypeError) as error:
            raise ValueError("malformed shared knowledge signature") from error
        if not did_matches_pubkey(draft.owner_did, public_key):
            raise PermissionError("shared knowledge signer does not match owner DID")
        payload = {
            "title": draft.title,
            "content": draft.content,
            "classification": draft.classification,
            "tags": list(draft.tags),
            "document_type": draft.document_type,
            "digest": draft.digest,
            "owner_did": draft.owner_did,
        }
        if not verify_signature(draft.algorithm, canonical_json(payload), signature, public_key):
            raise PermissionError("shared knowledge signature verification failed")

    def _pin_signer(self, did: str, public_key: str) -> None:
        trusted = self._load_trusted_signers()
        pinned = trusted.get(did)
        if pinned is not None and pinned != public_key:
            raise PermissionError("shared knowledge TOFU signer key changed")
        if pinned is None:
            trusted[did] = public_key
            atomic_write_text(self._trust, json.dumps(trusted, sort_keys=True) + "\n")

    def _require_pinned_signer(self, did: str, public_key: str) -> None:
        if self._load_trusted_signers().get(did) != public_key:
            raise PermissionError("shared knowledge signer is not TOFU-approved")

    def _load_trusted_signers(self) -> dict[str, str]:
        try:
            raw = (
                json.loads(self._trust.read_text(encoding="utf-8")) if self._trust.exists() else {}
            )
        except (OSError, ValueError) as error:
            raise ValueError("shared knowledge TOFU registry is malformed") from error
        if not isinstance(raw, dict) or any(
            not isinstance(did, str) or not isinstance(key, str) for did, key in raw.items()
        ):
            raise ValueError("shared knowledge TOFU registry is malformed")
        return raw

    def _audit_event(self, action: str, identifier: str, actor_did: str, digest: str) -> None:
        event = {
            "action": action,
            "identifier": identifier,
            "actor_did": actor_did,
            "digest": digest,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        event_name = hashlib.sha256(canonical_json(event)).hexdigest()
        atomic_write_text(
            self._audit / f"{event_name}.json", json.dumps(event, sort_keys=True) + "\n"
        )

    def _identifier(self, draft: SharedKnowledgeDraft) -> str:
        basis = f"{draft.owner_did}\0{draft.title}\0{draft.digest}".encode()
        return hashlib.sha256(basis).hexdigest()[:16]

    def _document_path(self, identifier: str) -> Path:
        return self._documents / f"{self._validated_identifier(identifier)}.md"

    def _revocation_path(self, identifier: str) -> Path:
        return self._revocations / f"{self._validated_identifier(identifier)}.json"

    @staticmethod
    def _validated_identifier(identifier: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(identifier):
            raise ValueError("invalid shared knowledge identifier")
        return identifier


__all__ = [
    "FleetSharedKnowledgeBackend",
    "SharedKnowledgeDocument",
    "SharedKnowledgeHit",
    "SharedKnowledgeReference",
]
