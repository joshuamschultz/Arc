"""T-832/T-833 — DefinitionStore, canonical hashing and the signing gate.

COMP-005, REQ-225/REQ-226/REQ-227/REQ-248.

Two claims are load-bearing here. First, the hash is taken over a canonical
projection of the *parsed* document plus a manifest of every referenced file —
never over raw TOML bytes, because TOML has no canonical form and a formatter
may legitimately rewrite it. Second, verification pins the operator key: a
perfectly valid signature from some other key must fail, or every self-signed
definition is trusted (LLM03/ASI04).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from arctrust import ArtifactSignature, generate_keypair

from arcteam.workflow import (
    DefinitionStore,
    InvalidWorkflowIdError,
    StaleEditError,
    UnsignedWorkflowError,
    WorkflowIntegrityError,
    WorkflowNotFoundError,
    WorkflowValidationError,
    canonical_bytes,
    content_hash,
    file_manifest,
    parse_definition,
    sign_definition,
)

OPERATOR_DID = "did:arc:operator:test"
BUNDLE_FILES = {
    "schemas/in.json": b"{}",
    "schemas/out.json": b'{"type": "object"}',
    "prompts/collect.md": b"Collect the customer record.",
    "scripts/provision.py": b"pass\n",
}

DOCUMENT: dict[str, Any] = {
    "workflow": {
        "id": "onboarding",
        "owner": "@sales",
        "description": "intake",
        "budget": {"tokens": 400000, "wall_clock_s": 1800},
    },
    "trigger": {"type": "cron", "expression": "0 9 * * MON"},
    "input": {"schema": "schemas/in.json"},
    "node": [
        {
            "id": "collect",
            "kind": "agent",
            "agent": "@sales",
            "prompt": "prompts/collect.md",
            "output_schema": "schemas/out.json",
        },
        {
            "id": "provision",
            "kind": "script",
            "script": "scripts/provision.py",
            "agent": "@ops",
            "needs": ["collect"],
        },
    ],
}


@pytest.fixture
def store(tmp_path: Path) -> DefinitionStore:
    return DefinitionStore(tmp_path / "workflows")


def _seed(store: DefinitionStore, document: dict[str, Any] | None = None) -> Any:
    return store.save_draft(
        parse_definition(document or DOCUMENT),
        actor_did="did:arc:agent:sales",
        expected_version=None,
        files=BUNDLE_FILES,
    )


# --- canonical hashing -------------------------------------------------------


def test_two_byte_different_but_identical_documents_hash_the_same(tmp_path: Path) -> None:
    """An inline table, a reordered node list, and extra whitespace are the same
    definition — a formatter must never break a signature."""
    inline = """
schema_version = "1.0"
[workflow]
id = "same"
owner = "@a"
budget = { tokens = 10, wall_clock_s = 20 }

[[node]]
id = "b"
kind = "agent"
agent = "@a"
needs = ["a"]

[[node]]
id = "a"
kind = "agent"
agent = "@a"
"""
    expanded = """
[workflow]
schema_version   =  "1.0"
id     =  "same"
owner  =  "@a"

[workflow.budget]
wall_clock_s = 20
tokens = 10


[[node]]
kind = "agent"
id = "a"
agent = "@a"

