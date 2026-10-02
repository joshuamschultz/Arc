"""Fleet-owned signed shared-knowledge persistence and authorization.

Layout under the collection root:

* ``documents/<id>.md`` — one signed OKF document per promoted non-entity card;
* ``entities/<id>/<slot>.md`` — one signed block per contributor of a canonical entity;
* ``revocations/<id>.json`` — a tombstone: an owner's revoke, or an operator's
  signed demote (alpha-2 item 16);
* ``provenance/<id>/<slot>.<digest>.json`` — who promoted which bytes, on what
  decision, signed by the contributor (alpha-2 item 16);
* ``audit/<sha>.json`` — one file per lifecycle event;
* ``trusted-signers.json`` — the TOFU pin of every contributor key.

Nothing is erased (NIST AU-9/AU-11). A revoke or demote moves the signed bytes out
of the live set — ``documents/revoked/<id>.md``, ``entities/<id>/revoked/
<slot>.<digest>.md`` — and the tombstone names them, so deleting a tombstone never
resurrects a document and every revoked byte stays verifiable.
"""

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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from arcokf import Document, OKFValidationError, parse, render
from arctrust import canonical_json, verify_signature
from arctrust.classification import dominates, parse_classification
from arctrust.identity import did_from_public_key, did_matches_pubkey
from arctrust.paths import arc_team

from arcteam.shared_knowledge.signed_records import RecordSigner, sign_record, verified_payload

_IDENTIFIER_RE = re.compile(r"^[a-f0-9]{16}$")
_ENTITY_TYPE = "entity"
_REVOKED_DIR = "revoked"
_MAX_REASON_CHARS = 500


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
class SharedKnowledgeContribution:
    """One contributor's signed provenance block inside a canonical shared entity."""

    contributor_did: str
    source_digest: str
    content: str


@dataclass(frozen=True)
class SharedKnowledgeDocument:
    reference: SharedKnowledgeReference
    title: str
    content: str
    classification: str
    tags: tuple[str, ...]
    contributions: tuple[SharedKnowledgeContribution, ...] = ()
    #: The card kind the document was promoted from (``insight`` / ``procedure`` /
    #: ``entity``; ``note`` and friends for direct promotions).
    kind: str = ""
    #: Every DID whose signed bytes make up the document (one for a plain document).
    contributors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SharedKnowledgeHit:
    reference: SharedKnowledgeReference
    title: str
    excerpt: str


@dataclass(frozen=True)
class SharedKnowledgeDemotion:
    """A verified operator demotion: the operator-signed tombstone of one document."""

    identifier: str
    digest: str
    demoted_by: str
    reason: str
    demoted_at: str


@dataclass(frozen=True)
class SharedKnowledgeProvenance:
    """Who promoted one contributor's bytes, when, and on what decision (signed)."""

    identifier: str
    contributor_did: str
    source_ref: str
    digest: str
    kind: str
    decision: str
    confidence: float | None
    classifier_version: str | None
    decided_by: str | None
    promoted_at: str


@dataclass(frozen=True)
class SharedKnowledgeSummary:
    """One promoted document as the fleet read surface sees it (contributor-attributed)."""

    reference: SharedKnowledgeReference
    title: str
    classification: str
    tags: tuple[str, ...]
    #: The first contributor; a canonical entity lists them all in ``contributors``.
    owner_did: str
    excerpt: str
    kind: str = ""
    contributors: tuple[str, ...] = ()
    #: Earliest verified provenance time of the live bytes; ``None`` when none recorded.
    promoted_at: str | None = None
    #: Set only on a demoted document (listed when ``include_demoted`` is asked).
    demotion: SharedKnowledgeDemotion | None = None


