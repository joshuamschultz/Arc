"""H-REG-1 turn-path bound — ``iter_source_chunks``'s ``skip_file`` short-circuit.

``SurfaceIndex.index_if_needed`` must not read every markdown card's full body
on every turn just to prove most of them are unchanged. ``skip_file`` lets it
prove that from a cheap ``stat()`` alone (mtime match) and skip the read
entirely — these tests pin the low-level primitive that guarantee rests on.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.index.source import iter_source_chunks


def _entity_file(workspace: Path, name: str, body: bytes) -> Path:
    entities = workspace / "memory" / "entities"
    entities.mkdir(parents=True, exist_ok=True)
    path = entities / f"{name}.md"
    path.write_bytes(body)
    return path


def test_skip_file_true_omits_the_chunk_without_ever_reading_it(workspace: Path) -> None:
    """A file skip_file rejects must never be opened — proven by making a read
    raise: invalid UTF-8 bytes blow up ``read_text(encoding="utf-8")`` on
    contact, so the generator completing at all IS the proof it never tried.
    """
    _entity_file(workspace, "rex", b"\xff\xfe not valid utf-8 \x80\x81")

    chunks = list(
        iter_source_chunks(workspace / "memory", workspace, [], skip_file=lambda _cid, _m: True)
    )

    assert chunks == [], "a skipped file must not be yielded"


def test_skip_file_false_reads_normally(workspace: Path) -> None:
    _entity_file(workspace, "felix", b"---\ntype: entity\nname: Felix\n---\n\nthe feline meowed")

    chunks = list(
        iter_source_chunks(workspace / "memory", workspace, [], skip_file=lambda _cid, _m: False)
    )

    assert len(chunks) == 1
    assert "feline meowed" in chunks[0].text


def test_skip_file_none_default_preserves_the_full_unconditional_walk(workspace: Path) -> None:
    """``IndexRebuilder`` never passes ``skip_file`` — must stay byte-identical."""
    _entity_file(workspace, "orca", b"---\ntype: entity\nname: Orca\n---\n\nthe pod surfaced")

    chunks = list(iter_source_chunks(workspace / "memory", workspace, []))

    assert len(chunks) == 1
    assert "pod surfaced" in chunks[0].text


def test_skip_file_receives_the_chunk_id_and_on_disk_mtime(workspace: Path) -> None:
    path = _entity_file(workspace, "sable", b"---\ntype: entity\nname: Sable\n---\n\nbody text")
    seen: list[tuple[str, float]] = []

    def _record(chunk_id: str, mtime: float) -> bool:
        seen.append((chunk_id, mtime))
        return False

    list(iter_source_chunks(workspace / "memory", workspace, [], skip_file=_record))

    assert seen == [("file:memory/entities/sable.md", path.stat().st_mtime)]