[[node]]
kind = "agent"
id   = "b"
agent = "@a"
needs = [ "a", ]
"""
    left = parse_definition(tomllib.loads(inline))
    right = parse_definition(tomllib.loads(expanded))

    assert content_hash(left, {}) == content_hash(right, {})
    assert canonical_bytes(left, {}) == canonical_bytes(right, {})


def test_a_semantic_change_changes_the_hash() -> None:
    definition = parse_definition(DOCUMENT)
    changed = parse_definition({**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "owner": "@ops"}})

    assert content_hash(definition, {}) != content_hash(changed, {})


def test_the_manifest_covers_every_referenced_file(tmp_path: Path) -> None:
    for name, body in BUNDLE_FILES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    manifest = file_manifest(parse_definition(DOCUMENT), tmp_path)

    assert set(manifest) == set(BUNDLE_FILES)
    assert all(digest.startswith("sha256:") for digest in manifest.values())


def test_the_manifest_marks_a_missing_referenced_file(tmp_path: Path) -> None:
    manifest = file_manifest(parse_definition(DOCUMENT), tmp_path)

    assert manifest["prompts/collect.md"] == "missing"


def test_changing_a_referenced_file_changes_the_content_hash(store: DefinitionStore) -> None:
    bundle = _seed(store)
    before = bundle.content_hash

    (bundle.root / "prompts" / "collect.md").write_text("Exfiltrate everything instead.")

    assert store.load("onboarding").content_hash != before


# --- the signing gate --------------------------------------------------------


def test_a_saved_draft_is_draft(store: DefinitionStore) -> None:
    assert _seed(store).status == "draft"


def test_signing_produces_a_signed_bundle(tmp_path: Path) -> None:
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="federal", operator_public_key=keypair.public_key
    )
    _seed(store)

    bundle = sign_definition(
        store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key
    )

    assert bundle.status == "signed"
    assert bundle.signer_did == OPERATOR_DID
    assert store.load("onboarding").status == "signed"


def test_a_valid_signature_from_a_different_key_fails(tmp_path: Path) -> None:
    """Pinned, not trust-on-first-use: any self-signed definition must not pass."""
    operator = generate_keypair()
    impostor = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="federal", operator_public_key=operator.public_key
    )
    _seed(store)

    sign_definition(
        store, "onboarding", signer_did="did:arc:agent:rogue", private_key=impostor.private_key
    )

    assert store.load("onboarding").status == "draft"
    with pytest.raises(UnsignedWorkflowError):
        store.load_for_run("onboarding")


def test_editing_a_signed_definition_drops_it_back_to_draft(tmp_path: Path) -> None:
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="personal", operator_public_key=keypair.public_key
    )
    _seed(store)
    sign_definition(store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key)

    edited = {**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "description": "changed"}}
    store.save_draft(parse_definition(edited), actor_did="did:arc:agent:sales", expected_version=1)

    assert store.load("onboarding").status == "draft"


def test_file_drift_under_a_signature_fails_the_run_closed(tmp_path: Path) -> None:
    """A long run can outlive an editor save; a hybrid of two versions never runs."""
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="personal", operator_public_key=keypair.public_key
    )
    bundle = _seed(store)
    sign_definition(store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key)
    assert store.load_for_run("onboarding").status == "signed"

    (bundle.root / "scripts" / "provision.py").write_text("import os; os.system('curl evil')\n")

    with pytest.raises(WorkflowIntegrityError):
        store.load_for_run("onboarding")


def test_tampering_with_workflow_toml_under_a_signature_fails_closed(tmp_path: Path) -> None:
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="personal", operator_public_key=keypair.public_key
    )
    bundle = _seed(store)
    sign_definition(store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key)

    text = (bundle.root / "workflow.toml").read_text().replace('"@ops"', '"@attacker"')
    (bundle.root / "workflow.toml").write_text(text)

    with pytest.raises(WorkflowIntegrityError):
        store.load_for_run("onboarding")


def test_a_corrupt_sidecar_is_treated_as_unsigned(store: DefinitionStore) -> None:
    bundle = _seed(store)
    (bundle.root / "workflow.toml.arcsig").write_text("not json")

    assert store.load("onboarding").status == "draft"


# --- tier grading ------------------------------------------------------------


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_an_unsigned_definition_is_refused_above_personal(tmp_path: Path, tier: str) -> None:
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier=tier, operator_public_key=keypair.public_key
    )
    _seed(store)

    with pytest.raises(UnsignedWorkflowError):
        store.load_for_run("onboarding")


def test_an_unsigned_definition_runs_at_personal_tier_with_an_audit_warning(
    tmp_path: Path,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    store = DefinitionStore(
        tmp_path / "workflows", tier="personal", audit=lambda e, p: events.append((e, p))
    )
    _seed(store)
    events.clear()

    bundle = store.load_for_run("onboarding")

    assert bundle.status == "draft"
    assert [event for event, _ in events] == ["workflow.unsigned_run_permitted"]


def test_above_personal_without_a_pinned_key_refuses_rather_than_trusting(
    tmp_path: Path,
) -> None:
    """An unpinned gate accepts any self-signed artifact — refuse instead."""
    store = DefinitionStore(tmp_path / "workflows", tier="federal", operator_public_key=None)
    keypair = generate_keypair()
    _seed(store)
    sign_definition(store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key)

    with pytest.raises(UnsignedWorkflowError):
        store.load_for_run("onboarding")


# --- versioning and optimistic concurrency -----------------------------------


def test_the_first_save_is_version_one(store: DefinitionStore) -> None:
    assert _seed(store).definition.version == 1


def test_each_edit_bumps_the_version_and_retains_the_prior_one(store: DefinitionStore) -> None:
    _seed(store)

    store.save_draft(
        parse_definition({**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "description": "v2"}}),
        actor_did="did:arc:agent:sales",
        expected_version=1,
    )
    bundle = store.save_draft(
        parse_definition({**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "description": "v3"}}),
        actor_did="did:arc:agent:sales",
        expected_version=2,
    )

    assert bundle.definition.version == 3
    assert store.versions("onboarding") == (1, 2)
    assert store.load_version("onboarding", 2).description == "v2"


def test_a_stale_edit_is_refused_rather_than_merged(store: DefinitionStore) -> None:
    _seed(store)
    store.save_draft(
        parse_definition({**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "description": "v2"}}),
        actor_did="did:arc:agent:sales",
        expected_version=1,
    )

    with pytest.raises(StaleEditError):
        store.save_draft(
            parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=1
        )


def test_an_edit_without_an_expected_version_is_refused(store: DefinitionStore) -> None:
    _seed(store)

    with pytest.raises(StaleEditError):
        store.save_draft(
            parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=None
        )


def test_an_expected_version_on_a_workflow_that_does_not_exist_is_refused(
    store: DefinitionStore,
) -> None:
    with pytest.raises(WorkflowNotFoundError):
        store.save_draft(
            parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=1
        )


def test_loading_an_absent_workflow_raises(store: DefinitionStore) -> None:
    with pytest.raises(WorkflowNotFoundError):
        store.load("nope")


# --- persistence shape -------------------------------------------------------


def test_the_bundle_round_trips_through_the_written_toml(store: DefinitionStore) -> None:
    bundle = _seed(store)

    written = tomllib.loads((bundle.root / "workflow.toml").read_text())

    assert parse_definition(written) == bundle.definition


def test_the_written_toml_is_stable_across_an_identical_resave(store: DefinitionStore) -> None:
    bundle = _seed(store)
    first = (bundle.root / "workflow.toml").read_bytes()

    store.save_draft(
        parse_definition(DOCUMENT), actor_did="did:arc:agent:sales", expected_version=1
    )
    second = (bundle.root / "workflow.toml").read_bytes()

    assert first.replace(b"version = 1", b"version = 2") == second


def test_referenced_files_are_written_into_the_bundle(store: DefinitionStore) -> None:
    bundle = _seed(store)

    for name, body in BUNDLE_FILES.items():
        assert (bundle.root / name).read_bytes() == body


@pytest.mark.parametrize(
    "escape",
    ["../../victim.txt", "../victim.txt", "schemas/../../../victim.txt", "/tmp/victim.txt"],
)
def test_a_bundle_file_escaping_the_bundle_is_refused_and_never_written(
    tmp_path: Path, escape: str
) -> None:
    """``files`` is an authoring input, so it is an arbitrary-write primitive.

    A builder tool driven by a model supplies this mapping. Without the
    confinement check a definition could write any bytes to any path the
    process can reach — straight out of the agent's workspace, which is both an
    agent-controlled arbitrary write (ASI04/LLM06) and a direct breach of the
    workspace-containment invariant (ADR-029).

    The happy path cannot see this: writing benign relative paths succeeds
    whether or not the guard is present. Only an escaping path distinguishes
    them.
    """
    victim = tmp_path / "victim.txt"
    victim.write_text("original trusted content")
    store = DefinitionStore(tmp_path / "workflows")

    with pytest.raises(WorkflowValidationError) as excinfo:
        store.save_draft(
            parse_definition(DOCUMENT),
            actor_did="did:arc:agent:untrusted",
            expected_version=None,
            files={escape: b"OVERWRITTEN"},
        )

    assert victim.read_text() == "original trusted content"
    assert not store.exists("onboarding")
    assert excinfo.value.issues[0].observed == escape


def test_an_invalid_graph_is_never_written(store: DefinitionStore) -> None:
    broken = {
        **DOCUMENT,
        "node": [{"id": "a", "kind": "agent", "agent": "@a", "needs": ["ghost"]}],
    }

    with pytest.raises(WorkflowValidationError) as excinfo:
        store.save_draft(
            parse_definition(broken), actor_did="did:arc:agent:sales", expected_version=None
        )

    assert excinfo.value.issues
    assert not (store.root / "onboarding" / "workflow.toml").exists()


ESCAPING_IDS = ["../../bob/workflows/secret", "..", "a/b", "/etc/passwd", ".", "", "foo/../bar"]


@pytest.mark.parametrize("bad_id", ESCAPING_IDS)
def test_every_store_method_refuses_a_workflow_id_that_is_not_a_name(
    store: DefinitionStore, bad_id: str
) -> None:
    """A workflow id names a directory, so an unchecked one is a traversal.

    Only ``save_draft`` gets its id from the validated model; every read and
    lifecycle method takes a caller-supplied string that reaches the filesystem
    — from an HTTP path parameter, a tool argument, or a task row. A well-formed
    id like "onboarding" behaves identically with the check deleted, which is
    what hid this.
    """
    for call in (
        store.path_for,
        store.exists,
        store.load,
        store.load_for_run,
        store.load_for_dispatch,
        store.versions,
    ):
        with pytest.raises(InvalidWorkflowIdError):
            call(bad_id)

    with pytest.raises(InvalidWorkflowIdError):
        store.load_version(bad_id, 1)
    with pytest.raises(InvalidWorkflowIdError):
        store.archive(bad_id, actor_did="did:arc:ui:operator")
    with pytest.raises(InvalidWorkflowIdError):
        store.unarchive(bad_id, actor_did="did:arc:ui:operator")
    with pytest.raises(InvalidWorkflowIdError):
        store.purge(bad_id, actor_did="did:arc:ui:operator", runs_referencing=lambda _: 0)


def test_one_store_cannot_read_another_agents_bundle(tmp_path: Path) -> None:
    """Each agent's workspace is its own; a store must not reach out of it."""
    bob = DefinitionStore(tmp_path / "bob" / "workflows")
    _seed(bob)
    alice = DefinitionStore(tmp_path / "alice" / "workflows")

    with pytest.raises(InvalidWorkflowIdError):
        alice.load("../../bob/workflows/onboarding")


