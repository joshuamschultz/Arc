"""F6: every reader uses the exact bytes it verified, and never follows a symlink leaf."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from arcokf import (
    DIGEST_NAME,
    INDEX_NAME,
    LOG_DIGEST_NAME,
    LOG_NAME,
    LogEntry,
    UnsafeFileError,
    folder_entry,
    read_regular_file,
    read_verified_log,
    render_change_log,
    render_folder_digest,
    render_folder_index,
    render_log_digest,
    validate_folder_index,
)


def _doc(path: Path, title: str) -> Path:
    path.write_text(f"---\ntype: Entity\ntitle: {title}\n---\nbody\n", encoding="utf-8")
    return path


def _indexed(folder: Path) -> str:
    entry = folder_entry(folder / "a.md")
    assert entry is not None
    text = render_folder_index([entry], root=False)
    (folder / INDEX_NAME).write_text(text, encoding="utf-8")
    (folder / DIGEST_NAME).write_text(render_folder_digest(text, (entry,)), encoding="utf-8")
    return text


def test_read_regular_file_refuses_a_symlink_leaf(tmp_path: Path) -> None:
    real = _doc(tmp_path / "real.md", "Real")
    os.symlink(real, tmp_path / "link.md")

    with pytest.raises(UnsafeFileError):
        read_regular_file(tmp_path / "link.md")
    assert read_regular_file(real) == real.read_bytes()


def test_read_regular_file_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe.md")

    with pytest.raises(UnsafeFileError):
        read_regular_file(tmp_path / "pipe.md")


def test_read_regular_file_refuses_a_swapped_inode(tmp_path: Path) -> None:
    doc = _doc(tmp_path / "a.md", "A")
    seen = os.stat(doc)
    doc.unlink()
    _doc(tmp_path / "a.md", "A")  # same name, same bytes, a different file

    with pytest.raises(UnsafeFileError):
        read_regular_file(doc, expect=seen)


def test_folder_entry_never_follows_a_symlinked_document(tmp_path: Path) -> None:
    secret = _doc(tmp_path / "elsewhere.md", "Other Agent Secret")
    folder = tmp_path / "f"
    folder.mkdir()
    os.symlink(secret, folder / "a.md")

    assert folder_entry(folder / "a.md") is None


def test_validation_returns_the_exact_verified_text_and_sidecar(tmp_path: Path) -> None:
    _doc(tmp_path / "a.md", "A")
    text = _indexed(tmp_path)

    validation = validate_folder_index(tmp_path)

    assert validation.valid
    assert validation.text == text
    assert validation.digest is not None and set(validation.digest.docs) == {"a.md"}
    sidecar = (tmp_path / DIGEST_NAME).read_bytes()
    assert validation.sidecar_sha == hashlib.sha256(sidecar).hexdigest()


def test_symlinked_index_or_sidecar_is_refused(tmp_path: Path) -> None:
    _doc(tmp_path / "a.md", "A")
    _indexed(tmp_path)
    elsewhere = tmp_path / "copy"
    elsewhere.mkdir()
    for name in (INDEX_NAME, DIGEST_NAME):
        (elsewhere / name).write_bytes((tmp_path / name).read_bytes())
        (tmp_path / name).unlink()
        os.symlink(elsewhere / name, tmp_path / name)

    assert not validate_folder_index(tmp_path).valid


def test_deep_validation_refuses_a_symlinked_listed_document(tmp_path: Path) -> None:
    _doc(tmp_path / "a.md", "A")
    _indexed(tmp_path)
    copy = tmp_path / "same-bytes.md.bak"
    copy.write_bytes((tmp_path / "a.md").read_bytes())
    (tmp_path / "a.md").unlink()
    os.symlink(copy, tmp_path / "a.md")

    assert not validate_folder_index(tmp_path, deep=True).valid


def test_verified_log_refuses_a_symlinked_log(tmp_path: Path) -> None:
    text = render_change_log([LogEntry("2026-10-02", "Creation", "a.md", "A")])
    (tmp_path / "real-log").write_text(text, encoding="utf-8")
    os.symlink(tmp_path / "real-log", tmp_path / LOG_NAME)
    (tmp_path / LOG_DIGEST_NAME).write_text(render_log_digest(text, {}), encoding="utf-8")

    assert read_verified_log(tmp_path) is None
