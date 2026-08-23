"""Adversarial contract tests for the optional S3-compatible source bundle.

These tests use an in-process SDK double: they exercise the native extension
through the shipped factory but never contact AWS or MinIO.  The assertions are
deliberately at the source seam because every connected-data consumer relies on
the same discovery, reconciliation, and bounded-fetch behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest
from arcagent.extension.secrets import Secret
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceError,
    SourceFailureCode,
    SourceObjectKind,
    SyncSource,
)
from arcagent.modules.connectors.install import build_attachment

_BUNDLE = Path(__file__).resolve().parents[1] / "s3"
_ACCESS_KEY = "AKIA-connector-test"
_SECRET_KEY = "s3-secret-must-never-escape"
_SESSION_TOKEN = "session-token-must-never-escape"
_MINIO_ENDPOINT = "https://minio.test:9000"


class _Body:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, amount: int = -1) -> bytes:
        self.read_sizes.append(amount)
        return self._content if amount < 0 else self._content[:amount]

    def close(self) -> None:
        self.closed = True


class _ClientError(Exception):
    """Enough of botocore's public error shape for the adapter contract."""

    def __init__(self, status: int, code: str, *, retry_after: str | None = None) -> None:
        super().__init__(f"{code} ({status})")
        self.response: dict[str, Any] = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }
        if retry_after is not None:
            self.response["ResponseMetadata"]["HTTPHeaders"] = {"retry-after": retry_after}


class _S3Double:
    def __init__(self) -> None:
        self.pages: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.heads: dict[str, dict[str, Any]] = {}
        self.bodies: dict[str, _Body] = {}
        self.closed = False

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": [{"Name": "alpha"}]}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.list_calls.append(kwargs)
        return self.pages.pop(0)

    def head_object(self, **kwargs: str) -> dict[str, Any]:
        return self.heads[kwargs["Key"]]

    def get_object(self, **kwargs: str) -> dict[str, Any]:
        key = kwargs["Key"]
        head = self.heads[key]
        return {"Body": self.bodies[key], "ContentType": head.get("ContentType")}

    def close(self) -> None:
        self.closed = True