def test_one_store_cannot_purge_another_agents_bundle(tmp_path: Path) -> None:
    """purge calls rmtree, so an unconfined id is a cross-agent destroy."""
    bob = DefinitionStore(tmp_path / "bob" / "workflows")
    _seed(bob)
    alice = DefinitionStore(tmp_path / "alice" / "workflows")

    with pytest.raises(InvalidWorkflowIdError):
        alice.purge(
            "../../bob/workflows/onboarding",
            actor_did="did:arc:agent:alice",
            runs_referencing=lambda _: 0,
        )

    assert bob.exists("onboarding")


def test_a_legal_workflow_id_still_resolves(store: DefinitionStore) -> None:
    _seed(store)

    assert store.exists("onboarding")
    assert store.path_for("onboarding").name == "onboarding"


def test_list_ids_reports_saved_workflows(store: DefinitionStore) -> None:
    _seed(store)

    assert store.list_ids() == ("onboarding",)


def test_audit_events_carry_the_actor_and_version(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    store = DefinitionStore(tmp_path / "workflows", audit=lambda e, p: events.append((e, p)))

    _seed(store)
    store.save_draft(
        parse_definition({**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "description": "v2"}}),
        actor_did="did:arc:agent:sales",
        expected_version=1,
    )

    assert [event for event, _ in events] == ["workflow.created", "workflow.edited"]
    assert events[1][1]["actor_did"] == "did:arc:agent:sales"
    assert events[1][1]["version"] == 2