class FleetSharedKnowledgeBackend:
    """Persist a fleet's signed knowledge, membership, and lifecycle decisions.

    ``operator_public_key`` is the deployment operator's trust anchor. Only a
    signer holding that key may demote, and only tombstones it signed count as
    demotions; with no anchor, demote is refused (fail closed).
    """

    def __init__(self, root: Path, *, operator_public_key: bytes | None = None) -> None:
        self._root = root.resolve()
        self._documents = self._root / "documents"
        self._entities = self._root / "entities"
        self._revocations = self._root / "revocations"
        self._provenance = self._root / "provenance"
        self._audit = self._root / "audit"
        self._trust = self._root / "trusted-signers.json"
        self._trust_lock = self._root / ".trusted-signers.lock"
        self._operator_public_key = operator_public_key

    @classmethod
    def for_arc_team(
        cls, base: Path | str | None = None, *, operator_public_key: bytes | None = None
    ) -> FleetSharedKnowledgeBackend:
        return cls(
            arc_team(base=base) / "shared" / "knowledge", operator_public_key=operator_public_key
        )

    @property
    def root(self) -> Path:
        return self._root

    async def save(self, draft: _Draft, access: _Access) -> SharedKnowledgeReference:
        return await asyncio.to_thread(self._save, draft, access)

    async def authorize_save(self, draft: _Draft, access: _Access) -> None:
        """Run every check :meth:`save` runs before writing, and write nothing.

        Owner == caller, clearance / no-write-down, not revoked or demoted, digest +
        signature, signer bound to the owner DID, and the TOFU pin (read-only: a
        first key is not pinned here). Lets a caller record a decision only once
        the write would be accepted; :meth:`save` re-runs the same checks, so
        nothing is trusted across the gap.
        """
        await asyncio.to_thread(self._authorize_save, draft, access)

    async def read(self, reference: str, access: _Access) -> SharedKnowledgeDocument:
        return await asyncio.to_thread(self._read, reference, access)

    async def search(self, query: str, access: _Access) -> list[SharedKnowledgeHit]:
        return await asyncio.to_thread(self._search, query, access)

    async def list_documents(
        self, access: _Access, *, include_demoted: bool = False
    ) -> list[SharedKnowledgeSummary]:
        return await asyncio.to_thread(self._list_documents, access, include_demoted)

    async def revoke(self, reference: str, access: _Access) -> None:
        await asyncio.to_thread(self._revoke, reference, access)

    async def demote(
        self, reference: str, access: _Access, operator: RecordSigner, reason: str
    ) -> SharedKnowledgeDemotion:
        return await asyncio.to_thread(self._demote, reference, access, operator, reason)

    async def demotions(self) -> dict[str, SharedKnowledgeDemotion]:
        return await asyncio.to_thread(self._demotions)

    async def record_provenance(
        self, reference: SharedKnowledgeReference, signer: RecordSigner, fields: dict[str, Any]
    ) -> None:
        await asyncio.to_thread(self._record_provenance, reference, signer, fields)

    async def provenance(
        self, reference: str, access: _Access
    ) -> tuple[SharedKnowledgeProvenance, ...]:
        return await asyncio.to_thread(self._provenance_of, reference, access)

    # -- write ---------------------------------------------------------------------

    def _authorize_save(self, draft: _Draft, access: _Access) -> None:
        self._authorize_write(draft, access)
        self._verify_signature(draft)
        pinned = self._load_trusted_signers().get(draft.owner_did)
        if pinned is not None and pinned != draft.public_key:
            raise PermissionError("shared knowledge TOFU signer key changed")

    def _save(self, draft: _Draft, access: _Access) -> SharedKnowledgeReference:
        self._authorize_write(draft, access)
        self._verify_signature(draft)
        self._pin_signer(draft.owner_did, draft.public_key)
        if draft.document_type == _ENTITY_TYPE:
            return self._save_entity_block(draft, access)
        identifier = self._identifier(draft)
        encoded = _encode(draft)
        _atomic_write_text(self._document_path(identifier), encoded)
        self._audit_event("knowledge.saved", identifier, access.caller_did, draft.digest)
        return SharedKnowledgeReference("shared", identifier, draft.digest)

    def _save_entity_block(self, draft: _Draft, access: _Access) -> SharedKnowledgeReference:
        """Merge this contributor's signed block into the canonical entity.

        Each contributor owns one block file under the entity, so concurrent
        contributors never read-modify-write a shared file (no lost block) and a
        contributor's update or revoke touches only its own bytes. Identical
        re-promotion leaves the store byte-identical.
        """
        identifier = _entity_identifier(draft.title, draft.classification)
        path = self._entity_block_path(identifier, draft.owner_did)
        encoded = _encode(draft)
        if path.exists() and path.read_text(encoding="utf-8") == encoded:
            return SharedKnowledgeReference("shared", identifier, draft.digest)
        _atomic_write_text(path, encoded)
        self._audit_event("knowledge.saved", identifier, access.caller_did, draft.digest)
        return SharedKnowledgeReference("shared", identifier, draft.digest)

    def _authorize_write(self, draft: _Draft, access: _Access) -> None:
        if access.caller_did != draft.owner_did:
            raise PermissionError("shared knowledge owner does not match caller")
        caller = parse_classification(access.clearance, strict=True)
        document = parse_classification(draft.classification, strict=True)
        if not dominates(caller, document):
            raise PermissionError("knowledge classification exceeds caller clearance")
        # The one exception to no-write-down is a promotion the service attested as a
        # declassified-at-source share (the card's own stored label, below the
        # clearance, decided + audited). Every other lower label is still refused.
        if not dominates(document, caller) and not getattr(draft, "declassified_at_source", False):
            raise PermissionError("no-write-down forbids lower-classification shared knowledge")
        # A revoked or demoted identifier is closed for good: no contributor
        # re-promotes into it (a demoted card never re-promotes).
        identifier = (
            _entity_identifier(draft.title, draft.classification)
            if draft.document_type == _ENTITY_TYPE
            else self._identifier(draft)
        )
        if self._revocation_path(identifier).exists():
            raise PermissionError("shared knowledge document was revoked or demoted")

    # -- read ----------------------------------------------------------------------

    def _read(self, reference: str, access: _Access) -> SharedKnowledgeDocument:
        identifier = self._validated_identifier(reference)
        if self._revocation_path(identifier).exists():
            raise FileNotFoundError("shared knowledge document is revoked")
        if self._entity_dir(identifier).is_dir():
            return self._read_entity(identifier, access, self._entity_blocks(identifier))
        return self._read_document(identifier, access, self._document_path(identifier))

    def _read_entity(
        self, identifier: str, access: _Access, paths: Sequence[Path]
    ) -> SharedKnowledgeDocument:
        """Assemble the canonical entity from every verified contributor block.

        Fails closed: one block whose signature, pinned signer, digest, or
        entity/contributor binding does not verify refuses the whole read, so a
        forged value is never served beside genuine ones.
        """
        blocks = [self._verified_block(identifier, path) for path in paths]
        if not blocks:
            raise FileNotFoundError("shared knowledge document is revoked")
        self._require_clearance(access, blocks[0].classification)
        contributions = tuple(
            SharedKnowledgeContribution(block.owner_did, block.digest, block.content)
            for block in blocks
        )
        content = "\n\n".join(
            f"[contributor {block.owner_did}]\n{block.content}" for block in blocks
        )
        tags = tuple(dict.fromkeys(tag for block in blocks for tag in block.tags))
        digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
        return SharedKnowledgeDocument(
            SharedKnowledgeReference("shared", identifier, digest),
            blocks[0].title,
            content,
            blocks[0].classification,
            tags,
            contributions,
            kind=_ENTITY_TYPE,
            contributors=tuple(block.owner_did for block in blocks),
        )

    def _verified_block(self, identifier: str, path: Path) -> _StoredDraft:
        block = self._load_stored(path)
        self._verify_signature(block)
        self._require_pinned_signer(block.owner_did, block.public_key)
        # Bind the block to the entity AND the contributor slot it sits in (live,
        # or retired under ``revoked/``), so a validly signed block copied into
        # another entity or another contributor's slot is refused, not re-attributed.
        if (
            block.document_type != _ENTITY_TYPE
            or _entity_identifier(block.title, block.classification) != identifier
            or not self._is_block_slot(identifier, block.owner_did, block.digest, path)
        ):
            raise PermissionError("shared entity block is not bound to its entity and contributor")
        return block

    def _is_block_slot(self, identifier: str, did: str, digest: str, path: Path) -> bool:
        live = self._entity_block_path(identifier, did)
        return path in (live, self._retired_block_path(identifier, did, digest))

    def _read_document(
        self, identifier: str, access: _Access, path: Path
    ) -> SharedKnowledgeDocument:
        draft = self._load_stored(path)
        self._verify_signature(draft)
        self._require_pinned_signer(draft.owner_did, draft.public_key)
        self._require_clearance(access, draft.classification)
        if self._identifier(draft) != identifier:
            raise PermissionError("shared document is not bound to its identifier")
        return SharedKnowledgeDocument(
            SharedKnowledgeReference("shared", identifier, draft.digest),
            draft.title,
            draft.content,
            draft.classification,
            draft.tags,
            kind=draft.document_type,
            contributors=(draft.owner_did,),
        )

    @staticmethod
    def _load_stored(path: Path) -> _StoredDraft:
        try:
            document = parse(path.read_bytes(), path=path.as_posix())
        except (OSError, OKFValidationError) as error:
            raise FileNotFoundError("shared knowledge document is unavailable") from error
        metadata = document.metadata
        try:
            return _StoredDraft(
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

    @staticmethod
    def _require_clearance(access: _Access, classification: str) -> None:
        if not dominates(
            parse_classification(access.clearance, strict=True),
            parse_classification(classification, strict=True),
        ):
            raise PermissionError("knowledge classification exceeds caller clearance")

    def _search(self, query: str, access: _Access) -> list[SharedKnowledgeHit]:
        if not query.strip():
            return []
        matches: list[SharedKnowledgeHit] = []
        for identifier in self._identifiers():
            try:
                document = self._read(identifier, access)
            except (FileNotFoundError, PermissionError, ValueError):
                continue
            if query.casefold() in f"{document.title}\n{document.content}".casefold():
                matches.append(
                    SharedKnowledgeHit(document.reference, document.title, document.content[:160])
                )
        return matches

    def _list_documents(
        self, access: _Access, include_demoted: bool
    ) -> list[SharedKnowledgeSummary]:
        """Every readable promoted document — classification-filtered, revocation-aware.

        Reuses ``_read`` per document, so a doc the caller may not read (no-read-up)
        or a revoked doc is skipped exactly as ``_search`` skips it. Each surviving
        document carries its kind, every contributor DID and its promotion time.
        ``include_demoted`` adds every operator-demoted document the caller may
        read, from its retired signed bytes, with its demotion.
        """
        summaries: list[SharedKnowledgeSummary] = []
        for identifier in self._identifiers():
            try:
                document = self._read(identifier, access)
            except (FileNotFoundError, PermissionError, ValueError):
                continue
            summaries.append(self._summary(document, None))
        if include_demoted:
            for identifier, (demotion, files) in self._verified_demotions().items():
                try:
                    document = self._read_retired(identifier, files, access)
                except (FileNotFoundError, PermissionError, ValueError):
                    continue
                summaries.append(self._summary(document, demotion))
        return summaries

    def _summary(
        self, document: SharedKnowledgeDocument, demotion: SharedKnowledgeDemotion | None
    ) -> SharedKnowledgeSummary:
        records = self._provenance_records(document)
        return SharedKnowledgeSummary(
            document.reference,
            document.title,
            document.classification,
            document.tags,
            document.contributors[0] if document.contributors else "",
            document.content[:160],
            kind=document.kind,
            contributors=document.contributors,
            promoted_at=min((record.promoted_at for record in records), default=None),
            demotion=demotion,
        )

    def _identifiers(self) -> list[str]:
        """Every stored document and canonical-entity identifier, in stable order."""
        documents = (
            [path.stem for path in self._documents.glob("*.md")]
            if self._documents.exists()
            else []
        )
        entities = (
            [path.name for path in self._entities.iterdir() if path.is_dir()]
            if self._entities.exists()
            else []
        )
        return sorted({*documents, *entities})

    # -- owner revoke --------------------------------------------------------------

    def _revoke(self, reference: str, access: _Access) -> None:
        document = self._read(reference, access)
        if document.contributions:
            self._revoke_entity_block(document, access)
            return
        if access.caller_did not in document.contributors:
            raise PermissionError("only the shared knowledge owner can revoke it")
        identifier = document.reference.identifier
        retired = self._retired_document_path(identifier)
        payload = {
            "identifier": identifier,
            "digest": document.reference.digest,
            "revoked_by": access.caller_did,
            "revoked_at": datetime.now(UTC).isoformat(),
            "revoked_files": [self._relative(retired)],
        }
        _atomic_write_text(self._revocation_path(identifier), json.dumps(payload) + "\n")
        _retire(self._document_path(identifier), retired)
        self._audit_event(
            "knowledge.revoked", identifier, access.caller_did, document.reference.digest
        )

    def _revoke_entity_block(self, document: SharedKnowledgeDocument, access: _Access) -> None:
        """Retire only the caller's provenance block; other contributors stay live.

        The block's signed bytes move under ``revoked/`` (never unlinked). The
        entity directory is left in place so a concurrent contributor's save never
        races a directory removal; with no live block it reads as revoked.
        """
        block = next(
            (b for b in document.contributions if b.contributor_did == access.caller_did),
            None,
        )
        if block is None:
            raise PermissionError("only a contributor can revoke its shared entity block")
        identifier = document.reference.identifier
        _retire(
            self._entity_block_path(identifier, access.caller_did),
            self._retired_block_path(identifier, access.caller_did, block.source_digest),
        )
        self._audit_event("knowledge.revoked", identifier, access.caller_did, block.source_digest)

    # -- operator demote (alpha-2 item 16) ----------------------------------------------

    def _demote(
        self, reference: str, access: _Access, operator: RecordSigner, reason: str
    ) -> SharedKnowledgeDemotion:
        """Demote one live document under the operator's signed tombstone.

        Refused unless ``operator`` holds the anchored operator key, and unless the
        document reads (signatures, pins, clearance) at ``access``. Idempotent: a
        document this operator already demoted returns that demotion. The
        tombstone is written before the bytes move, so a crash between the two
        leaves the document hidden (fail closed), never live without a decision.
        """
        operator_did = self._require_operator(operator)
        identifier = self._validated_identifier(reference)
        existing = self._verified_demotions().get(identifier)
        if existing is not None:
            return existing[0]
        document = self._read(identifier, access)
        moves = self._live_files(document)
        payload = {
            "action": "demote",
            "identifier": identifier,
            "digest": document.reference.digest,
            "revoked_by": operator_did,
            "reason": _clean_reason(reason),
            "revoked_at": datetime.now(UTC).isoformat(),
            "revoked_files": sorted(self._relative(target) for _, target in moves),
        }
        _atomic_write_text(self._revocation_path(identifier), sign_record(payload, operator))
        for source, target in moves:
            _retire(source, target)
        self._audit_event("knowledge.demoted", identifier, operator_did, document.reference.digest)
        return _demotion(payload)

    def _require_operator(self, operator: RecordSigner) -> str:
        anchor = self._operator_public_key
        if anchor is None or bytes(operator.public_key) != bytes(anchor):
            raise PermissionError("only the deployment operator can demote shared knowledge")
        return _operator_did(anchor)

    def _live_files(self, document: SharedKnowledgeDocument) -> list[tuple[Path, Path]]:
        """Each live file of ``document`` and where its retired copy goes."""
        identifier = document.reference.identifier
        if not document.contributions:
            return [(self._document_path(identifier), self._retired_document_path(identifier))]
        return [
            (
                self._entity_block_path(identifier, block.contributor_did),
                self._retired_block_path(identifier, block.contributor_did, block.source_digest),
            )
            for block in document.contributions
        ]

    def _demotions(self) -> dict[str, SharedKnowledgeDemotion]:
        return {key: value[0] for key, value in self._verified_demotions().items()}

    def _verified_demotions(self) -> dict[str, tuple[SharedKnowledgeDemotion, list[str]]]:
        """Every tombstone the anchored operator signed, with the files it retired.

        An owner's unsigned revoke, a tombstone signed by any other key, and one
        copied onto another identifier (the signature binds the identifier) are
        not demotions. With no operator anchor there are none.
        """
        anchor = self._operator_public_key
        if anchor is None or not self._revocations.exists():
            return {}
        found: dict[str, tuple[SharedKnowledgeDemotion, list[str]]] = {}
        for path in sorted(self._revocations.glob("*.json")):
            verified = verified_payload(
                path.read_text(encoding="utf-8"), signer_field="revoked_by"
            )
            if verified is None or verified[1] != bytes(anchor):
                continue
            payload = verified[0]
            files = payload.get("revoked_files")
            if payload.get("identifier") != path.stem or not isinstance(files, list):
                continue
            found[path.stem] = (_demotion(payload), [str(name) for name in files])
        return found

    def _read_retired(
        self, identifier: str, files: list[str], access: _Access
    ) -> SharedKnowledgeDocument:
        """A demoted document rebuilt from the retired bytes its tombstone names."""
        paths = [self._resolved_retired(identifier, name) for name in files]
        if self._entity_dir(identifier).is_dir():
            return self._read_entity(identifier, access, paths)
        if len(paths) != 1:
            raise ValueError("a demoted document retires exactly one file")
        return self._read_document(identifier, access, paths[0])

    def _resolved_retired(self, identifier: str, name: str) -> Path:
        """Jail a tombstone-named path to this identifier's retired locations."""
        path = (self._root / name).resolve()
        allowed = (self._documents / _REVOKED_DIR, self._entity_dir(identifier) / _REVOKED_DIR)
        if path.parent not in allowed:
            raise PermissionError("tombstone names a file outside the retired set")
        return path

    # -- provenance (alpha-2 item 16) ---------------------------------------------------

    def _record_provenance(
        self, reference: SharedKnowledgeReference, signer: RecordSigner, fields: dict[str, Any]
    ) -> None:
        """Write the contributor-signed provenance of one saved set of bytes, once.

        The contributor must be the pinned signer of the bytes it describes. An
        existing record for the same bytes is kept (first promotion wins), so an
        identical re-promotion leaves the store unchanged.
        """
        contributor = str(fields["contributor_did"])
        public_key = base64.b64encode(signer.public_key).decode("ascii")
        if not did_matches_pubkey(contributor, signer.public_key):
            raise PermissionError("provenance signer does not match the contributor DID")
        self._require_pinned_signer(contributor, public_key)
        path = self._provenance_path(reference.identifier, contributor, reference.digest)
        if path.exists():
            return
        payload = {**fields, "identifier": reference.identifier, "digest": reference.digest}
        _atomic_write_text(path, sign_record(payload, signer))

    def _provenance_of(
        self, reference: str, access: _Access
    ) -> tuple[SharedKnowledgeProvenance, ...]:
        return self._provenance_records(self._read(reference, access))

    def _provenance_records(
        self, document: SharedKnowledgeDocument
    ) -> tuple[SharedKnowledgeProvenance, ...]:
        """The verified provenance of each contributor's live bytes; forged ones are dropped."""
        identifier = document.reference.identifier
        pairs = (
            [(block.contributor_did, block.source_digest) for block in document.contributions]
            if document.contributions
            else [(did, document.reference.digest) for did in document.contributors]
        )
        records: list[SharedKnowledgeProvenance] = []
        for did, digest in pairs:
            record = self._load_provenance(identifier, did, digest)
            if record is not None:
                records.append(record)
        return tuple(records)

    def _load_provenance(
        self, identifier: str, did: str, digest: str
    ) -> SharedKnowledgeProvenance | None:
        path = self._provenance_path(identifier, did, digest)
        if not path.is_file():
            return None
        verified = verified_payload(
            path.read_text(encoding="utf-8"), signer_field="contributor_did"
        )
        if verified is None:
            return None
        payload, public_key = verified
        pinned = self._load_trusted_signers().get(did)
        if (
            pinned != base64.b64encode(public_key).decode("ascii")
            or payload.get("contributor_did") != did
            or payload.get("identifier") != identifier
            or payload.get("digest") != digest
        ):
            return None
        return _provenance(payload)

    # -- signatures and trust ------------------------------------------------------

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

    # -- paths ---------------------------------------------------------------------

    def _identifier(self, draft: _Draft | _StoredDraft) -> str:
        basis = f"{draft.owner_did}\0{draft.title}\0{draft.digest}".encode()
        return hashlib.sha256(basis).hexdigest()[:16]

    def _document_path(self, identifier: str) -> Path:
        return self._documents / f"{self._validated_identifier(identifier)}.md"

    def _retired_document_path(self, identifier: str) -> Path:
        return self._documents / _REVOKED_DIR / f"{self._validated_identifier(identifier)}.md"

    def _entity_dir(self, identifier: str) -> Path:
        return self._entities / self._validated_identifier(identifier)

    def _entity_blocks(self, identifier: str) -> list[Path]:
        # Non-recursive: retired blocks under ``revoked/`` are never live.
        return sorted(self._entity_dir(identifier).glob("*.md"))

    def _entity_block_path(self, identifier: str, contributor_did: str) -> Path:
        return self._entity_dir(identifier) / f"{_slot(contributor_did)}.md"

    def _retired_block_path(self, identifier: str, contributor_did: str, digest: str) -> Path:
        name = f"{_slot(contributor_did)}.{_digest_tag(digest)}.md"
        return self._entity_dir(identifier) / _REVOKED_DIR / name

    def _provenance_path(self, identifier: str, contributor_did: str, digest: str) -> Path:
        name = f"{_slot(contributor_did)}.{_digest_tag(digest)}.json"
        return self._provenance / self._validated_identifier(identifier) / name

    def _revocation_path(self, identifier: str) -> Path:
        return self._revocations / f"{self._validated_identifier(identifier)}.json"

    def _relative(self, path: Path) -> str:
        return path.relative_to(self._root).as_posix()

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


def _entity_identifier(title: str, classification: str) -> str:
    """One shared identifier per real-world entity (title slug) per classification label.

    The label is part of the key so contributors at different clearances never
    share (or learn of) one canonical entity — each label has its own.
    """
    slug = " ".join(title.split()).casefold()
    basis = f"{_ENTITY_TYPE}\0{slug}\0{classification}".encode()
    return hashlib.sha256(basis).hexdigest()[:16]


def _slot(contributor_did: str) -> str:
    return hashlib.sha256(contributor_did.encode()).hexdigest()[:16]


def _digest_tag(digest: str) -> str:
    """A filename-safe tag of a ``sha256:<hex>`` digest."""
    return hashlib.sha256(digest.encode()).hexdigest()[:16]


def _operator_did(public_key: bytes) -> str:
    """The operator DID (the same derivation as ``arctrust`` approval authorities)."""
    return did_from_public_key(public_key, org="operator", agent_type="approver")


def _clean_reason(reason: str) -> str:
    cleaned = " ".join(str(reason).split())
    if not cleaned or len(cleaned) > _MAX_REASON_CHARS:
        raise ValueError(f"a demote reason is 1..{_MAX_REASON_CHARS} characters")
    return cleaned


def _demotion(payload: dict[str, Any]) -> SharedKnowledgeDemotion:
    return SharedKnowledgeDemotion(
        identifier=str(payload["identifier"]),
        digest=str(payload["digest"]),
        demoted_by=str(payload["revoked_by"]),
        reason=str(payload.get("reason", "")),
        demoted_at=str(payload["revoked_at"]),
    )


def _provenance(payload: dict[str, Any]) -> SharedKnowledgeProvenance:
    confidence = payload.get("confidence")
    version = payload.get("classifier_version")
    decided_by = payload.get("decided_by")
    return SharedKnowledgeProvenance(
        identifier=str(payload["identifier"]),
        contributor_did=str(payload["contributor_did"]),
        source_ref=str(payload.get("source_ref", "")),
        digest=str(payload["digest"]),
        kind=str(payload.get("kind", "")),
        decision=str(payload.get("decision", "")),
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        classifier_version=str(version) if version is not None else None,
        decided_by=str(decided_by) if decided_by is not None else None,
        promoted_at=str(payload.get("promoted_at", "")),
    )


def _encode(draft: _Draft) -> str:
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
    return encoded + "\n"


def _retire(source: Path, target: Path) -> None:
    """Move signed bytes out of the live set; never overwrite a retired copy (no erasure)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != source.read_bytes():
            raise FileExistsError("a different retired copy already exists")
        source.unlink()  # byte-identical copy already retired; nothing is lost
        return
    os.replace(source, target)


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
    "SharedKnowledgeContribution",
    "SharedKnowledgeDemotion",
    "SharedKnowledgeDocument",
    "SharedKnowledgeHit",
    "SharedKnowledgeProvenance",
    "SharedKnowledgeReference",
    "SharedKnowledgeSummary",
]
