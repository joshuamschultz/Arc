"""The approval diff: current draft against the last SIGNED version (J3 F10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust import generate_keypair

from arcteam.workflow import DefinitionStore, parse_definition, sign_definition
from arcteam.workflow.diff import MAX_DIFF_CHARS, diff_against_last_signed

OPERATOR = "did:arc:operator:test"

V1: dict[str, Any] = {
    "workflow": {"id": "brief", "owner": "@ops", "description": "daily brief"},
    "node": [
        {"id": "gather", "kind": "agent", "agent": "@ops", "prompt": "prompts/gather.md"},
        {"id": "send", "kind": "agent", "agent": "@ops", "needs": ["gather"]},
    ],
}


def _v2() -> dict[str, Any]:
    return {
        "workflow": {"id": "brief", "owner": "@ops", "description": "daily brief"},
        "node": [
            {"id": "gather", "kind": "agent", "agent": "@ops", "prompt": "prompts/gather.md"},
            {"id": "review", "kind": "agent", "agent": "@ops", "needs": ["gather"]},
        ],
    }


def _save(
    store: DefinitionStore, document: dict[str, Any], files: dict[str, bytes], expected: int | None
) -> None:
    store.save_draft(
        parse_definition(document),
        actor_did="did:arc:agent:a",
        expected_version=expected,
        files=files,
    )


@pytest.fixture
def signed_store(tmp_path: Path) -> DefinitionStore:
    keys = generate_keypair()
    store = DefinitionStore(tmp_path / "wf", operator_public_key=keys.public_key)
    _save(store, V1, {"prompts/gather.md": b"Gather the news.\nBe brief.\n"}, None)
    sign_definition(store, "brief", signer_did=OPERATOR, private_key=keys.private_key)
    return store


def test_diff_vs_last_signed(signed_store: DefinitionStore) -> None:
    changed = _v2()
    _save(signed_store, changed, {"prompts/gather.md": b"Gather the news.\nBe thorough.\n"}, 1)

    diff = diff_against_last_signed(signed_store, "brief")

    assert diff["baseline_version"] == 1
    assert diff["nodes"] == {"added": ["review"], "removed": ["send"], "changed": []}
    [file] = diff["files"]
    assert file["path"] == "prompts/gather.md"
    assert file["status"] == "changed"
    assert file["diff"] is not None
    assert "-Be brief." in file["diff"] and "+Be thorough." in file["diff"]


def test_a_node_whose_fields_changed_is_reported_changed(signed_store: DefinitionStore) -> None:
    document = {
        "workflow": V1["workflow"],
        "node": [
            {"id": "gather", "kind": "agent", "agent": "@other", "prompt": "prompts/gather.md"},
            V1["node"][1],
        ],
    }
    _save(signed_store, document, {}, 1)

    diff = diff_against_last_signed(signed_store, "brief")

    assert diff["nodes"] == {"added": [], "removed": [], "changed": ["gather"]}
    assert diff["files"] == []


def test_a_secret_in_a_file_is_redacted_from_the_diff(signed_store: DefinitionStore) -> None:
    leak = b"Use key AKIAIOSFODNN7EXAMPLE to fetch.\n"
    _save(signed_store, _v2(), {"prompts/gather.md": leak}, 1)

    [file] = diff_against_last_signed(signed_store, "brief")["files"]

    assert file["diff"] is not None
    assert "AKIAIOSFODNN7EXAMPLE" not in file["diff"]


def test_a_huge_file_diff_is_bounded(signed_store: DefinitionStore) -> None:
    big = ("line of prompt text\n" * 5000).encode()
    _save(signed_store, _v2(), {"prompts/gather.md": big}, 1)

    [file] = diff_against_last_signed(signed_store, "brief")["files"]

    assert file["diff"] is not None
    assert len(file["diff"]) <= MAX_DIFF_CHARS + 64


def test_nothing_signed_yet_reports_everything_added(tmp_path: Path) -> None:
    store = DefinitionStore(tmp_path / "wf")
    _save(store, V1, {"prompts/gather.md": b"hello\n"}, None)

    diff = diff_against_last_signed(store, "brief")

    assert diff["baseline_version"] is None
    assert diff["nodes"]["added"] == ["gather", "send"]
    assert [(f["path"], f["status"]) for f in diff["files"]] == [("prompts/gather.md", "added")]