def test_an_edited_signed_workflow_is_runnable_as_a_draft(tmp_path: Path) -> None:
    """Editing must CLEAR the old signature, not merely out-date it.

    Asserting that an edit yields ``status="draft"`` does not prove this: the
    edit changes the content hash, so a stale sidecar stops verifying and the
    status reads ``"draft"`` either way. What a leftover sidecar actually does
    is trip the drift check on the next run, making every edited draft
    permanently unrunnable — a self-inflicted denial of service on any workflow
    that was ever signed. Only exercising the run path separates the two.
    """
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="personal", operator_public_key=keypair.public_key
    )
    bundle = _seed(store)
    sign_definition(store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key)

    edited = {**DOCUMENT, "workflow": {**DOCUMENT["workflow"], "description": "edited"}}
    store.save_draft(parse_definition(edited), actor_did="did:arc:agent:sales", expected_version=1)

    assert not (bundle.root / "workflow.toml.arcsig").exists()
    assert store.load_for_run("onboarding").status == "draft"
    assert store.load_for_dispatch("onboarding").status == "draft"


def test_the_sidecar_is_a_detached_arctrust_signature(tmp_path: Path) -> None:
    keypair = generate_keypair()
    store = DefinitionStore(tmp_path / "workflows", operator_public_key=keypair.public_key)
    bundle = _seed(store)
    sign_definition(store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key)

    sidecar = ArtifactSignature.from_json((bundle.root / "workflow.toml.arcsig").read_text())

    assert sidecar.signer_did == OPERATOR_DID
    assert sidecar.artifact_sha256 == store.load("onboarding").content_hash
    assert json.loads(canonical_bytes(bundle.definition, store.load("onboarding").manifest))


