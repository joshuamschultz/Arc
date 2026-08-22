"""TDD coverage for streaming attachment custody and claim authorization."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import pytest

from arcgateway.attachment_scanner import ScanStatus
from arcgateway.media_store import (
    AttachmentClaimError,
    AttachmentQuotaError,
    AttachmentValidationError,
    MediaStore,
    MediaTooLargeError,
)

pytestmark = pytest.mark.asyncio

_PNG = b"\x89PNG\r\n\x1a\n" + b"payload"
_OWNER = "did:arc:user:alice"
_AGENT = "did:arc:agent:olivia"
_SESSION = "session-1"


class ChunkStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.seen = 0
        self.max_chunk = 0

    def __aiter__(self) -> ChunkStream:
        return self

    async def __anext__(self) -> bytes:
        await asyncio.sleep(0)
        if self.seen == len(self.chunks):
            raise StopAsyncIteration
        chunk = self.chunks[self.seen]
        self.seen += 1
        self.max_chunk = max(self.max_chunk, len(chunk))
        return chunk


class Scanner:
    def __init__(self, verdict: ScanStatus = ScanStatus.CLEAN) -> None:
        self.verdict = verdict
        self.calls: list[tuple[Path, str, str]] = []

    async def scan(self, path: Path, *, mime: str, sha256: str) -> ScanStatus:
        self.calls.append((path, mime, sha256))
        return self.verdict


class FailingStream(ChunkStream):
    async def __anext__(self) -> bytes:
        if self.seen == 1:
            raise RuntimeError("stream disconnected")
        return await super().__anext__()


async def store(store: MediaStore, stream: ChunkStream, **kwargs: str):
    return await store.store_stream(
        stream=stream,
        declared_name="photo.png",
        declared_mime="image/png",
        kind="image",
        owner_did=_OWNER,
        agent_did=_AGENT,
        session_key=_SESSION,
        **kwargs,
    )


async def test_streams_chunks_to_quarantine_and_promotes_content_addressed_object(
    tmp_path: Path,
) -> None:
    scanner = Scanner()
    stream = ChunkStream([_PNG[:4], _PNG[4:]])
    manifest = await store(MediaStore(workspace=tmp_path, max_bytes=1024, scanner=scanner), stream)

    assert stream.max_chunk == len(_PNG) - 4
    assert manifest.scan_status is ScanStatus.CLEAN
    assert manifest.sha256 == f"sha256:{hashlib.sha256(_PNG).hexdigest()}"
    assert re.fullmatch(r"att_[0-9a-f]{32}", manifest.attachment_id)
    assert manifest.workspace_ref.startswith("attachments/objects/")
    object_path = tmp_path / manifest.workspace_ref
    assert object_path.read_bytes() == _PNG
    assert not list((tmp_path / "attachments" / "quarantine").glob("*"))
    assert scanner.calls and scanner.calls[0][0].name.startswith(".att_")


async def test_over_limit_cleans_partial_quarantine(tmp_path: Path) -> None:
    stream = ChunkStream([_PNG[:8], b"x" * 10])
    with pytest.raises(MediaTooLargeError):
        await store(MediaStore(workspace=tmp_path, max_bytes=8), stream)
    assert not list((tmp_path / "attachments").rglob("*upload"))
    assert not list((tmp_path / "attachments" / "objects").rglob("*"))


async def test_stream_failure_cleans_partial_quarantine(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="disconnected"):
        await store(
            MediaStore(workspace=tmp_path, max_bytes=1024), FailingStream([_PNG[:4], _PNG[4:]])
        )
    assert not list((tmp_path / "attachments" / "quarantine").glob("*"))
    assert not list((tmp_path / "attachments" / "objects").rglob("*"))


async def test_exact_byte_limit_is_accepted(tmp_path: Path) -> None:
    manifest = await store(
        MediaStore(workspace=tmp_path, max_bytes=len(_PNG)), ChunkStream([_PNG])
    )
    assert manifest.size_bytes == len(_PNG)


async def test_scanner_rejection_never_promotes(tmp_path: Path) -> None:
    scanner = Scanner(ScanStatus.REJECTED)
    with pytest.raises(AttachmentValidationError):
        await store(
            MediaStore(workspace=tmp_path, max_bytes=1024, scanner=scanner), ChunkStream([_PNG])
        )
    assert not list((tmp_path / "attachments" / "objects").rglob("*"))
    assert not list((tmp_path / "attachments" / "quarantine").glob("*"))


async def test_declared_mime_mismatch_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AttachmentValidationError):
        await MediaStore(workspace=tmp_path, max_bytes=1024).store_stream(
            stream=ChunkStream([_PNG]),
            declared_name="photo.png",
            declared_mime="application/pdf",
            kind="image",
            owner_did=_OWNER,
            agent_did=_AGENT,
            session_key=_SESSION,
        )


async def test_quota_hooks_reject_file_count_and_bytes(tmp_path: Path) -> None:
    limited_files = MediaStore(workspace=tmp_path / "files", max_bytes=1024, max_files=0)
    with pytest.raises(AttachmentQuotaError):
        await store(limited_files, ChunkStream([_PNG]))

    limited_bytes = MediaStore(
        workspace=tmp_path / "bytes", max_bytes=1024, max_total_bytes=len(_PNG) - 1
    )
    with pytest.raises(AttachmentQuotaError):
        await store(limited_bytes, ChunkStream([_PNG]))


async def test_storage_modes_and_manifest_contain_no_absolute_path(tmp_path: Path) -> None:
    manifest = await store(MediaStore(workspace=tmp_path, max_bytes=1024), ChunkStream([_PNG]))
    object_path = tmp_path / manifest.workspace_ref
    assert object_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "attachments").stat().st_mode & 0o777 == 0o700
    assert not manifest.workspace_ref.startswith("/")
    assert str(tmp_path) not in manifest.model_dump_json()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_did", "did:arc:user:bob"),
        ("agent_did", "did:arc:agent:other"),
        ("session_key", "session-2"),
    ],
)
async def test_claim_binding_fails_closed(tmp_path: Path, field: str, value: str) -> None:
    manifest = await store(MediaStore(workspace=tmp_path, max_bytes=1024), ChunkStream([_PNG]))
    kwargs = {
        "attachment_id": manifest.attachment_id,
        "owner_did": _OWNER,
        "agent_did": _AGENT,
        "session_key": _SESSION,
    }
    kwargs[field] = value
    with pytest.raises(AttachmentClaimError):
        MediaStore(workspace=tmp_path, max_bytes=1024).claim(**kwargs)


async def test_claim_enforces_classification_clearance(tmp_path: Path) -> None:
    manifest = await store(
        MediaStore(workspace=tmp_path, max_bytes=1024, clearance="CUI"),
        ChunkStream([_PNG]),
        classification="CUI",
    )
    with pytest.raises(AttachmentClaimError):
        MediaStore(workspace=tmp_path, max_bytes=1024, clearance="UNCLASSIFIED").claim(
            attachment_id=manifest.attachment_id,
            owner_did=_OWNER,
            agent_did=_AGENT,
            session_key=_SESSION,
        )


async def test_claim_requires_clean_status(tmp_path: Path) -> None:
    manifest = await store(MediaStore(workspace=tmp_path, max_bytes=1024), ChunkStream([_PNG]))
    path = tmp_path / "attachments" / "manifests" / f"{manifest.attachment_id}.json"
    path.write_text(
        manifest.model_copy(update={"scan_status": ScanStatus.REJECTED}).model_dump_json()
    )
    with pytest.raises(AttachmentClaimError):
        MediaStore(workspace=tmp_path, max_bytes=1024).claim(
            attachment_id=manifest.attachment_id,
            owner_did=_OWNER,
            agent_did=_AGENT,
            session_key=_SESSION,
        )


async def test_symlinked_attachment_root_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / "attachments").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AttachmentValidationError):
        await store(
            MediaStore(workspace=tmp_path / "workspace", max_bytes=1024), ChunkStream([_PNG])
        )
    assert not list(outside.rglob("*"))