def _attachment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoint_url: str = _MINIO_ENDPOINT,
    session_token: str = _SESSION_TOKEN,
    role_arn: str = "arn:aws:iam::123456789012:role/arc-read-only",
) -> tuple[Any, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def client(service: str, **kwargs: Any) -> _S3Double:
        assert service == "s3"
        calls.append(kwargs)
        return _S3Double()

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=client))
    botocore = SimpleNamespace()
    exceptions = SimpleNamespace(
        ClientError=Exception,
        NoCredentialsError=RuntimeError,
        PartialCredentialsError=RuntimeError,
        BotoCoreError=RuntimeError,
        EndpointConnectionError=RuntimeError,
        ReadTimeoutError=RuntimeError,
    )
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.exceptions", exceptions)
    manifest = load_manifest(
        (_BUNDLE / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL
    )
    wrapper = build_attachment(
        manifest,
        _BUNDLE,
        {
            "access_key_id": Secret(_ACCESS_KEY),
            "secret_access_key": Secret(_SECRET_KEY),
            "region": Secret("us-east-1"),
            "endpoint_url": Secret(endpoint_url),
            "session_token": Secret(session_token),
            "role_arn": Secret(role_arn),
        },
    )
    return wrapper._delegate, calls


async def test_s3_and_minio_share_the_same_vault_injected_client_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment, calls = _attachment(monkeypatch)

    await attachment.probe()

    assert calls == [
        {
            "aws_access_key_id": _ACCESS_KEY,
            "aws_secret_access_key": _SECRET_KEY,
            "aws_session_token": _SESSION_TOKEN,
            "region_name": "us-east-1",
            "endpoint_url": _MINIO_ENDPOINT,
        }
    ]


async def test_aws_default_endpoint_uses_the_same_session_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment, calls = _attachment(monkeypatch, endpoint_url="")

    await attachment.probe()

    assert calls[0]["endpoint_url"] is None
    assert calls[0]["aws_session_token"] == _SESSION_TOKEN


async def test_prefixes_are_discovered_and_a_selected_prefix_bounds_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment, _ = _attachment(monkeypatch)
    client = _S3Double()
    client.pages = [
        {
            "CommonPrefixes": [{"Prefix": "engineering/"}, {"Prefix": "legal/"}],
            "IsTruncated": False,
        },
        {
            "CommonPrefixes": [{"Prefix": "engineering/"}, {"Prefix": "legal/"}],
            "IsTruncated": False,
        },
        {
            "Contents": [{"Key": "engineering/design.md", "ETag": '"v1"', "Size": 4}],
            "IsTruncated": False,
        },
    ]
    attachment._client = client

    resources = await attachment.list_source_resources(ListSourceResources(connection_id="s3:one"))
    await attachment.select_source_resources(
        SelectSourceResources(connection_id="s3:one", resource_ids=("alpha:engineering/",))
    )
    page = await attachment.sync_source(SyncSource(connection_id="s3:one", page_size=25))

    assert {resource.resource_id for resource in resources} >= {"alpha", "alpha:engineering/"}
    assert page.objects[0].object_id == "alpha:engineering/design.md"
    assert client.list_calls[0] == {"Bucket": "alpha", "Delimiter": "/", "MaxKeys": 1_000}
    assert client.list_calls[-1]["Prefix"] == "engineering/"


async def test_continuation_preserves_etag_versions_and_content_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment, _ = _attachment(monkeypatch)
    client = _S3Double()
    client.pages = [
        {"CommonPrefixes": [{"Prefix": "docs/"}], "IsTruncated": False},
        {
            "Contents": [
                {
                    "Key": "docs/alpha.pdf",
                    "ETag": '"old-etag"',
                    "Size": 4,
                    "ContentType": "application/pdf",
                    "Metadata": {"classification": "internal"},
                }
            ],
            "NextContinuationToken": "next-token",
            "IsTruncated": True,
        },
        {
            "Contents": [
                {
                    "Key": "docs/alpha.pdf",
                    "ETag": '"new-etag"',
                    "Size": 5,
                    "ContentType": "application/pdf",
                    "Metadata": {"classification": "internal"},
                }
            ],
            "IsTruncated": False,
        },
    ]
    attachment._client = client
    await attachment.select_source_resources(
        SelectSourceResources(connection_id="s3:one", resource_ids=("alpha:docs/",))
    )

    first = await attachment.sync_source(SyncSource(connection_id="s3:one", page_size=10))
    second = await attachment.sync_source(
        SyncSource(connection_id="s3:one", checkpoint=first.next_checkpoint, page_size=10)
    )

    assert first.has_more and first.next_checkpoint == "next-token"
    assert second.objects[0].version == "new-etag"
    assert second.objects[0].content_hash == "new-etag"
    assert second.objects[0].metadata["classification"] == "internal"
    continuation_calls = [
        call for call in client.list_calls if call.get("ContinuationToken") == "next-token"
    ]
    assert continuation_calls == [
        {"Bucket": "alpha", "Prefix": "docs/", "MaxKeys": 10, "ContinuationToken": "next-token"}
    ]


async def test_completed_inventory_diff_emits_tombstones_for_deleted_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 has no change feed, so a later complete inventory must retract absences."""
    attachment, _ = _attachment(monkeypatch)
    client = _S3Double()
    client.pages = [
        {"CommonPrefixes": [{"Prefix": "docs/"}], "IsTruncated": False},
        {
            "Contents": [{"Key": "docs/removed.txt", "ETag": '"v1"', "Size": 1}],
            "IsTruncated": False,
        },
        {"Contents": [], "IsTruncated": False},
    ]
    attachment._client = client
    await attachment.select_source_resources(
        SelectSourceResources(connection_id="s3:one", resource_ids=("alpha:docs/",))
    )

    first = await attachment.sync_source(SyncSource(connection_id="s3:one"))
    second = await attachment.sync_source(
        SyncSource(connection_id="s3:one", checkpoint=first.next_checkpoint)
    )

    assert first.objects[0].object_id == "alpha:docs/removed.txt"
    assert len(second.objects) == 1
    assert second.objects[0].kind is SourceObjectKind.DELETED
    assert second.objects[0].deleted
    assert second.objects[0].object_id == "alpha:docs/removed.txt"
    description = await attachment.inspect_source(InspectSource(connection_id="s3:one"))
    assert description.supports_deletes


async def test_fetch_closes_stream_after_a_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment, _ = _attachment(monkeypatch)
    body = _Body(b"hello")
    client = _S3Double()
    client.pages = [
        {"CommonPrefixes": [{"Prefix": "docs/"}], "IsTruncated": False},
    ]
    client.heads["docs/hello.txt"] = {
        "ETag": '"v1"',
        "ContentLength": 5,
        "ContentType": "text/plain",
        "Metadata": {"classification": "restricted"},
    }
    client.bodies["docs/hello.txt"] = body
    attachment._client = client
    await attachment.select_source_resources(
        SelectSourceResources(connection_id="s3:one", resource_ids=("alpha:docs/",))
    )

    content = await attachment.fetch_source(
        FetchSourceObject(
            connection_id="s3:one", object_id="alpha:docs/hello.txt", version="v1", max_bytes=5
        )
    )

    assert content.content == b"hello"
    assert content.metadata["classification"] == "restricted"
    assert body.read_sizes and max(body.read_sizes) <= 6
    assert body.closed


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "InvalidAccessKeyId", SourceFailureCode.AUTH_REQUIRED),
        (403, "AccessDenied", SourceFailureCode.AUTH_REQUIRED),
        (429, "SlowDown", SourceFailureCode.RATE_LIMITED),
        (503, "ServiceUnavailable", SourceFailureCode.TRANSIENT),
    ],
)
async def test_service_failures_are_typed_and_never_leak_vault_values(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str, expected: SourceFailureCode
) -> None:
    attachment, _ = _attachment(monkeypatch)

    class _FailingClient:
        def list_buckets(self) -> dict[str, Any]:
            raise _ClientError(status, code, retry_after="3")

    attachment._client = _FailingClient()
    attachment._client_errors = (_ClientError,)

    with pytest.raises(SourceError) as raised:
        await attachment.inspect_source(InspectSource(connection_id="s3:one"))

    assert raised.value.code is expected
    assert raised.value.retry_after == (3.0 if status == 429 else None)
    detail = str(raised.value)
    assert _ACCESS_KEY not in detail
    assert _SECRET_KEY not in detail
    assert _SESSION_TOKEN not in detail


async def test_close_releases_the_sdk_client_without_exposing_connection_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment, _ = _attachment(monkeypatch)
    client = _S3Double()
    attachment._client = client

    await attachment.close_source()

    assert client.closed
