"""P14-B step 6 — every `$nodes.X.output.*` in prose must name an ancestor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from arcteam.workflow import parse_definition, validate_definition


def _document() -> dict[str, Any]:
    return {
        "workflow": {"id": "prose", "owner": "@a"},
        "node": [
            {"id": "first", "kind": "agent", "agent": "@a", "prompt": "prompts/first.md"},
            {
                "id": "second",
                "kind": "agent",
                "agent": "@a",
                "needs": ["first"],
                "prompt": "prompts/second.md",
            },
            {"id": "side", "kind": "agent", "agent": "@a", "prompt": "prompts/side.md"},
        ],
    }


def _issues(tmp_path: Path, **prompts: str) -> list[tuple[str | None, str, str]]:
    (tmp_path / "prompts").mkdir()
    for name in ("first", "second", "side"):
        (tmp_path / "prompts" / f"{name}.md").write_text(prompts.get(name, "No references."))
    issues = validate_definition(parse_definition(_document()), bundle_root=tmp_path)
    return [(i.node_id, i.field, str(i.observed)) for i in issues]


def test_prose_nodes_ref_must_be_ancestor_and_bound(tmp_path: Path) -> None:
    found = _issues(
        tmp_path,
        second="Summarise $nodes.first.output.text, then compare with $nodes.side.output.text "
        "and $nodes.ghost.output.text.",
    )

    assert ("second", "prompt", "side") in found, "a non-ancestor is never bound"
    assert ("second", "prompt", "ghost") in found, "an unknown node is never bound"
    assert ("second", "prompt", "first") not in found


def test_prose_with_only_ancestor_references_validates(tmp_path: Path) -> None:
    assert _issues(tmp_path, second="Use $nodes.first.output.text.") == []
