from __future__ import annotations

from pathlib import Path

import pytest
from arcokf import (
    CollectionEntry,
    CollectionIndexError,
    inventory_documents,
    render_collection_index,
    validate_collection_index,
)


def test_collection_index_is_deterministic_and_contains_digests() -> None:
    entries = [
        CollectionEntry("zeta.md", "Zeta", "f" * 64, "a short summary"),
        CollectionEntry("alpha.md", "Alpha", "a" * 64, "another summary"),
    ]

    rendered = render_collection_index(entries)

    assert rendered == render_collection_index(list(reversed(entries)))
    assert rendered.index("alpha.md") < rendered.index("zeta.md")
    assert "sha256:" + "a" * 64 in rendered


def test_collection_index_verifies_paths_and_content(tmp_path: Path) -> None:
    document = tmp_path / "alpha.md"
    document.write_text(
        "---\ntype: Entity\nclassification: unclassified\ntitle: Alpha\n---\nAlpha\n",
        encoding="utf-8",
    )
    digest = __import__("hashlib").sha256(document.read_bytes()).hexdigest()
    index = tmp_path / "index.md"
    index.write_text(
        render_collection_index([CollectionEntry("alpha.md", "Alpha", digest, "Alpha")]),
        encoding="utf-8",
    )

    assert validate_collection_index(index, tmp_path).valid
    document.write_text(document.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    result = validate_collection_index(index, tmp_path)
    assert not result.valid
    assert "stale" in result.error.lower()


def test_collection_index_rejects_omitted_document(tmp_path: Path) -> None:
    first = tmp_path / "alpha.md"
    second = tmp_path / "beta.md"
    first.write_text("---\ntype: Entity\n---\nAlpha\n", encoding="utf-8")
    second.write_text("---\ntype: Entity\n---\nBeta\n", encoding="utf-8")
    import hashlib

    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    index = tmp_path / "index.md"
    index.write_text(
        render_collection_index([CollectionEntry("alpha.md", "alpha", digest)]),
        encoding="utf-8",
    )

    result = validate_collection_index(index, tmp_path)
    assert not result.valid
    assert "stale" in result.error


def test_collection_index_rejects_reserved_and_operational_paths() -> None:
    with pytest.raises(CollectionIndexError):
        render_collection_index([CollectionEntry("config.toml", "Config", "a" * 64, "")])


def test_inventory_excludes_classified_missing_and_invalid_documents(tmp_path: Path) -> None:
    (tmp_path / "public.md").write_text(
        "---\ntype: Entity\nclassification: unclassified\ntitle: Public\n---\nPublic summary\n",
        encoding="utf-8",
    )
    (tmp_path / "secret.md").write_text(
        "---\ntype: Entity\nclassification: SECRET\ntitle: Secret Plan\n---\nDo not leak\n",
        encoding="utf-8",
    )
    (tmp_path / "missing-label.md").write_text(
        "---\ntype: Entity\ntitle: Missing Label\n---\nPrivate\n", encoding="utf-8"
    )
    (tmp_path / "invalid.md").write_text("---\ntype: [bad]\n---\nInvalid\n", encoding="utf-8")

    entries = inventory_documents(tmp_path)

    assert [entry.path for entry in entries] == ["public.md"]
    rendered = render_collection_index(entries)
    assert "Secret Plan" not in rendered
    assert "missing-label.md" not in rendered
