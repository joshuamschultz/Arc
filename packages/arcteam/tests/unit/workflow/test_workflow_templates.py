"""The four starter templates: each is a complete bundle that validates and signs (J3 F6, G5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import generate_keypair

from arcteam.workflow import (
    DefinitionStore,
    parse_definition,
    sign_definition,
    validate_definition,
)
from arcteam.workflow.errors import InvalidWorkflowIdError
from arcteam.workflow.templates import (
    TemplateNotFoundError,
    list_templates,
    load_template,
)

EXPECTED = {"intake_specialist", "fanout_synthesize", "maker_checker", "scheduled_watcher"}


def test_the_four_starter_templates_are_listed_with_titles() -> None:
    listed = {info.id: info for info in list_templates()}

    assert set(listed) == EXPECTED
    for info in listed.values():
        assert info.title.strip()
        assert info.description.strip()


@pytest.mark.parametrize("template", sorted(EXPECTED))
def test_every_template_validates_and_signs(template: str, tmp_path: Path) -> None:
    document, files = load_template(template, workflow_id="my-flow")
    keys = generate_keypair()
    store = DefinitionStore(tmp_path / "wf", operator_public_key=keys.public_key)

    definition = parse_definition(document)
    saved = store.save_draft(
        definition, actor_did="did:arc:op", expected_version=None, files=files
    )

    assert saved.status == "draft"
    assert definition.id == "my-flow"
    assert validate_definition(definition, bundle_root=saved.root) == ()
    signed = sign_definition(
        store, "my-flow", signer_did="did:arc:op", private_key=keys.private_key
    )
    assert signed.is_verified


@pytest.mark.parametrize("template", sorted(EXPECTED))
def test_templates_use_no_deleted_constructs(template: str) -> None:
    document, _ = load_template(template, workflow_id="my-flow")

    for node in document["node"]:
        assert "loop_back_to" not in node
        assert "max_iterations" not in node
        assert node.get("join", "all") == "all"


def test_the_owner_can_be_chosen_at_instantiation() -> None:
    document, _ = load_template("maker_checker", workflow_id="x", owner="@writer")

    assert document["workflow"]["owner"] == "@writer"


def test_an_unknown_template_is_refused() -> None:
    with pytest.raises(TemplateNotFoundError):
        load_template("../etc", workflow_id="x")


def test_an_illegal_workflow_id_is_refused() -> None:
    with pytest.raises(InvalidWorkflowIdError):
        load_template("maker_checker", workflow_id="../escape")


def test_the_watcher_declares_a_schedule_trigger() -> None:
    document, _ = load_template("scheduled_watcher", workflow_id="w")

    assert document["trigger"]["type"] in {"interval", "cron"}
