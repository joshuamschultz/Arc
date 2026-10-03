"""The Drive source adapter over a fake Drive HTTP API (the shipped native attachment).

Everything under the adapter is real: the native tools, the HTTP helper, the
credential handle. Only Google's wire is faked, so a pass means the requests the
tools build and the cursor the adapter keeps agree with how Drive behaves.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SyncSource,
)

from extensions.google_workspace.arc_ext_google_workspace.drive_source import DriveSourceAdapter
from extensions.google_workspace.arc_ext_google_workspace.native import build_native_attachment
from extensions.google_workspace.arc_ext_google_workspace.source import (
    GmailSourceAdapter,
    build_source_adapters,
)
from extensions.tests.fake_credential import FakeCredentialHandle
from packages.arcagent.tests.drive_fake import DOC, EXPORT_LIMIT, FOLDER, PDF, SHEET, FakeDrive

CONNECTION = "g:drive"


def _adapter(drive: FakeDrive, credential: Any = None) -> DriveSourceAdapter:
    attachment = build_native_attachment(
        {
            "credential": credential or FakeCredentialHandle(["token"]),
            "account": "me@example.com",
            "read_only": "yes",
            "download_dir": "",
            "transport": drive.transport(),
        }
    )
    return DriveSourceAdapter(attachment, account="me@example.com")


async def _sync(
    adapter: DriveSourceAdapter, checkpoint: str | None, *, root: str = "", size: int = 200
) -> tuple[list[SourceObject], str]:
    """Run sync pages until the source says it is done; return the objects and cursor."""
    objects: list[SourceObject] = []
    while True:
        page = await adapter.sync_source(
            SyncSource(
                connection_id=CONNECTION, checkpoint=checkpoint, root_locator=root, page_size=size
            )
        )
        objects.extend(page.objects)
        checkpoint = page.next_checkpoint
        if not page.has_more:
            return objects, checkpoint


async def _fetch(adapter: DriveSourceAdapter, item: SourceObject, **kwargs: Any) -> Any:
    return await adapter.fetch_source(
        FetchSourceObject(
            connection_id=CONNECTION,
            object_id=item.object_id,
            version=item.version or "",
            **kwargs,
        )
    )


def _ids(objects: list[SourceObject]) -> set[str]:
    return {item.object_id for item in objects if not item.deleted}


def _deleted(objects: list[SourceObject]) -> set[str]:
    return {item.object_id for item in objects if item.kind is SourceObjectKind.DELETED}


@pytest.fixture
def drive() -> FakeDrive:
    fake = FakeDrive()
    fake.add("folder-a", "Plans", FOLDER)
    fake.add("doc-1", "Launch plan", DOC, "The launch is on Tuesday.")
    fake.add("pdf-1", "Contract.pdf", PDF, b"%PDF-1.4 fake")
    return fake


async def test_snapshot_lists_documents_with_provenance_then_changes_are_incremental(
    drive: FakeDrive,
) -> None:
    adapter = _adapter(drive)
    snapshot, cursor = await _sync(adapter, None)

    assert _ids(snapshot) == {"doc-1", "pdf-1"}, "a folder is not a document"
    doc = next(item for item in snapshot if item.object_id == "doc-1")
    assert doc.metadata["title"] == "Launch plan"
    assert doc.metadata["url"] == "https://drive.google.com/file/d/doc-1/view"
    assert doc.metadata["owner"] == "Ann Owner <ann@example.com>"
    assert doc.modified_at is not None and doc.version == doc.modified_at
    assert doc.media_type == "text/plain"

    drive.add("doc-2", "Budget", DOC, "Budget is 90 thousand.")
    drive.edit("doc-1", "The launch moved to Friday.")
    changed, cursor = await _sync(adapter, cursor)
    assert _ids(changed) == {"doc-2", "doc-1"}
    assert next(i for i in changed if i.object_id == "doc-1").version != doc.version

    quiet, _ = await _sync(adapter, cursor)
    assert quiet == [], "nothing changed, nothing re-read"


async def test_the_start_token_is_taken_before_the_snapshot_so_nothing_is_missed(
    drive: FakeDrive,
) -> None:
    adapter = _adapter(drive)
    page = await adapter.sync_source(
        SyncSource(connection_id=CONNECTION, checkpoint=None, page_size=1)
    )
    assert page.has_more
    drive.add("late", "Late file", DOC, "added mid snapshot")
    objects, cursor = await _sync(adapter, page.next_checkpoint, size=1)
    after, _ = await _sync(adapter, cursor)

    assert "late" in _ids(objects) | _ids(after)


async def test_trashed_and_unshared_files_become_tombstones(drive: FakeDrive) -> None:
    adapter = _adapter(drive)
    _, cursor = await _sync(adapter, None)

    drive.trash("doc-1")
    drive.unshare("pdf-1")
    changed, _ = await _sync(adapter, cursor)

    assert _deleted(changed) == {"doc-1", "pdf-1"}
    assert all(item.deleted and item.metadata["revision"] > 10**9 for item in changed)


async def test_a_google_doc_is_exported_to_text_and_a_sheet_to_csv(drive: FakeDrive) -> None:
    drive.add("sheet-1", "Numbers", SHEET, "a,b\n1,2\n")
    adapter = _adapter(drive)
    snapshot, _ = await _sync(adapter, None)
    by_id = {item.object_id: item for item in snapshot}

    doc = await _fetch(adapter, by_id["doc-1"])
    sheet = await _fetch(adapter, by_id["sheet-1"])

    assert (doc.media_type, doc.content) == ("text/plain", b"The launch is on Tuesday.")
    assert (sheet.media_type, sheet.content) == ("text/plain", b"a,b\n1,2\n")
    exports = [r for r in drive.requests if r.url.path.endswith("/export")]
    assert {r.url.params["mimeType"] for r in exports} == {"text/plain", "text/csv"}


async def test_a_regular_file_is_downloaded(drive: FakeDrive) -> None:
    adapter = _adapter(drive)
    snapshot, _ = await _sync(adapter, None)
    pdf = next(item for item in snapshot if item.object_id == "pdf-1")

    content = await _fetch(adapter, pdf)

    assert (content.media_type, content.content) == (PDF, b"%PDF-1.4 fake")
    assert any(r.url.params.get("alt") == "media" for r in drive.requests)


async def test_a_file_that_changed_between_listing_and_fetch_is_a_version_change(
    drive: FakeDrive,
) -> None:
    adapter = _adapter(drive)
    snapshot, _ = await _sync(adapter, None)
    doc = next(item for item in snapshot if item.object_id == "doc-1")
    drive.edit("doc-1", "newer")

    with pytest.raises(SourceError) as refusal:
        await _fetch(adapter, doc)

    assert refusal.value.code is SourceFailureCode.VERSION_CHANGED


async def test_an_invalid_page_token_is_a_dead_checkpoint_not_a_blip(drive: FakeDrive) -> None:
    adapter = _adapter(drive)
    stale = json.dumps(
        {"v": 1, "mode": "changes", "scope": "all", "page_token": "expired-token"},
        separators=(",", ":"),
    )

    with pytest.raises(SourceError) as refusal:
        await _sync(adapter, stale)
    assert refusal.value.code is SourceFailureCode.CHECKPOINT_INVALID

    objects, _ = await _sync(adapter, None)
    assert _ids(objects) == {"doc-1", "pdf-1"}, "a fresh snapshot recovers"


@pytest.mark.parametrize("garbage", ["not json", '{"v":2,"mode":"changes"}', '{"v":1}'])
async def test_a_garbled_checkpoint_is_invalid(drive: FakeDrive, garbage: str) -> None:
    with pytest.raises(SourceError) as refusal:
        await _sync(_adapter(drive), garbage)
    assert refusal.value.code is SourceFailureCode.CHECKPOINT_INVALID


async def test_a_large_file_is_declared_big_and_refused_with_a_typed_reason(
    drive: FakeDrive,
) -> None:
    drive.add("big-pdf", "Huge.pdf", PDF, b"x", declared_size=EXPORT_LIMIT + 1)
    drive.add("big-doc", "Huge doc", DOC, b"y" * (EXPORT_LIMIT + 1))
    adapter = _adapter(drive)
    snapshot, _ = await _sync(adapter, None)
    big_pdf = next(item for item in snapshot if item.object_id == "big-pdf")
    big_doc = next(item for item in snapshot if item.object_id == "big-doc")

    assert big_pdf.size == EXPORT_LIMIT + 1, "the coordinator skips on the declared size"
    for item in (big_pdf, big_doc):
        with pytest.raises(SourceError) as refusal:
            await _fetch(adapter, item)
        assert refusal.value.code is SourceFailureCode.TOO_LARGE
    assert not [r for r in drive.requests if r.url.params.get("alt") == "media"], (
        "an oversized file is never downloaded"
    )


async def test_the_caller_cap_is_respected_below_the_ten_megabyte_ceiling(
    drive: FakeDrive,
) -> None:
    adapter = _adapter(drive)
    snapshot, _ = await _sync(adapter, None)
    pdf = next(item for item in snapshot if item.object_id == "pdf-1")

    with pytest.raises(SourceError) as refusal:
        await _fetch(adapter, pdf, max_bytes=4)

    assert refusal.value.code is SourceFailureCode.TOO_LARGE


async def test_an_unreadable_type_is_a_typed_per_object_refusal(drive: FakeDrive) -> None:
    drive.add("pic", "Photo.png", "image/png", b"\x89PNG")
    adapter = _adapter(drive)
    snapshot, _ = await _sync(adapter, None)
    pic = next(item for item in snapshot if item.object_id == "pic")
    assert pic.media_type == "image/png"

    with pytest.raises(SourceError) as refusal:
        await _fetch(adapter, pic)

    assert refusal.value.code is SourceFailureCode.UNSUPPORTED_CONTENT
    assert "image/png" in refusal.value.detail


async def test_a_folder_selection_covers_its_subtree_and_follows_moves(
    drive: FakeDrive,
) -> None:
    drive.add("folder-b", "Q4", FOLDER, parents=("folder-a",))
    drive.add("in-a", "In plans", DOC, "x", parents=("folder-a",))
    drive.add("in-b", "In Q4", DOC, "y", parents=("folder-b",))
    adapter = _adapter(drive)
    root = "folder:folder-a"

    snapshot, cursor = await _sync(adapter, None, root=root)
    assert _ids(snapshot) == {"in-a", "in-b"}, "doc-1 and pdf-1 sit outside the folder"

    drive.edit("in-b", "y2")
    drive.edit("doc-1", "unrelated edit")
    drive.move("in-a", ("root",))
    changed, _ = await _sync(adapter, cursor, root=root)

    assert _ids(changed) == {"in-b"}
    assert _deleted(changed) >= {"doc-1", "in-a"}, "a file outside the scope is a tombstone"


async def test_a_shared_drive_selection_keeps_only_that_drive(drive: FakeDrive) -> None:
    drive.shared_drives["sd1"] = "Finance"
    drive.add("fin-1", "Ledger", DOC, "ledger", parents=("sd1",), drive_id="sd1")
    adapter = _adapter(drive)
    root = "drive:sd1"

    snapshot, cursor = await _sync(adapter, None, root=root)
    drive.add("fin-2", "Audit", DOC, "audit", parents=("sd1",), drive_id="sd1")
    drive.edit("doc-1", "elsewhere")
    changed, _ = await _sync(adapter, cursor, root=root)

    assert _ids(snapshot) == {"fin-1"}
    assert _ids(changed) == {"fin-2"}


async def test_changing_the_selection_restarts_from_a_snapshot(drive: FakeDrive) -> None:
    adapter = _adapter(drive)
    _, cursor = await _sync(adapter, None)

    with pytest.raises(SourceError) as refusal:
        await _sync(adapter, cursor, root="folder:folder-a")

    assert refusal.value.code is SourceFailureCode.CHECKPOINT_INVALID


async def test_resources_offer_all_shared_drives_and_folders_and_selection_sticks(
    drive: FakeDrive,
) -> None:
    drive.shared_drives["sd1"] = "Finance"
    adapter = _adapter(drive)
    resources = await adapter.list_source_resources(ListSourceResources(connection_id=CONNECTION))

    assert {r.resource_id: r.resource_kind for r in resources} == {
        "all": "drive",
        "drive:sd1": "shared_drive",
        "folder:folder-a": "folder",
    }
    await adapter.select_source_resources(
        SelectSourceResources(
            connection_id=CONNECTION, resource_ids=("folder:folder-a", "drive:sd1")
        )
    )
    described = await adapter.inspect_source(InspectSource(connection_id=CONNECTION))
    assert described.root_locator == "folder:folder-a,drive:sd1"
    assert described.source_kind == "google_drive" and described.supports_deletes
    with pytest.raises(SourceError):
        await adapter.select_source_resources(
            SelectSourceResources(connection_id=CONNECTION, resource_ids=("folder:nope",))
        )


async def test_a_401_invalidates_the_credential_and_retries_once(drive: FakeDrive) -> None:
    fake = FakeDrive(bearer_ok=lambda bearer: bearer == "fresh")
    fake.add("doc-1", "Launch plan", DOC, "text")
    credential = FakeCredentialHandle(["stale", "fresh"])
    adapter = _adapter(fake, credential)

    objects, _ = await _sync(adapter, None)

    assert _ids(objects) == {"doc-1"}
    assert credential.invalidations == 1


async def test_a_revoked_grant_is_an_account_wide_auth_failure_not_an_object_skip() -> None:
    fake = FakeDrive(bearer_ok=lambda _bearer: False)
    adapter = _adapter(fake)

    with pytest.raises(SourceError) as refusal:
        await _sync(adapter, None)

    assert refusal.value.code is SourceFailureCode.AUTH_REQUIRED


async def test_one_grant_exposes_mail_and_drive_as_separate_streams() -> None:
    sources = build_source_adapters(
        {
            "attachment": build_native_attachment(
                {
                    "credential": FakeCredentialHandle(["t"]),
                    "account": "me@example.com",
                    "read_only": "yes",
                    "download_dir": "",
                    "transport": httpx.MockTransport(lambda _request: httpx.Response(404)),
                }
            ),
            "account": "me@example.com",
        }
    )

    assert set(sources) == {"", "drive"}
    assert isinstance(sources[""], GmailSourceAdapter)
    assert isinstance(sources["drive"], DriveSourceAdapter)
