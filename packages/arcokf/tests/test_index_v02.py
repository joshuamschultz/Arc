from __future__ import annotations

from pathlib import Path

import pytest
from arcokf import (
    DIGEST_NAME,
    DiagnosticCode,
    FolderIndexError,
    IndexEntry,
    folder_entry,
    folder_summary_entry,
    lint,
    parse_folder_index,
    render_folder_digest,
    render_folder_index,
    validate,
    validate_folder_index,
)


def _doc(
    path: Path, *, kind: str = "Entity", title: str = "", label: str = "", body: str = "x"
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [f"type: {kind}"]
    if title:
        meta.append(f"title: {title}")
    if label:
        meta.append(f"classification: {label}")
    path.write_text("---\n" + "\n".join(meta) + f"\n---\n{body}\n", encoding="utf-8")


def _write_index(folder: Path, *, root: bool = False) -> None:
    entries = [
        entry
        for path in sorted(folder.glob("*.md"))
        if path.name != "index.md" and (entry := folder_entry(path)) is not None
    ]
    text = render_folder_index(entries, root=root)
    (folder / "index.md").write_text(text, encoding="utf-8")
    (folder / DIGEST_NAME).write_text(render_folder_digest(text, tuple(entries)), encoding="utf-8")


def test_root_frontmatter_only_okf_version() -> None:
    root = render_folder_index((), root=True)
    assert root.startswith('---\nokf_version: "0.2"\n---\n')
    assert validate(root, path="index.md", bundle_root=True).valid
    # Not the root: any frontmatter on an index is refused.
    refused = validate(root, path="sub/index.md")
    assert not refused.valid
    assert refused.diagnostics[0].code is DiagnosticCode.RESERVED_DOCUMENT
    # The root may say nothing but okf_version.
    extra = '---\nokf_version: "0.2"\ntitle: x\n---\n# Documents\n'
    assert not validate(extra, path="index.md", bundle_root=True).valid
    assert not validate('---\nokf_version: "0.1"\n---\n', path="index.md", bundle_root=True).valid
    assert validate(render_folder_index((), root=False), path="index.md").valid


def test_optional_fields_tolerated_when_absent_and_checked_when_present() -> None:
    assert validate("---\ntype: Entity\n---\nx\n").valid
    good = (
        "---\ntype: Entity\ntitle: T\ndescription: D\n"
        "generated:\n  by: process:arcmemory\n  at: '2026-10-02'\n---\nx\n"
    )
    assert validate(good).valid
    assert not validate("---\ntype: Entity\ntitle: [a]\n---\nx\n").valid
    assert not validate("---\ntype: Entity\ngenerated: nope\n---\nx\n").valid
    assert validate("---\ntype: Entity\nmystery: 1\n---\nx\n").valid


def test_format_is_grouped_spec_shape_without_digests() -> None:
    text = render_folder_index(
        [
            IndexEntry("b.md", "Beta", "second", "Insight"),
            IndexEntry("a.md", "Alpha", "first", "Entity"),
            folder_summary_entry("sub", 3),
        ],
        root=False,
    )
    assert text == (
        "# Folders\n* [sub/](sub/index.md) - 3 docs\n\n"
        "# Entity\n* [Alpha](a.md) - first\n\n"
        "# Insight\n* [Beta](b.md) - second\n"
    )
    assert "sha256" not in text
    assert parse_folder_index(text, root=False)[0].count == 3


def test_classified_entry_carries_its_label_and_roundtrips() -> None:
    text = render_folder_index(
        [IndexEntry("s.md", "Secret", "plan", "Entity", "SECRET")], root=False
    )
    assert "* [Secret](s.md) - plan (classification: secret)" in text
    (entry,) = parse_folder_index(text, root=False)
    assert entry.classification == "secret"
    assert entry.description == "plan"


def test_paths_must_be_relative_listable_documents() -> None:
    for bad in ("../x.md", "/x.md", "a/b.md", "index.md", "config.toml"):
        with pytest.raises(FolderIndexError):
            render_folder_index([IndexEntry(bad, "T", "", "Entity")], root=False)


def test_folder_entry_lists_every_valid_document_with_its_label(tmp_path: Path) -> None:
    _doc(tmp_path / "pub.md", title="Public", label="unclassified")
    _doc(tmp_path / "sec.md", title="Secret Plan", label="SECRET")
    _doc(tmp_path / "bare.md", title="Bare")
    (tmp_path / "bad.md").write_text("---\ntype: [x]\n---\nbad\n", encoding="utf-8")
    entries = {path.name: folder_entry(path) for path in tmp_path.glob("*.md")}
    assert entries["bad.md"] is None
    secret = entries["sec.md"]
    assert secret is not None and secret.classification == "secret"
    bare = entries["bare.md"]
    assert bare is not None and bare.classification == ""


def test_shallow_validation_fails_closed_on_index_edit(tmp_path: Path) -> None:
    _doc(tmp_path / "a.md", title="Alpha")
    _write_index(tmp_path)
    assert validate_folder_index(tmp_path).valid
    index = tmp_path / "index.md"
    index.write_text(index.read_text() + "* [evil](evil.md)\n", encoding="utf-8")
    assert not validate_folder_index(tmp_path).valid


def test_deep_validation_catches_changed_missing_and_unlisted_documents(tmp_path: Path) -> None:
    _doc(tmp_path / "a.md", title="Alpha")
    _write_index(tmp_path)
    assert validate_folder_index(tmp_path, deep=True).valid
    _doc(tmp_path / "b.md", title="Beta")
    deep = validate_folder_index(tmp_path, deep=True)
    assert not deep.valid and "stale" in deep.error
    assert validate_folder_index(tmp_path).valid  # shallow does not read documents
    (tmp_path / "b.md").unlink()
    (tmp_path / "a.md").write_text((tmp_path / "a.md").read_text() + "changed\n", encoding="utf-8")
    assert "digest mismatch" in validate_folder_index(tmp_path, deep=True).error


def test_root_index_validates_as_root(tmp_path: Path) -> None:
    _doc(tmp_path / "a.md", title="Alpha")
    _write_index(tmp_path, root=True)
    assert validate_folder_index(tmp_path, root=True).valid
    assert not validate_folder_index(tmp_path, root=False).valid
    assert lint(tmp_path / "index.md", bundle_root=True).valid
