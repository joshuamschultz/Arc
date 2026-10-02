"""H-REG-1 turn-path bound — ``iter_source_chunks``'s ``skip_file`` short-circuit.

``SurfaceIndex.index_if_needed`` must not read every markdown card's full body
on every turn just to prove most of them are unchanged. ``skip_file`` lets it
prove that from a cheap ``stat()`` alone (mtime match) and skip the read
entirely — these tests pin the low-level primitive that guarantee rests on.
"""

from __future__ import annotations

from pathlib import Path

from arcmemory.collection_index import memory_maintainer
from arcmemory.index.source import MAX_CHUNK_BYTES, iter_source_chunks
from arcmemory.types import Event


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


# -- size bound (tsvector poison fix): oversized files split into windows -----


def _daily_log(workspace: Path, name: str, body: str) -> Path:
    directory = workspace / "memory" / "daily-log"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_small_file_stays_a_single_base_id_chunk(workspace: Path) -> None:
    """A file under the byte budget is one chunk keyed by the plain ``file:`` id —
    window 0 carries no ``#`` suffix, so existing card ids never change."""
    _entity_file(workspace, "wren", b"---\ntype: entity\nname: Wren\n---\n\nsmall body")

    chunks = list(iter_source_chunks(workspace / "memory", workspace, []))

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "file:memory/entities/wren.md"


def test_oversized_file_splits_into_bounded_windows_window0_keeps_base_id(
    workspace: Path,
) -> None:
    """An oversized daily-log must never become one chunk that overflows the
    Postgres tsvector limit — it splits into windows each under the byte budget,
    window 0 keeping the base id and the rest suffixed ``#1``, ``#2`` …"""
    # ~3x the budget of distinct paragraphs, so it must span several windows.
    entries = "\n\n".join(f"paragraph number {i} " + "x" * 400 for i in range(600))
    body = f"---\ntype: daily-log\n---\n\n{entries}"
    assert len(body.encode("utf-8")) > MAX_CHUNK_BYTES * 2
    _daily_log(workspace, "2026-07-17", body)

    chunks = [
        c
        for c in iter_source_chunks(workspace / "memory", workspace, [])
        if c.source_path == "memory/daily-log/2026-07-17.md"
    ]

    assert len(chunks) >= 3, "an oversized file must fan out into several windows"
    assert chunks[0].chunk_id == "file:memory/daily-log/2026-07-17.md"
    assert [c.chunk_id for c in chunks[1:]] == [
        f"file:memory/daily-log/2026-07-17.md#{i}" for i in range(1, len(chunks))
    ]
    for c in chunks:
        assert len(c.text.encode("utf-8")) <= MAX_CHUNK_BYTES, "every window must fit the budget"


def test_file_too_large_for_okf_degrades_instead_of_aborting_the_walk(workspace: Path) -> None:
    """A daily-log past the 2 MB OKF ceiling must not raise out of the walk (which
    would stop ALL of the agent's indexing). It degrades to raw windows with an
    empty (fail-closed) classification, and every sibling file still indexes."""
    entries = "\n\n".join(f"line {i} " + "y" * 60 for i in range(35000))  # > 2 MB
    _daily_log(workspace, "2026-07-18", f"---\ntype: daily-log\n---\n\n{entries}")
    _entity_file(workspace, "sibling", b"---\ntype: entity\nname: Sib\n---\n\nstill indexed")

    chunks = list(iter_source_chunks(workspace / "memory", workspace, []))

    ids = {c.chunk_id for c in chunks}
    assert "file:memory/entities/sibling.md" in ids, "a sibling file must still be walked"
    log_chunks = [c for c in chunks if c.source_path == "memory/daily-log/2026-07-18.md"]
    assert len(log_chunks) >= 2, "the too-large log is degraded into raw windows, not dropped"
    assert all(c.classification == "" for c in log_chunks), "degraded windows are fail-closed"
    for c in log_chunks:
        assert len(c.text.encode("utf-8")) <= MAX_CHUNK_BYTES


