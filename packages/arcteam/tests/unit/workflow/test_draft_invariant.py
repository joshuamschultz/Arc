"""T-834 — the security gate for the whole feature (COMP-005, REQ-223).

A fully valid definition, produced through any authoring path, at any tier,
must come out ``status="draft"``. There must exist **no** code path by which
successful validation confers signed status.

This is not a style rule. If the authoring process could also sign, a prompt
injection could author an exfiltration pipeline and bless it in the same breath
(LLM06/ASI04). The whole draft-then-operator-sign lifecycle rests on this test.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from arctrust import generate_keypair

from arcteam.workflow import (
    DefinitionStore,
    WorkflowBundle,
    parse_definition,
    sign_definition,
    validate_definition,
)
from arcteam.workflow import store as store_module

VALID_DOCUMENT: dict[str, Any] = {
    "workflow": {"id": "spotless", "owner": "@sales", "description": "flawless"},
    "trigger": {"type": "cron", "expression": "0 9 * * MON"},
    "node": [
        {"id": "collect", "kind": "agent", "agent": "@sales"},
        {"id": "verify", "kind": "tool", "tool": "crm", "agent": "@sales", "needs": ["collect"]},
        {"id": "gate", "kind": "gate", "gate": "human:ok", "needs": ["verify"]},
    ],
}


@pytest.mark.parametrize("tier", ["personal", "enterprise", "federal"])
def test_a_flawless_definition_is_still_a_draft_at_every_tier(tmp_path: Path, tier: str) -> None:
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier=tier, operator_public_key=keypair.public_key
    )
    definition = parse_definition(VALID_DOCUMENT)
    assert validate_definition(definition) == ()

    bundle = store.save_draft(definition, actor_did="did:arc:agent:sales", expected_version=None)

    assert bundle.status == "draft"
    assert store.load("spotless").status == "draft"


def test_repeated_valid_edits_never_accumulate_into_signed(tmp_path: Path) -> None:
    store = DefinitionStore(tmp_path / "workflows")
    store.save_draft(
        parse_definition(VALID_DOCUMENT), actor_did="did:arc:agent:a", expected_version=None
    )

    for version in range(1, 5):
        document = {
            **VALID_DOCUMENT,
            "workflow": {**VALID_DOCUMENT["workflow"], "description": f"pass {version}"},
        }
        bundle = store.save_draft(
            parse_definition(document), actor_did="did:arc:agent:a", expected_version=version
        )
        assert bundle.status == "draft"


def test_only_the_out_of_band_signing_function_can_produce_signed(tmp_path: Path) -> None:
    keypair = generate_keypair()
    store = DefinitionStore(
        tmp_path / "workflows", tier="federal", operator_public_key=keypair.public_key
    )
    store.save_draft(
        parse_definition(VALID_DOCUMENT), actor_did="did:arc:agent:a", expected_version=None
    )
    assert store.load("spotless").status == "draft"

    signed = sign_definition(
        store, "spotless", signer_did="did:arc:operator:x", private_key=keypair.private_key
    )

    assert signed.status == "signed"


def test_signing_requires_a_private_key_that_cannot_come_from_a_definition() -> None:
    """The signing key is a required argument, so it must be supplied out of band."""
    signature = inspect.signature(sign_definition)

    assert signature.parameters["private_key"].default is inspect.Parameter.empty
    assert signature.parameters["private_key"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["signer_did"].default is inspect.Parameter.empty


def test_no_authoring_function_constructs_a_signed_bundle() -> None:
    """Source-level guard: ``status="signed"`` is set in exactly one place.

    A behavioural test can only cover the paths it thinks of. This one reads the
    module and asserts the literal cannot appear anywhere except where the
    signature has just been verified.
    """
    tree = ast.parse(inspect.getsource(store_module))
    type_alias = {
        node
        for assignment in ast.walk(tree)
        if isinstance(assignment, ast.Assign) and _assigns(assignment, "DefinitionStatus")
        for node in ast.walk(assignment)
    }
    signing_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "signed" and node not in type_alias
    ]

    functions = {_enclosing_function(tree, node) for node in signing_sites}

    assert functions <= {"load", "load_for_run"}, (
        f'"signed" appears in {functions}; status may only be derived from a verified '
        f"signature, never assigned by an authoring path"
    )


def test_the_bundle_model_has_no_setter_for_status(tmp_path: Path) -> None:
    """Status cannot be flipped on a bundle already handed to a caller."""
    store = DefinitionStore(tmp_path / "workflows")
    bundle = store.save_draft(
        parse_definition(VALID_DOCUMENT), actor_did="did:arc:agent:a", expected_version=None
    )

    assert WorkflowBundle.model_config["frozen"] is True
    with pytest.raises(Exception, match="frozen"):
        bundle.status = "signed"  # type: ignore[misc]


def test_status_is_not_a_field_of_the_definition_itself() -> None:
    """A document can never carry its own trust status into the store."""
    from arcteam.workflow import WorkflowDefinition

    fields = set(WorkflowDefinition.model_fields)

    assert "status" not in fields
    assert "signed" not in fields
    assert "content_hash" not in fields


def test_a_document_declaring_itself_signed_is_refused() -> None:
    """Forbidding extra fields is what makes the previous test enforceable."""
    from arcteam.workflow import WorkflowParseError

    with pytest.raises(WorkflowParseError):
        parse_definition(
            {
                **VALID_DOCUMENT,
                "workflow": {**VALID_DOCUMENT["workflow"], "status": "signed"},
            }
        )


def _assigns(assignment: ast.Assign, name: str) -> bool:
    return any(isinstance(t, ast.Name) and t.id == name for t in assignment.targets)


def _enclosing_function(tree: ast.AST, target: ast.AST) -> str:
    """Name of the function containing ``target``, or ``"<module>"``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(child is target for child in ast.walk(node)):
            return node.name
    return "<module>"
