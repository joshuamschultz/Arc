"""What the Dropbox adapter puts on the wire.

The conformance suite (``test_connector_bundles``) proves the manifest and the
adapter agree and that an unconfigured probe refuses by name. This file proves the
part only a live call exercises: that the adapter asks its credential handle for a
bearer on every request, carries it on both Dropbox hosts, and reaches the files
API for a read and a write. Token renewal is Arc's; it is not under test here.

httpx is mocked at the transport, so no socket is opened; every assertion is on
the request the shipped adapter actually built.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    SourceAdapter,
    SourceError,
    SourceFailureCode,
    SourceObjectKind,
    SyncSource,
)
from arcagent.modules.connectors.install import build_attachment

from extensions.tests.fake_credential import FakeCredentialHandle

_BUNDLE = Path(__file__).resolve().parents[1] / "dropbox"
_TOKEN = "AT-live-xyz"


def _attachment(handle: FakeCredentialHandle | None = None) -> Any:
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper: Any = build_attachment(
        manifest,
        _BUNDLE,
        {},
        credential=handle or FakeCredentialHandle(bearer_values=[_TOKEN]),  # type: ignore[arg-type]  # structural stand-in
    )
    return wrapper._delegate


class _Recorder:
    """Answers Dropbox's three hosts and remembers what each request carried."""

    def __init__(self) -> None:
        self.list_body: dict[str, Any] | None = None
        self.upload_arg: str | None = None
        self.upload_body: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers.get("authorization") == f"Bearer {_TOKEN}", url
        if url.endswith("/2/users/get_current_account"):
            return httpx.Response(200, json={"email": "josh@example.com"})
        if url.endswith("/2/files/list_folder"):
            self.list_body = json.loads(request.content)
            return httpx.Response(200, json={"entries": [{"name": "Arc"}]})
        if url.endswith("/2/files/upload"):
            self.upload_arg = request.headers.get("dropbox-api-arg")
            self.upload_body = request.content.decode("utf-8")
            return httpx.Response(200, json={"path_display": "/Notes/note.md"})
        return httpx.Response(404, json={"error": f"unmapped {url}"})


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Recorder]:
    rec = _Recorder()
    real: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(rec)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    yield rec


async def test_probe_reads_the_account(recorder: _Recorder) -> None:
    result = await _attachment().probe()

    assert result.reachable
    assert "josh@example.com" in result.detail


async def test_a_call_works_after_close_source_closed_the_client(recorder: _Recorder) -> None:
    """close_source() closes the shared httpx client (source-lifecycle teardown),
    but the SAME instance also serves the agent's interactive tools. A later call
    must recreate the client, not raise 'client has been closed'. Regression for a
    connection that probed green yet failed every tool call after a sync."""
    att = _attachment()
    assert (await att.probe()).reachable  # first use
    await att.close_source()  # a source operation releases the client
    # The connection must still be usable — the client is recreated on demand.
    assert (await att.probe()).reachable


async def test_a_bearer_is_asked_for_on_every_request(recorder: _Recorder) -> None:
    handle = FakeCredentialHandle(bearer_values=[_TOKEN])
    attachment = _attachment(handle)

    await attachment.invoke("dropbox_account", {})
    await attachment.invoke("dropbox_list", {})

    assert handle.bearer_calls == 2, "the attachment cached a bearer instead of asking per request"


async def test_list_reaches_list_folder_with_a_rooted_path(recorder: _Recorder) -> None:
    result = await _attachment().invoke("dropbox_list", {"path": "Meetings", "limit": "5"})

    assert result.outcome == "ok"
    assert recorder.list_body == {"path": "/Meetings", "recursive": False, "limit": 5}


async def test_upload_sends_content_to_the_content_host(recorder: _Recorder) -> None:
    result = await _attachment().invoke(
        "dropbox_upload", {"path": "/Notes/note.md", "content": "hello", "mode": "overwrite"}
    )

    assert result.outcome == "ok"
    assert recorder.upload_body == "hello"
    assert recorder.upload_arg is not None
    arg = json.loads(recorder.upload_arg)
    assert arg["path"] == "/Notes/note.md"
    assert arg["mode"] == "overwrite"


async def test_a_traversal_path_is_refused_before_any_request(recorder: _Recorder) -> None:
    result = await _attachment().invoke("dropbox_download", {"path": "/a/../../etc/passwd"})

    assert result.outcome == "error"
    assert "invalid Dropbox path" in result.content


