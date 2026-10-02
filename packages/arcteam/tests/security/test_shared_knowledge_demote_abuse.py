"""Alpha-2 item 16 abuse battery — operator demote, tombstones and provenance.

Every case drives the real ``FleetSharedKnowledgeService`` over a tmp collection
and must fail closed:

  1. forged demote — a tombstone signed by any key but the anchored operator is
     not a demotion (no agent turns it into a sticky decision);
  2. replayed demote — a valid tombstone copied onto another document is refused
     (the signature binds the identifier);
  3. tombstone deletion — deleting the tombstone never resurrects the document
     (the signed bytes were retired, not left live);
  4. retired-path traversal — a tombstone naming a file outside the retired set
     is never read;
  5. re-promotion after demote — no contributor re-promotes into a demoted
     identifier;
  6. TOFU — a provenance record signed by an unpinned key for a pinned
     contributor is dropped; a second operator key cannot demote.

Registered in ``tests/run_adversarial_tests.py`` under the item 16 scenario.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity
from arctrust.identity import did_from_public_key
from arctrust.signer import InProcessSigner

from arcteam.shared_knowledge import FleetSharedKnowledgeService
from arcteam.shared_knowledge.signed_records import sign_record

_OPERATOR = InProcessSigner(b"\x0a" * 32)
_CLEARANCE = "UNCLASSIFIED"


class _Access:
    def __init__(self, identity: AgentIdentity) -> None:
        self.caller_did = identity.did
        self.clearance = _CLEARANCE


class _Draft:
    def __init__(self, title: str, content: str) -> None:
        self.title = title
        self.content = content
        self.classification = _CLEARANCE
        self.tags = ("insight",)
        self.document_type = "insight"


def _service(tmp_path: Path) -> FleetSharedKnowledgeService:
    return FleetSharedKnowledgeService.for_arc_team(
        tmp_path, operator_public_key=_OPERATOR.public_key
    )


async def _share(
    service: FleetSharedKnowledgeService, tmp_path: Path, owner: AgentIdentity, title: str
) -> Any:
    personal = PersonalKnowledgeAdapter(tmp_path / owner.did.rsplit("/", 1)[-1], owner.did)
    ref = await personal.save(_Draft(title, f"{title} closes on day three."), _Access(owner))
    return await service.promote(personal, ref.identifier, _Access(owner), owner)


def _tombstone(service: FleetSharedKnowledgeService, identifier: str) -> Path:
    return service.backend.root / "revocations" / f"{identifier}.json"


async def test_tombstone_signed_by_a_non_operator_key_is_not_a_demotion(tmp_path: Path) -> None:
    service = _service(tmp_path)
    owner = AgentIdentity.generate("test", "owner")
    shared = await _share(service, tmp_path, owner, "Close timing")
    forger = InProcessSigner(b"\x0c" * 32)
    payload = {
        "action": "demote",
        "identifier": shared.identifier,
        "digest": shared.digest,
        "revoked_by": did_from_public_key(
            forger.public_key, org="operator", agent_type="approver"
        ),
        "reason": "forged",
        "revoked_at": "2026-10-02T00:00:00+00:00",
        "revoked_files": [],
    }
    _tombstone(service, shared.identifier).parent.mkdir(parents=True, exist_ok=True)
    _tombstone(service, shared.identifier).write_text(sign_record(payload, forger))

    assert await service.demotions() == {}


async def test_tombstone_claiming_the_operator_did_with_another_key_is_ignored(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    shared = await _share(service, tmp_path, AgentIdentity.generate("test", "o"), "Close")
    forger = InProcessSigner(b"\x0d" * 32)
    payload = {
        "identifier": shared.identifier,
        "digest": shared.digest,
        "revoked_by": did_from_public_key(
            _OPERATOR.public_key, org="operator", agent_type="approver"
        ),
        "reason": "impersonated",
        "revoked_at": "2026-10-02T00:00:00+00:00",
        "revoked_files": [],
    }
    _tombstone(service, shared.identifier).parent.mkdir(parents=True, exist_ok=True)
    _tombstone(service, shared.identifier).write_text(sign_record(payload, forger))

    assert await service.demotions() == {}


async def test_replayed_demote_onto_another_document_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    owner = AgentIdentity.generate("test", "owner")
    demoted = await _share(service, tmp_path, owner, "Close timing")
    other = await _share(service, tmp_path, owner, "Payroll timing")
    await service.demote(
        demoted.identifier, operator_signer=_OPERATOR, reason="stale", clearance=_CLEARANCE
    )

    _tombstone(service, other.identifier).write_bytes(
        _tombstone(service, demoted.identifier).read_bytes()
    )

    assert set(await service.demotions()) == {demoted.identifier}


async def test_deleting_the_tombstone_never_resurrects_the_document(tmp_path: Path) -> None:
    service = _service(tmp_path)
    owner = AgentIdentity.generate("test", "owner")
    shared = await _share(service, tmp_path, owner, "Close timing")
    await service.demote(
        shared.identifier, operator_signer=_OPERATOR, reason="stale", clearance=_CLEARANCE
    )

    _tombstone(service, shared.identifier).unlink()

    with pytest.raises(FileNotFoundError):
        await service.read(shared.identifier, _Access(owner))
    assert await service.list_documents(_Access(owner)) == []
    assert await service.search("close", _Access(owner)) == []


async def test_tombstone_naming_a_file_outside_the_retired_set_is_never_read(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    owner = AgentIdentity.generate("test", "owner")
    shared = await _share(service, tmp_path, owner, "Close timing")
    secret = await _share(service, tmp_path, owner, "Board secret")
    payload = {
        "action": "demote",
        "identifier": shared.identifier,
        "digest": shared.digest,
        "revoked_by": did_from_public_key(
            _OPERATOR.public_key, org="operator", agent_type="approver"
        ),
        "reason": "traversal",
        "revoked_at": "2026-10-02T00:00:00+00:00",
        "revoked_files": [f"documents/{secret.identifier}.md"],
    }
    _tombstone(service, shared.identifier).parent.mkdir(parents=True, exist_ok=True)
    _tombstone(service, shared.identifier).write_text(sign_record(payload, _OPERATOR))

    listed = await service.list_documents(_Access(owner), include_demoted=True)

    assert [(s.title, s.demotion is None) for s in listed] == [("Board secret", True)]


async def test_no_contributor_re_promotes_into_a_demoted_document(tmp_path: Path) -> None:
    service = _service(tmp_path)
    owner = AgentIdentity.generate("test", "owner")
    shared = await _share(service, tmp_path, owner, "Close timing")
    await service.demote(
        shared.identifier, operator_signer=_OPERATOR, reason="stale", clearance=_CLEARANCE
    )

    with pytest.raises(PermissionError):
        await _share(service, tmp_path, owner, "Close timing")

    assert await service.list_documents(_Access(owner)) == []


async def test_a_second_operator_key_cannot_demote(tmp_path: Path) -> None:
    service = _service(tmp_path)
    owner = AgentIdentity.generate("test", "owner")
    shared = await _share(service, tmp_path, owner, "Close timing")

    with pytest.raises(PermissionError):
        await service.demote(
            shared.identifier,
            operator_signer=InProcessSigner(b"\x0e" * 32),
            reason="rotate",
            clearance=_CLEARANCE,
        )

    assert len(await service.list_documents(_Access(owner))) == 1


async def test_provenance_signed_by_an_unpinned_key_is_dropped(tmp_path: Path) -> None:
    service = _service(tmp_path)
    owner = AgentIdentity.generate("test", "owner")
    shared = await _share(service, tmp_path, owner, "Close timing")
    [record_path] = (service.backend.root / "provenance" / shared.identifier).glob("*.json")
    record = json.loads(record_path.read_text())
    impostor = AgentIdentity.generate("test", "impostor")
    forged = {
        key: record[key] for key in record if key not in ("public_key", "signature", "algorithm")
    }
    forged.update(
        decision="operator_promote", decided_by=impostor.did, contributor_did=impostor.did
    )
    record_path.write_text(sign_record(forged, impostor))

    assert await service.provenance(shared.identifier, _Access(owner)) == ()
    [summary] = await service.list_documents(_Access(owner))
    assert summary.promoted_at is None