def test_oversized_folder_index_splits_into_bounded_windows(workspace: Path) -> None:
    """A large folder ``index.md`` (thousands of routing lines) must not become
    one chunk that overflows the tsvector limit — it too is bounded."""
    mem = workspace / "memory"
    for i in range(1500):
        _entity_file(
            workspace,
            f"item-{i:05d}",
            f"---\ntype: Entity\ntitle: Item {i:05d} {'x' * 30}\n---\n\nbody\n".encode(),
        )
    memory_maintainer(mem).sync_all()
    index = mem / "entities" / "index.md"
    assert len(index.read_text("utf-8").encode("utf-8")) > MAX_CHUNK_BYTES

    chunks = [
        c
        for c in iter_source_chunks(mem, workspace, [])
        if c.source_path == "memory/entities/index.md"
    ]

    assert len(chunks) >= 2
    assert chunks[0].chunk_id == "file:memory/entities/index.md"
    for c in chunks:
        assert len(c.text.encode("utf-8")) <= MAX_CHUNK_BYTES


def test_index_chunks_contain_routing_lines_only(workspace: Path) -> None:
    """Embedded index text is headings + listing lines: never ``okf_version``
    frontmatter, digests, the sidecar, or the dirty journal."""
    mem = workspace / "memory"
    _entity_file(
        workspace,
        "felix",
        b"---\ntype: Entity\ntitle: Felix\nclassification: unclassified\n---\n\nthe feline\n",
    )
    _entity_file(
        workspace,
        "ledger",
        b"---\ntype: Entity\nentity_type: mapping\ntitle: Ledger\n---\n\nbookkeeping\n",
    )
    memory_maintainer(mem).sync_all()
    (mem / ".dirty").write_text("entities\n", encoding="utf-8")

    chunks = list(iter_source_chunks(mem, workspace, []))

    by_id = {c.chunk_id: c for c in chunks}
    root = by_id["file:memory/index.md"]
    assert root.text == "# Folders\n* [entities/](entities/index.md)"  # no count, no frontmatter
    folder = by_id["file:memory/entities/index.md"]
    assert (
        folder.text == "# Entity\n* [Felix](felix.md) - the feline (classification: unclassified)"
    )  # bookkeeping card dropped
    assert folder.classification == "unclassified"
    for chunk in chunks:
        assert "okf_version" not in chunk.text
        assert "sha256" not in chunk.text
    assert not any(c.source_path.endswith((".index.digest", ".dirty")) for c in chunks)


def test_unlabeled_listed_document_makes_the_routing_chunk_fail_closed(workspace: Path) -> None:
    mem = workspace / "memory"
    _entity_file(workspace, "bare", b"---\ntype: Entity\ntitle: Bare\n---\n\nno label\n")
    memory_maintainer(mem).sync_all()

    chunks = {c.chunk_id: c for c in iter_source_chunks(mem, workspace, [])}

    assert chunks["file:memory/entities/index.md"].classification == ""


def test_oversized_event_splits_into_bounded_windows(workspace: Path) -> None:
    """A giant raw event (e.g. an ingested transcript) must also stay under the
    budget — window 0 keeps ``event:<id>``, extras are suffixed."""
    huge = "sentence. " * 40000  # well over the byte budget
    event = Event(
        event_id="e-big",
        ts="2026-08-01T00:00:00Z",
        scope="did:arc:test-agent",
        kind="obs",
        text=huge,
    )

    chunks = [c for c in iter_source_chunks(workspace / "memory", workspace, [event])]

    assert len(chunks) >= 2
    assert chunks[0].chunk_id == "event:e-big"
    assert chunks[1].chunk_id == "event:e-big#1"
    for c in chunks:
        assert len(c.text.encode("utf-8")) <= MAX_CHUNK_BYTES