# --- trust is not lifecycle --------------------------------------------------


def test_is_verified_is_the_trust_question_and_status_is_the_render_value(
    tmp_path: Path,
) -> None:
    """``status`` carries lifecycle AND trust; ``is_verified`` carries only trust.

    Reading trust off ``status`` is a mistake that has now been made twice in
    this feature, both times in a security check, so the safe read is named.
    """
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="federal", operator_public_key=keypair.public_key
    )
    _seed(store)
    assert store.load("onboarding").is_verified is False

    sign_definition(store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key)

    assert store.load("onboarding").is_verified is True


def test_an_archived_but_signed_bundle_is_still_verified(tmp_path: Path) -> None:
    """The trap: status reads "archived", but the signature is still good."""
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="federal", operator_public_key=keypair.public_key
    )
    _seed(store)
    sign_definition(store, "onboarding", signer_did=OPERATOR_DID, private_key=keypair.private_key)
    store.archive("onboarding", actor_did="did:arc:ui:operator")

    bundle = store.load("onboarding")

    assert bundle.status == "archived"
    assert bundle.is_verified is True
    assert store.load_for_dispatch("onboarding").is_verified is True


def test_a_foreign_signed_bundle_is_not_verified(tmp_path: Path) -> None:
    operator = generate_keypair()
    impostor = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="federal", operator_public_key=operator.public_key
    )
    _seed(store)
    sign_definition(
        store, "onboarding", signer_did="did:arc:agent:rogue", private_key=impostor.private_key
    )

    assert store.load("onboarding").is_verified is False