def _mock_attachment(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> Any:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    return _attachment()


async def test_source_sync_maps_initial_and_incremental_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url.endswith("/2/files/list_folder"):
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            ".tag": "file",
                            "id": "id:one",
                            "path_display": "/one.pdf",
                            "rev": "r1",
                            "content_hash": "hash1",
                            "size": 12,
                        }
                    ],
                    "cursor": "c1",
                    "has_more": True,
                },
            )
        if url.endswith("/2/files/list_folder/continue"):
            assert json.loads(request.content) == {"cursor": "c1"}
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {".tag": "deleted", "path_lower": "/old.pdf"},
                        {
                            ".tag": "file",
                            "id": "id:one",
                            "path_display": "/moved.pdf",
                            "rev": "r2",
                        },
                    ],
                    "cursor": "c2",
                    "has_more": False,
                },
            )
        return httpx.Response(404)

    attachment = _mock_attachment(monkeypatch, handler)
    assert isinstance(attachment, SourceAdapter)
    first = await attachment.sync_source(SyncSource(connection_id="dbx:a", page_size=50))
    second = await attachment.sync_source(
        SyncSource(connection_id="dbx:a", checkpoint=first.next_checkpoint)
    )
    await attachment.close_source()

    assert first.has_more and first.next_checkpoint == "c1"
    assert first.objects[0].object_id == "id:one"
    assert first.objects[0].media_type == "application/pdf"
    assert second.next_checkpoint == "c2"
    assert second.objects[0].kind is SourceObjectKind.DELETED
    assert second.objects[1].object_id == "id:one"


async def test_binary_fetch_preserves_bytes_and_refuses_a_changed_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = b"%PDF-\x00\xffcontent"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Dropbox-API-Result": json.dumps(
                    {
                        "id": "id:pdf",
                        "rev": "r2",
                        "path_display": "/report.pdf",
                        "content_hash": "hash2",
                    }
                )
            },
            content=binary,
        )

    attachment = _mock_attachment(monkeypatch, handler)
    content = await attachment.fetch_source(
        FetchSourceObject(connection_id="dbx:a", object_id="id:pdf", version="r2", max_bytes=50)
    )
    with pytest.raises(SourceError) as changed:
        await attachment.fetch_source(
            FetchSourceObject(connection_id="dbx:a", object_id="id:pdf", version="r1")
        )
    await attachment.close_source()

    assert content.content == binary
    assert content.media_type == "application/pdf"
    assert changed.value.code is SourceFailureCode.VERSION_CHANGED


async def test_retry_is_bounded_and_a_401_invalidates_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401)
        if calls == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={"account_id": "dbid:a", "email": "a@example.com"},
        )

    handle = FakeCredentialHandle(bearer_values=[_TOKEN])
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    attachment = _attachment(handle)
    description = await attachment.inspect_source(InspectSource(connection_id="dbx:a"))
    await attachment.close_source()

    assert description.account_id == "dbid:a"
    assert handle.invalidations == 1
    assert calls == 3


async def test_checkpoint_reset_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error_summary": "reset/invalid_cursor"})

    attachment = _mock_attachment(monkeypatch, handler)
    with pytest.raises(SourceError) as raised:
        await attachment.sync_source(SyncSource(connection_id="dbx:a", checkpoint="stale"))
    await attachment.close_source()
    assert raised.value.code is SourceFailureCode.CHECKPOINT_INVALID


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, SourceFailureCode.RATE_LIMITED), (503, SourceFailureCode.TRANSIENT)],
)
async def test_retry_budget_is_bounded(
    monkeypatch: pytest.MonkeyPatch, status: int, code: SourceFailureCode
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"Retry-After": "0"})

    attachment = _mock_attachment(monkeypatch, handler)
    with pytest.raises(SourceError) as raised:
        await attachment.inspect_source(InspectSource(connection_id="dbx:a"))
    await attachment.close_source()

    assert raised.value.code is code
    assert calls == 3


async def test_binary_fetch_enforces_the_byte_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Dropbox-API-Result": json.dumps(
                    {"id": "id:large", "rev": "r1", "path_display": "/large.bin"}
                )
            },
            content=b"0123456789",
        )

    attachment = _mock_attachment(monkeypatch, handler)
    with pytest.raises(SourceError) as raised:
        await attachment.fetch_source(
            FetchSourceObject(
                connection_id="dbx:a", object_id="id:large", version="r1", max_bytes=5
            )
        )
    await attachment.close_source()
    assert raised.value.code is SourceFailureCode.TOO_LARGE


