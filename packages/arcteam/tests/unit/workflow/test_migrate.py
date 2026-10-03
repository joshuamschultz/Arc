"""Legacy-bundle migration: the alpha-2 regression where every workflow vanished.

The old serializer wrote ``join = "all"`` on every node; the alpha-2 schema
forbids the field, so every saved bundle stopped parsing. These tests pin the
explicit one-time migration (no parser shim) and the health check that makes a
broken bundle visible instead of silently absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust import InProcessSigner, generate_keypair, sign_artifact

from arcteam.workflow import DefinitionStore, WorkflowParseError
from arcteam.workflow.migrate import check_bundle, check_store, migrate_bundle, migrate_store
from arcteam.workflow.store import DEFINITION_FILE, SIDECAR_FILE

LEGACY = """[workflow]
schema_version = "1.0"
id = "morning"
version = 1
owner = "@olivia"

[trigger]
type = "cron"
expression = "0 7 * * *"

[[node]]
id = "a"
kind = "agent"
agent = "@olivia"
join = "all"

[[node]]
id = "b"
kind = "agent"
agent = "@olivia"
needs = ["a"]
join = "all"
"""

Events = list[tuple[str, dict[str, Any]]]


@pytest.fixture
def keys() -> Any:
    return generate_keypair()


@pytest.fixture
def events() -> Events:
    return []


@pytest.fixture
def store(tmp_path: Path, keys: Any, events: Events) -> DefinitionStore:
    return DefinitionStore(
        tmp_path / "workflows",
        operator_public_key=keys.public_key,
        audit=lambda event, payload: events.append((event, payload)),
    )


def _legacy(store: DefinitionStore, keys: Any, text: str = LEGACY, wid: str = "morning") -> Path:
    body = text.replace('id = "morning"', f'id = "{wid}"')
    bundle = store.root / wid
    bundle.mkdir(parents=True)
    (bundle / DEFINITION_FILE).write_text(body)
    (bundle / "versions").mkdir()
    (bundle / "versions" / "1.toml").write_text(body)
    old_signature = sign_artifact(
        b"signed-under-the-old-canonical-form",
        signer_did="did:arc:op",
        private_key=keys.private_key,
    )
    (bundle / SIDECAR_FILE).write_text(old_signature.to_json())
    return bundle


def test_legacy_bundle_is_reported_unreadable_not_missing(
    store: DefinitionStore, keys: Any
) -> None:
    _legacy(store, keys)
    with pytest.raises(WorkflowParseError):
        store.load("morning")
    check = check_bundle(store, "morning")
    assert check.state == "unreadable"
    assert "join" in check.detail
    assert check.fix_action == "migrate"


def test_dry_run_lists_the_change_and_touches_nothing(store: DefinitionStore, keys: Any) -> None:
    bundle = _legacy(store, keys)
    before = (bundle / DEFINITION_FILE).read_text()
    results = migrate_store(store, dry_run=True)
    assert [r.workflow_id for r in results] == ["morning"]
    assert results[0].action == "would_rewrite"
    assert set(results[0].nodes) == {"a", "b"}
    assert (bundle / DEFINITION_FILE).read_text() == before


def test_migrate_resign_makes_it_parse_verify_and_audit(
    store: DefinitionStore, keys: Any, events: Events
) -> None:
    bundle = _legacy(store, keys)
    signer = InProcessSigner(keys.private_key)
    results = migrate_store(store, dry_run=False, signer=signer, signer_did="did:arc:op")
    assert results[0].action == "rewritten"
    assert "join" not in (bundle / DEFINITION_FILE).read_text()
    assert "join" not in (bundle / "versions" / "1.toml").read_text()
    assert store.load("morning").is_verified
    assert check_bundle(store, "morning").state == "ok"
    names = [name for name, _ in events]
    assert "workflow.migrated" in names
    assert "workflow.signed" in names


def test_migrate_without_resign_leaves_it_needing_resign(
    store: DefinitionStore, keys: Any
) -> None:
    _legacy(store, keys)
    migrate_store(store, dry_run=False)
    assert check_bundle(store, "morning").state == "needs_resign"


def test_join_any_bundle_is_refused_untouched(store: DefinitionStore, keys: Any) -> None:
    text = LEGACY.replace('needs = ["a"]\njoin = "all"', 'needs = ["a"]\njoin = "any"')
    bundle = _legacy(store, keys, text)
    before = (bundle / DEFINITION_FILE).read_text()
    results = migrate_store(store, dry_run=False)
    assert results[0].action == "refused"
    assert "join" in results[0].reason
    assert "b" in results[0].reason
    assert (bundle / DEFINITION_FILE).read_text() == before


@pytest.mark.parametrize("field", ['loop_back_to = "a"', "max_iterations = 3"])
def test_loop_fields_are_refused_untouched(store: DefinitionStore, keys: Any, field: str) -> None:
    bundle = _legacy(store, keys, LEGACY.replace('needs = ["a"]', f'needs = ["a"]\n{field}'))
    before = (bundle / DEFINITION_FILE).read_text()
    results = migrate_store(store, dry_run=False)
    assert results[0].action == "refused"
    assert (bundle / DEFINITION_FILE).read_text() == before


def test_migration_is_idempotent(store: DefinitionStore, keys: Any, events: Events) -> None:
    _legacy(store, keys)
    signer = InProcessSigner(keys.private_key)
    migrate_store(store, dry_run=False, signer=signer, signer_did="did:arc:op")
    count = len(events)
    again = migrate_store(store, dry_run=False, signer=signer, signer_did="did:arc:op")
    assert all(r.action == "unchanged" for r in again)
    assert len(events) == count


def test_check_store_lists_every_failure(store: DefinitionStore, keys: Any) -> None:
    _legacy(store, keys, wid="one")
    _legacy(store, keys, wid="two")
    states = {c.workflow_id: c.state for c in check_store(store)}
    assert states == {"one": "unreadable", "two": "unreadable"}


def test_canonical_document_excludes_defaults(store: DefinitionStore, keys: Any) -> None:
    """Schema evolution (a new defaulted field) must not void signatures."""
    _legacy(store, keys)
    migrate_store(
        store, dry_run=False, signer=InProcessSigner(keys.private_key), signer_did="did:arc:op"
    )
    document = store.load("morning").definition.canonical_document()
    assert all("on_failure" not in node for node in document["node"])
    assert document["workflow"]["schema_version"] == "1.0"


def test_a_draft_and_a_stale_signature_name_the_sign_action(
    store: DefinitionStore, keys: Any
) -> None:
    fixed = LEGACY.replace('join = "all"\n', "")
    draft = store.root / "draft"
    draft.mkdir(parents=True)
    (draft / DEFINITION_FILE).write_text(fixed.replace('id = "morning"', 'id = "draft"'))
    assert check_bundle(store, "draft").fix_action == "sign"


def test_migrate_bundle_touches_only_the_named_workflow(store: DefinitionStore, keys: Any) -> None:
    other = _legacy(store, keys, wid="other")
    _legacy(store, keys)
    before = (other / DEFINITION_FILE).read_text()
    signer = InProcessSigner(keys.private_key)
    result = migrate_bundle(
        store, "morning", dry_run=False, signer=signer, signer_did="did:arc:op"
    )
    assert result.action == "rewritten"
    assert store.load("morning").is_verified
    assert (other / DEFINITION_FILE).read_text() == before


def test_resign_refuses_a_signer_that_is_not_the_pinned_operator_key(
    store: DefinitionStore, keys: Any
) -> None:
    bundle = _legacy(store, keys)
    stranger = InProcessSigner(generate_keypair().private_key)
    result = migrate_bundle(
        store, "morning", dry_run=False, signer=stranger, signer_did="did:arc:stranger"
    )
    assert result.action == "refused"
    assert "pinned" in result.reason
    assert "join" in (bundle / DEFINITION_FILE).read_text()
