"""Fleet-owned signed shared-knowledge persistence and authorization."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from arcokf import Document, OKFValidationError, parse, render
from arctrust import canonical_json, verify_signature
from arctrust.classification import dominates, parse_classification
from arctrust.identity import did_matches_pubkey
from arctrust.paths import arc_team

_IDENTIFIER_RE = re.compile(r"^[a-f0-9]{16}$")


class _Access(Protocol):
    @property
    def caller_did(self) -> str: ...

    @property
    def clearance(self) -> str: ...


class _Draft(Protocol):
    @property
    def title(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def classification(self) -> str: ...

    @property
    def tags(self) -> tuple[str, ...]: ...

    @property
    def document_type(self) -> str: ...

    @property
    def digest(self) -> str: ...

    @property
    def owner_did(self) -> str: ...

    @property
    def signature(self) -> str: ...

    @property
    def public_key(self) -> str: ...

    @property
    def algorithm(self) -> str: ...


@dataclass(frozen=True)
class SharedKnowledgeReference:
    scope: str
    identifier: str
    digest: str


@dataclass(frozen=True)
class SharedKnowledgeDocument:
    reference: SharedKnowledgeReference
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class SharedKnowledgeHit:
    reference: SharedKnowledgeReference
    title: str
    excerpt: str


@dataclass(frozen=True)
class SharedKnowledgeSummary:
    """One promoted document as the fleet read surface sees it (owner-attributed)."""

    reference: SharedKnowledgeReference
    title: str
    classification: str
    tags: tuple[str, ...]
    owner_did: str
    excerpt: str


class FleetSharedKnowledgeBackend:
    """Persist a fleet's signed knowledge, membership, and lifecycle decisions."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._documents = self._root / "documents"
        self._revocations = self._root / "revocations"
        self._audit = self._root / "audit"
        self._trust = self._root / "trusted-signers.json"
        self._trust_lock = self._root / ".trusted-signers.lock"

    @classmethod
    def for_arc_team(cls, base: Path | str | None = None) -> FleetSharedKnowledgeBackend:
        return cls(arc_team(base=base) / "shared" / "knowledge")

    @property
    def root(self) -> Path:
        return self._root

    async def save(self, draft: _Draft, access: _Access) -> SharedKnowledgeReference:
        return await asyncio.to_thread(self._save, draft, access)

    async def read(self, reference: str, access: _Access) -> SharedKnowledgeDocument:
        return await asyncio.to_thread(self._read, reference, access)

    async def search(self, query: str, access: _Access) -> list[SharedKnowledgeHit]:
        return await asyncio.to_thread(self._search, query, access)

    async def list_documents(self, access: _Access) -> list[SharedKnowledgeSummary]:
        return await asyncio.to_thread(self._list_documents, access)

    async def revoke(self, reference: str, access: _Access) -> None:
        await asyncio.to_thread(self._revoke, reference, access)

    def _save(self, draft: _Draft, access: _Access) -> SharedKnowledgeReference:
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
        _atomic_write_text(self._document_path(identifier), encoded + "\n")
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
            draft = _StoredDraft(
                title=str(metadata["title"]),
                content=document.body.strip(),
                classification=str(metadata["arc_classification"]),
                tags=tuple(str(value) for value in metadata["tags"]),
                document_type=str(metadata["type"]),
                digest=str(metadata["arc_content_sha256"]),
                owner_did=str(metadata["arc_owner_did"]),
                signature=str(metadata["arc_signature"]),
                public_key=str(metadata["arc_public_key"]),
                algorithm=str(metadata["arc_signature_algorithm"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("malformed shared knowledge frontmatter") from error
        self._verify_signature(draft)
        self._require_pinned_signer(draft.owner_did, draft.public_key)
        if not dominates(
            parse_classification(access.clearance, strict=True),
            parse_classification(draft.classification, strict=True),
        ):
            raise PermissionError("knowledge classification exceeds caller clearance")
        return SharedKnowledgeDocument(
            SharedKnowledgeReference("shared", identifier, draft.digest),
            draft.title,
            draft.content,
            draft.classification,
            draft.tags,
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
            if query.casefold() in f"{document.title}\n{document.content}".casefold():
                matches.append(
                    SharedKnowledgeHit(document.reference, document.title, document.content[:160])
                )
        return matches

    def _list_documents(self, access: _Access) -> list[SharedKnowledgeSummary]:
        """Every readable promoted document — classification-filtered, revocation-aware.

        Reuses ``_read`` per document, so a doc the caller may not read (no-read-up)
        or a revoked doc is skipped exactly as ``_search`` skips it. Attributes each
        surviving document to its owner DID so the operator sees who promoted what.
        """
        if not self._documents.exists():
            return []
        summaries: list[SharedKnowledgeSummary] = []
        for path in sorted(self._documents.glob("*.md")):
            try:
                document = self._read(path.stem, access)
            except (FileNotFoundError, PermissionError, ValueError):
                continue
            summaries.append(
                SharedKnowledgeSummary(
                    document.reference,
                    document.title,
                    document.classification,
                    document.tags,
                    self._owner(document.reference.identifier),
                    document.content[:160],
                )
            )
        return summaries

    def _revoke(self, reference: str, access: _Access) -> None:
        document = self._read(reference, access)
        if access.caller_did != self._owner(document.reference.identifier):
            raise PermissionError("only the shared knowledge owner can revoke it")
        payload = {
            "identifier": document.reference.identifier,
            "digest": document.reference.digest,
            "revoked_by": access.caller_did,
            "revoked_at": datetime.now(UTC).isoformat(),
        }
        _atomic_write_text(
            self._revocation_path(document.reference.identifier), json.dumps(payload) + "\n"
        )
        self._audit_event(
            "knowledge.revoked",
            document.reference.identifier,
            access.caller_did,
            document.reference.digest,
        )

    def _owner(self, identifier: str) -> str:
        parsed = parse(self._document_path(identifier).read_bytes())
        return str(parsed.metadata.get("arc_owner_did", ""))

    def _authorize_write(self, draft: _Draft, access: _Access) -> None:
        if access.caller_did != draft.owner_did:
            raise PermissionError("shared knowledge owner does not match caller")
        caller = parse_classification(access.clearance, strict=True)
        document = parse_classification(draft.classification, strict=True)
        if not dominates(caller, document):
            raise PermissionError("knowledge classification exceeds caller clearance")
        if not dominates(document, caller):
            raise PermissionError("no-write-down forbids lower-classification shared knowledge")

    def _verify_signature(self, draft: _Draft) -> None:
        if "sha256:" + hashlib.sha256(draft.content.encode()).hexdigest() != draft.digest:
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
        with self._signer_registry_lock():
            trusted = self._load_trusted_signers()
            pinned = trusted.get(did)
            if pinned is not None and pinned != public_key:
                raise PermissionError("shared knowledge TOFU signer key changed")
            if pinned is None:
                trusted[did] = public_key
                _atomic_write_text(self._trust, json.dumps(trusted, sort_keys=True) + "\n")

    @contextlib.contextmanager
    def _signer_registry_lock(self) -> Iterator[None]:
        self._root.mkdir(parents=True, exist_ok=True)
        with self._trust_lock.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _require_pinned_signer(self, did: str, public_key: str) -> None:
        if self._load_trusted_signers().get(did) != public_key:
            raise PermissionError("shared knowledge signer is not TOFU-approved")

    def _load_trusted_signers(self) -> dict[str, str]:
        try:
            raw = json.loads(self._trust.read_text()) if self._trust.exists() else {}
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
        _atomic_write_text(
            self._audit / f"{event_name}.json", json.dumps(event, sort_keys=True) + "\n"
        )

    def _identifier(self, draft: _Draft) -> str:
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


@dataclass(frozen=True)
class _StoredDraft:
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]
    document_type: str
    digest: str
    owner_did: str
    signature: str
    public_key: str
    algorithm: str


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


__all__ = [
    "FleetSharedKnowledgeBackend",
    "SharedKnowledgeDocument",
    "SharedKnowledgeHit",
    "SharedKnowledgeReference",
    "SharedKnowledgeSummary",
]