async def test_concurrent_connections_use_their_own_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer token-")
        return httpx.Response(
            200, json={"account_id": f"dbid:{token}", "email": f"{token}@example.com"}
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    first = _attachment(FakeCredentialHandle(bearer_values=["token-a"]))
    second = _attachment(FakeCredentialHandle(bearer_values=["token-b"]))
    results = await asyncio.gather(
        first.inspect_source(InspectSource(connection_id="dbx:a")),
        first.inspect_source(InspectSource(connection_id="dbx:a")),
        second.inspect_source(InspectSource(connection_id="dbx:b")),
    )
    await first.close_source()
    await second.close_source()

    assert [result.account_id for result in results] == ["dbid:a", "dbid:a", "dbid:b"]


def test_a_changed_file_gets_a_higher_revision_so_edits_reindex() -> None:
    """A later edit must carry a strictly higher revision, or ArcMemory rejects it.

    Dropbox's ``rev`` is the content version but is not monotonic, so without a
    real revision an edited file was ingested once and every later change was
    refused as out of order. ``server_modified`` supplies the monotonic value.
    """
    from extensions.dropbox.arc_ext_dropbox import _source_object

    older = _source_object(
        {
            ".tag": "file",
            "id": "id:x",
            "path_display": "/a.txt",
            "rev": "r1",
            "server_modified": "2026-08-01T10:00:00Z",
        }
    )
    newer = _source_object(
        {
            ".tag": "file",
            "id": "id:x",
            "path_display": "/a.txt",
            "rev": "r2",
            "server_modified": "2026-08-02T10:00:00Z",
        }
    )

    assert isinstance(older.metadata["revision"], int)
    assert newer.metadata["revision"] > older.metadata["revision"]

    # A file with no modified time still ingests the first time (revision 1).
    no_time = _source_object({".tag": "file", "id": "id:y", "path_display": "/b.txt", "rev": "r1"})
    assert no_time.metadata["revision"] == 1


class _FakeDropboxFiles:
    """An in-memory Dropbox: path -> (size, content_hash). Answers metadata and copy."""

    def __init__(self, files: dict[str, tuple[int, str]]) -> None:
        self.files = dict(files)
        self.copies: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content)
        if url.endswith("/2/files/get_metadata"):
            path = body["path"]
            if path not in self.files:
                return httpx.Response(409, json={"error_summary": "path/not_found/.."})
            size, digest = self.files[path]
            return httpx.Response(
                200,
                json={".tag": "file", "path_display": path, "size": size, "content_hash": digest},
            )
        if url.endswith("/2/files/copy_v2"):
            source, target = body["from_path"], body["to_path"]
            if target in self.files:
                return httpx.Response(409, json={"error_summary": "to/conflict/file/.."})
            self.files[target] = self.files[source]
            self.copies.append((source, target))
            return httpx.Response(200, json={"metadata": {"path_display": target}})
        return httpx.Response(404, json={"error": f"unmapped {url}"})


def _fake_dropbox(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, tuple[int, str]]
) -> _FakeDropboxFiles:
    fake = _FakeDropboxFiles(files)
    real: Callable[..., httpx.AsyncClient] = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(fake)}),
    )
    return fake


_ARCHIVE = "/CRM/transcripts"


async def _archive(files: Any, **extra: Any) -> Any:
    return await _attachment().invoke(
        "dropbox_archive_copy", {"files": files, "dest_root": _ARCHIVE, **extra}
    )


async def test_archive_copy_copies_server_side_into_a_meeting_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_dropbox(monkeypatch, {"/Meetings/standup/a.txt": (10, "h1")})

    result = await _archive(["/Meetings/standup/a.txt"])

    assert result.outcome.value != "error", result.content
    assert json.loads(result.content) == {
        "status": "archived",
        "count": 1,
        "archived": ["standup/a.txt"],
        "skipped": [],
    }
    assert fake.copies == [("/Meetings/standup/a.txt", f"{_ARCHIVE}/standup/a.txt")]


async def test_archive_copy_is_idempotent_and_accepts_object_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_dropbox(monkeypatch, {"/Meetings/m/a.txt": (10, "h1")})
    await _archive([{"path_display": "/Meetings/m/a.txt"}])

    again = json.loads((await _archive([{"path": "/Meetings/m/a.txt"}])).content)

    assert again["archived"] == [] and again["skipped"] == ["m/a.txt"]
    assert len(fake.copies) == 1


async def test_archive_copy_never_overwrites_a_different_archived_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_dropbox(
        monkeypatch,
        {"/Meetings/m/a.txt": (10, "new"), f"{_ARCHIVE}/m/a.txt": (10, "old")},
    )

    result = await _archive(["/Meetings/m/a.txt"])

    assert result.outcome.value == "error"
    assert "conflict" in result.content
    assert fake.copies == []


async def test_archive_copy_keeps_progress_when_one_file_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_dropbox(monkeypatch, {"/Meetings/m/ok.txt": (1, "h")})

    result = await _archive(["/Meetings/m/gone.txt", "/Meetings/m/ok.txt"])

    assert result.outcome.value == "error"
    assert "gone.txt" in result.content
    assert fake.copies == [("/Meetings/m/ok.txt", f"{_ARCHIVE}/m/ok.txt")]


@pytest.mark.parametrize("bad", ["/Meetings/../secret.txt", "/Meetings"])
async def test_archive_copy_refuses_paths_outside_a_meeting_file(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    fake = _fake_dropbox(monkeypatch, {})
    result = await _archive([bad])
    assert result.outcome.value == "error"
    assert fake.copies == []


async def test_archive_copy_bounds_count_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_dropbox(monkeypatch, {"/Meetings/m/big.txt": (2048, "h")})

    many = await _archive([f"/Meetings/m/{i}.txt" for i in range(501)])
    big = await _archive(["/Meetings/m/big.txt"], max_file_bytes="1024")

    assert "too many" in many.content
    assert "too large" in big.content
    assert fake.copies == []


async def test_archive_copy_refuses_a_non_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_dropbox(monkeypatch, {})
    result = await _archive({"artifact_ref": {"task_id": "t"}})
    assert result.outcome.value == "error"
    assert "files" in result.content
