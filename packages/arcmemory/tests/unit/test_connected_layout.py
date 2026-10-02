"""Connected documents mirror their source path; ``relayout_source`` moves old flat ones."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from arcokf import listable_file, validate_folder_index
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent

import arcmemory.connected_data as connected_data
from arcmemory.config import MemoryConfig
from arcmemory.connected_data import (
    ApprovedMapping,
    ConnectedDataService,
    ConnectedObject,
    ConnectedSource,
    ConnectedSourceShape,
    SourceContent,
    SourceMappingPendingError,
)
from arcmemory.connected_layout import (
    LocatorRefusedError,
    flat_name,
    mirror_relpath,
    prune_empty_dirs,
)
from arcmemory.doc_index import doc_scope, object_key
from arcmemory.index.backend import open_index_backend

_DID = "did:arc:layout"


class _CountingEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0] + [float(len(text) % 7)] * 383 for text in texts]


class _Sink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _source() -> ConnectedSource:
    return ConnectedSource(
        connection_id="drive",
        account_id="account",
        source_kind="drive",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )


async def _granted(
    workspace: Path, *, embedder: Any = None, sink: Any = None
) -> tuple[ConnectedDataService, ApprovedMapping]:
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(
        workspace,
        _DID,
        approval_store=approval,
        config=MemoryConfig(doc_chunk_tokens=32),
        embedder=embedder,
        audit_sink=sink,
    )
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(_source())
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    return service, await service.require_approved_mapping(_source())


async def _ingest(
    service: ConnectedDataService,
    mapping: ApprovedMapping,
    object_id: str,
    locator: str,
    text: str = "quarterly revenue figures",
) -> None:
    await service.ingest(
        _source(),
        ConnectedObject(
            object_id=object_id,
            locator=locator,
            version="1",
            media_type="text/plain",
            classification="unclassified",
            revision=1,
        ),
        SourceContent(
            object_id=object_id, version="1", media_type="text/plain", content=text.encode()
        ),
        mapping,
    )


def _root(workspace: Path, mapping: ApprovedMapping) -> Path:
    return workspace / "memory" / "connected" / mapping.source_id


def _docs(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if listable_file(p.name))


# -- pure layout rules ------------------------------------------------------


def test_locator_path_is_mirrored_and_sanitized() -> None:
    relative = mirror_relpath("/Projects/Q4 Plan/Budget: final?.xlsx", "obj-1")
    assert relative is not None
    folders, _, name = relative.rpartition("/")
    assert folders == "Projects/Q4 Plan"
    assert name.startswith("Budget_ final_-") and name.endswith(".md")
    assert mirror_relpath("https://wiki.example/space/page", "o")  # URL host + path
    assert mirror_relpath("", "o") is None


@pytest.mark.parametrize(
    "locator",
    ["../../etc/passwd", "/a/../../b.txt", "a/%2e%2e/b.txt", "a\\..\\b.txt", "a/b\x00.txt"],
)
def test_traversal_shaped_locator_is_refused(locator: str) -> None:
    with pytest.raises(LocatorRefusedError):
        mirror_relpath(locator, "obj")


def test_hostile_segments_cannot_become_hidden_or_operational_folders() -> None:
    relative = mirror_relpath("/.git/config/audit/secrets/x.txt", "o")
    assert relative is not None
    for part in relative.split("/")[:-1]:
        assert not part.startswith(".")
        assert part.lower() not in {".git", "config", "audit", "secrets"}


def test_depth_is_capped() -> None:
    relative = mirror_relpath("/" + "/".join(f"d{n}" for n in range(40)) + "/f.txt", "o")
    assert relative is not None and relative.count("/") <= 10


def test_same_name_different_objects_never_share_a_path() -> None:
    first = mirror_relpath("/a/report.pdf", "obj-1")
    second = mirror_relpath("/a/report.docx", "obj-2")
    assert first != second


# -- ingest -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_connected_doc_path_mirrors_locator_and_index_per_folder(tmp_path: Path) -> None:
    service, mapping = await _granted(tmp_path)
    await _ingest(service, mapping, "q4", "/Projects/Q4/plan.txt")
    await _ingest(service, mapping, "top", "/readme.txt")
    await service.finish_sync(_source())

    root = _root(tmp_path, mapping)
    nested = root / "Projects" / "Q4"
    assert [p.name for p in nested.glob("*.md") if listable_file(p.name)] == [
        f"plan-{hashlib.sha256(b'q4').hexdigest()[:12]}.md"
    ]
    for folder in (root, root / "Projects", nested):
        assert validate_folder_index(folder, deep=True).valid, folder
    assert "[Projects/](Projects/index.md) - 1 doc" in (root / "index.md").read_text("utf-8")
    listing = await service.list_documents(_source())
    assert {d.object_id for d in listing} == {"q4", "top"}
    hits = await service.document_search("quarterly revenue", _source())
    assert any("Projects/Q4/plan-" in h.pointer for h in hits)

    deleted = await service.delete_document(_source(), "q4")
    assert deleted.value == "missing"
    assert not (root / "Projects").exists(), "an emptied folder is pruned with its index"


@pytest.mark.asyncio
async def test_traversal_locator_lands_flat_inside_root_and_is_audited(tmp_path: Path) -> None:
    sink = _Sink()
    service, mapping = await _granted(tmp_path / "ws", sink=sink)
    await _ingest(service, mapping, "evil", "../../../outside/pwn.txt")
    root = _root(tmp_path / "ws", mapping)
    assert (root / flat_name("evil")).is_file()
    assert not (tmp_path / "outside").exists()
    assert not list(tmp_path.glob("**/pwn*"))
    refused = [e for e in sink.events if e.action == "connected_data.object.layout_refused"]
    assert refused and refused[0].outcome == "deny"


@pytest.mark.asyncio
async def test_a_path_held_by_another_object_falls_back_to_the_flat_name(tmp_path: Path) -> None:
    service, mapping = await _granted(tmp_path)
    root = _root(tmp_path, mapping)
    await _ingest(service, mapping, "first", "/a/report.txt")
    held = next(p for p in _docs(root))
    # Plant a squatter exactly where "second" would be mirrored.
    wanted = root / mirror_relpath("/a/report.txt", "second")  # type: ignore[operator]
    wanted.parent.mkdir(parents=True, exist_ok=True)
    wanted.write_bytes(held.read_bytes())
    await _ingest(service, mapping, "second", "/a/report.txt", text="different body")
    assert (root / flat_name("second")).is_file()
    assert wanted.read_bytes() == held.read_bytes(), "a held path is never overwritten"


def test_prune_never_removes_the_root_or_a_folder_with_documents(tmp_path: Path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "keep.md").write_text("x")
    prune_empty_dirs(tmp_path / "a" / "b", tmp_path)
    assert (tmp_path / "a").is_dir() and not (tmp_path / "a" / "b").exists()
    prune_empty_dirs(tmp_path, tmp_path)
    assert tmp_path.is_dir()


# -- relayout ---------------------------------------------------------------


async def _legacy_flat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder: _CountingEmbedder, sink: _Sink
) -> tuple[ConnectedDataService, ApprovedMapping]:
    """Ingest under the old flat layout: every document at ``<sha>.md``."""

    def flat(root: Path, object_id: str, locator: str, *, current: Path | None = None):  # type: ignore[no-untyped-def]
        return root / flat_name(object_id), False

    service, mapping = await _granted(tmp_path, embedder=embedder, sink=sink)
    with monkeypatch.context() as patch:
        patch.setattr(connected_data, "target_path", flat)
        await _ingest(service, mapping, "q4", "/Projects/Q4/plan.txt", "plan figures for q4")
        await _ingest(service, mapping, "budget", "/Projects/budget.txt", "budget figures")
        await _ingest(service, mapping, "top", "/readme.txt", "read me first")
    await service.finish_sync(_source())
    return service, mapping


@pytest.mark.asyncio
async def test_relayout_keeps_chunk_ids_and_does_not_reembed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder, sink = _CountingEmbedder(), _Sink()
    service, mapping = await _legacy_flat(tmp_path, monkeypatch, embedder, sink)
    root = _root(tmp_path, mapping)
    assert {p.name for p in _docs(root)} == {flat_name(o) for o in ("q4", "budget", "top")}
    backend = open_index_backend("sqlite", db=service._db)
    scope = doc_scope(_DID, mapping.source_id).key
    before_ids = await backend.stored_hashes(scope)
    before_vectors = await backend.embedded_hashes(scope)
    embedded_before = embedder.calls

    report = await service.relayout_source(_source())

    assert (report.scanned, report.moved, report.repathed, report.state_updated) == (3, 3, 3, 3)
    assert embedder.calls == embedded_before, "relayout must not embed"
    assert await backend.stored_hashes(scope) == before_ids
    assert await backend.embedded_hashes(scope) == before_vectors
    assert [p.parent.name for p in _docs(root)].count("Q4") == 1
    assert len(_docs(root / "Projects")) == 2 and len(_docs(root)) == 3
    key = object_key(mapping.source_id, "q4")
    meta = await backend.chunk_meta(scope, f"{key}#0")
    assert meta is not None and meta[0].startswith(
        f"memory/connected/{mapping.source_id}/Projects/Q4/plan-"
    )
    for folder in (root, root / "Projects", root / "Projects" / "Q4"):
        assert validate_folder_index(folder, deep=True).valid
    hits = await service.document_search("plan figures", _source())
    assert any("Projects/Q4/plan-" in hit.pointer for hit in hits)
    state = await service._object_state.get_object_state(mapping.source_id, "q4")
    assert state is not None and "/Projects/Q4/plan-" in state.path
    assert any(e.action == "connected_data.relayout" for e in sink.events)


@pytest.mark.asyncio
async def test_relayout_twice_is_a_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    embedder, sink = _CountingEmbedder(), _Sink()
    service, mapping = await _legacy_flat(tmp_path, monkeypatch, embedder, sink)
    root = _root(tmp_path, mapping)
    await service.relayout_source(_source())
    snapshot = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}

    again = await service.relayout_source(_source())

    assert (again.moved, again.repathed, again.state_updated) == (0, 0, 0)
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}
    assert after == snapshot, "a finished relayout writes nothing"


@pytest.mark.asyncio
async def test_relayout_resumes_after_a_crash_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder, sink = _CountingEmbedder(), _Sink()
    service, _mapping = await _legacy_flat(tmp_path, monkeypatch, embedder, sink)
    embedded_before = embedder.calls
    calls = {"n": 0}
    real = service._doc_index().__class__.repath_object

    async def flaky(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("crash")
        return await real(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(service._doc_index()), "repath_object", flaky)
        with pytest.raises(RuntimeError):
            await service.relayout_source(_source())
    # Files are already moved, one chunk set repointed, state not yet updated.
    report = await service.relayout_source(_source())
    assert report.moved == 0
    # The crashed pass finished the first document; the rerun finishes the other two.
    assert report.repathed == 2 and report.state_updated == 2
    assert embedder.calls == embedded_before, "no embedding in either pass"
    final = await service.relayout_source(_source())
    assert (final.moved, final.repathed, final.state_updated) == (0, 0, 0)


@pytest.mark.asyncio
async def test_relayout_refuses_a_traversal_locator_and_keeps_the_doc_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    embedder, sink = _CountingEmbedder(), _Sink()
    service, mapping = await _granted(tmp_path / "ws", embedder=embedder, sink=sink)
    root = _root(tmp_path / "ws", mapping)
    await _ingest(service, mapping, "evil", "/ok/file.txt")
    (doc,) = _docs(root)
    text = doc.read_text("utf-8").replace("locator: /ok/file.txt", "locator: ../../escape/x.txt")
    assert "../../escape" in text
    doc.write_text(text, "utf-8")

    report = await service.relayout_source(_source())

    assert report.refused == 1
    assert [p.name for p in _docs(root)] == [flat_name("evil")]
    assert not (tmp_path / "escape").exists() and not list(tmp_path.glob("**/escape"))


@pytest.mark.asyncio
async def test_relayout_logs_nothing_and_log_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, mapping = await _legacy_flat(tmp_path, monkeypatch, _CountingEmbedder(), _Sink())
    root = _root(tmp_path, mapping)
    before = (root / "log.md").read_bytes()
    await service.relayout_source(_source())
    assert (root / "log.md").read_bytes() == before
    await service.finish_sync(_source())  # the next sync heals routing, still no new entries
    assert (root / "log.md").read_bytes() == before
    assert os.path.getsize(root / "log.md") > 0
