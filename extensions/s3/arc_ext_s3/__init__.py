"""Optional S3/MinIO source adapter with bounded, credential-isolated reads."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import mimetypes
from typing import Any

from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    RequirementKind,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceContent,
    SourceDescription,
    SourceError,
    SourceFailureCode,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)

_MAX_READ = 20_000_000


class S3Attachment:
    """S3-compatible blob adapter; all blocking SDK work leaves the event loop."""

    def __init__(
        self,
        *,
        access_key_id: str,
        secret_access_key: str,
        session_token: str,
        region: str,
        endpoint_url: str,
    ) -> None:
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._session_token = session_token
        self._region = region
        self._endpoint_url = endpoint_url or None
        self._client: Any | None = None
        self._auth_errors: tuple[type[BaseException], ...] = ()
        self._client_errors: tuple[type[BaseException], ...] = ()
        self._transient_errors: tuple[type[BaseException], ...] = ()
        self._selected: tuple[str, str] | None = None
        self._inventories: dict[str, dict[str, SourceObject]] = {}
        self._observed: dict[str, dict[str, SourceObject]] = {}

    def requirements(self) -> list[Requirement]:
        return [
            Requirement(
                RequirementKind.CREDENTIAL, "access_key_id", instruction="Read-only S3 access key"
            ),
            Requirement(
                RequirementKind.CREDENTIAL,
                "secret_access_key",
                instruction="Read-only S3 secret key",
            ),
            Requirement(
                RequirementKind.CREDENTIAL,
                "session_token",
                instruction="Optional STS role-session token",
            ),
            Requirement(RequirementKind.CREDENTIAL, "region", instruction="S3 bucket region"),
            Requirement(
                RequirementKind.CREDENTIAL, "endpoint_url", instruction="S3 or MinIO endpoint URL"
            ),
        ]

    async def probe(self) -> ProbeResult:
        """Use ListBuckets as a bounded credential and endpoint check."""
        try:
            await self._call("list_buckets")
        except SourceError as exc:
            return ProbeResult(reachable=False, detail=exc.detail)
        return ProbeResult(reachable=True, tools=await self.describe_tools(), detail="reached S3")

    async def describe_tools(self) -> list[ToolSpec]:
        string = {"type": "string"}
        return [
            ToolSpec(
                name="s3_list",
                description="List objects under an approved bucket/prefix.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            ),
            ToolSpec(
                name="s3_read",
                description="Read one bounded object by its listed object id.",
                input_schema={
                    "type": "object",
                    "properties": {"object_id": string},
                    "required": ["object_id"],
                    "additionalProperties": False,
                },
                classification="read_only",
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        try:
            if tool == "s3_list":
                page = await self.sync_source(
                    SyncSource(connection_id="interactive", page_size=100)
                )
                return ToolResult(
                    tool=tool, content="\n".join(item.locator for item in page.objects)
                )
            if tool == "s3_read":
                object_id = args.get("object_id")
                if not isinstance(object_id, str) or not object_id:
                    raise ValueError("object_id is required")
                item = await self._head(object_id)
                content = await self.fetch_source(
                    FetchSourceObject(
                        connection_id="interactive",
                        object_id=object_id,
                        version=item.version or "",
                        max_bytes=_MAX_READ,
                    )
                )
                return ToolResult(
                    tool=tool, content=content.content.decode("utf-8", errors="replace")
                )
            return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content="unknown S3 tool")
        except (SourceError, ValueError) as exc:
            return ToolResult(tool=tool, outcome=ToolOutcome.ERROR, content=str(exc))

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        """Return an opaque account identity, not endpoint or credential material."""
        buckets = await self._call("list_buckets")
        names = ",".join(sorted(str(item.get("Name", "")) for item in buckets.get("Buckets", [])))
        account_id = hashlib.sha256(
            (self._endpoint_url or "aws").encode() + names.encode()
        ).hexdigest()[:24]
        return SourceDescription(
            connection_id=request.connection_id,
            source_kind="s3",
            account_id=account_id,
            display_name="S3-compatible storage",
            supports_incremental=True,
            supports_deletes=True,
            root_locator=self._selection_locator(),
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        del request
        response = await self._call("list_buckets")
        selected = self._selection_locator()
        resources: list[SourceResource] = []
        for bucket in response.get("Buckets", []):
            if not isinstance(bucket, dict) or not isinstance(bucket.get("Name"), str):
                continue
            name = str(bucket["Name"])
            resources.append(
                SourceResource(
                    resource_id=name,
                    label=name,
                    resource_kind="bucket",
                    locator=name,
                    selected=name == selected,
                )
            )
            resources.extend(await self._prefix_resources(name, selected))
        return tuple(resources)

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        if len(request.resource_ids) != 1:
            raise SourceError(
                SourceFailureCode.UNSUPPORTED_CONTENT, "select exactly one S3 bucket"
            )
        resource_id = request.resource_ids[0]
        bucket, _, prefix = resource_id.partition(":")
        available = {
            item.resource_id
            for item in await self.list_source_resources(
                ListSourceResources(connection_id=request.connection_id)
            )
        }
        if resource_id not in available:
            raise SourceError(SourceFailureCode.NOT_FOUND, "selected S3 bucket is unavailable")
        self._selected = (bucket, prefix)

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        """Page a selected bucket prefix with continuation tokens and stable ETags."""
        bucket, prefix = self._selection()
        selection = self._selection_locator()
        if not request.checkpoint:
            self._observed[selection] = {}
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": request.page_size}
        if request.checkpoint:
            kwargs["ContinuationToken"] = request.checkpoint
        response = await self._call("list_objects_v2", **kwargs)
        objects = tuple(
            _source_object(bucket, item)
            for item in response.get("Contents", [])
            if isinstance(item, dict)
        )
        observed = self._observed.setdefault(selection, {})
        observed.update({item.object_id: item for item in objects})
        has_more = bool(response.get("IsTruncated", False))
        if not has_more:
            previous = self._inventories.get(selection, {})
            removed = tuple(
                SourceObject(
                    object_id=object_id,
                    locator=item.locator,
                    kind=SourceObjectKind.DELETED,
                    version=item.version,
                    deleted=True,
                    metadata={"classification": "unclassified"},
                )
                for object_id, item in previous.items()
                if object_id not in observed
            )
            self._inventories[selection] = dict(observed)
            self._observed.pop(selection, None)
            objects = objects + removed
        return SyncSourcePage(
            objects=objects,
            next_checkpoint=str(response.get("NextContinuationToken") or ""),
            has_more=has_more,
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        """Fetch a version-checked bounded object from the selected bucket/prefix."""
        bucket, prefix = self._selection()
        object_bucket, key = _split_object_id(request.object_id)
        if object_bucket != bucket or not key.startswith(prefix):
            raise SourceError(
                SourceFailureCode.NOT_FOUND, "S3 object is outside the selected resource"
            )
        head = await self._call("head_object", Bucket=bucket, Key=key)
        version = _version(head)
        if version != request.version:
            raise SourceError(SourceFailureCode.VERSION_CHANGED, "S3 object changed before fetch")
        size = int(head.get("ContentLength", 0))
        if size > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "S3 object exceeds fetch limit")
        response = await self._call(
            "get_object", Bucket=bucket, Key=key, Range=f"bytes=0-{max(size - 1, 0)}"
        )
        body = response["Body"]
        try:
            content = await asyncio.to_thread(body.read, request.max_bytes + 1)
        finally:
            await asyncio.to_thread(body.close)
        if len(content) > request.max_bytes:
            raise SourceError(SourceFailureCode.TOO_LARGE, "S3 object exceeds fetch limit")
        return SourceContent(
            object_id=request.object_id,
            version=version,
            media_type=str(
                response.get("ContentType")
                or mimetypes.guess_type(key)[0]
                or "application/octet-stream"
            ),
            content=content,
            content_hash=str(head.get("ETag") or "").strip('"') or None,
            metadata={
                "bucket": bucket,
                "key": key,
                "etag": str(head.get("ETag") or "").strip('"'),
                "classification": str(
                    head.get("Metadata", {}).get("classification") or "unclassified"
                ),
            },
        )

    async def close_source(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await asyncio.to_thread(client.close)

    async def _head(self, object_id: str) -> SourceObject:
        bucket, key = _split_object_id(object_id)
        head = await self._call("head_object", Bucket=bucket, Key=key)
        return SourceObject(
            object_id=object_id,
            locator=f"s3://{bucket}/{key}",
            kind=SourceObjectKind.FILE,
            version=_version(head),
        )

    async def _call(self, method: str, **kwargs: Any) -> Any:
        try:
            client = self._client or await self._open_client()
            operation = getattr(client, method)
            return await asyncio.to_thread(operation, **kwargs)
        except (OSError, ConnectionError, TimeoutError) as exc:
            raise SourceError(SourceFailureCode.TRANSIENT, "S3 connection failed") from exc
        except self._auth_errors as exc:
            raise SourceError(SourceFailureCode.AUTH_REQUIRED, "S3 authorization failed") from exc
        except self._transient_errors as exc:
            raise SourceError(SourceFailureCode.TRANSIENT, "S3 request failed") from exc
        except self._client_errors as exc:
            metadata = exc.response.get("ResponseMetadata", {})
            code = str(metadata.get("HTTPStatusCode", ""))
            retry_after = _retry_after(metadata) if code == "429" else None
            failure = (
                SourceFailureCode.AUTH_REQUIRED
                if code in {"401", "403"}
                else SourceFailureCode.RATE_LIMITED
                if code == "429"
                else SourceFailureCode.TRANSIENT
                if code.startswith("5")
                else SourceFailureCode.NOT_FOUND
            )
            raise SourceError(
                failure, f"S3 request refused: {code or 'unknown'}", retry_after=retry_after
            ) from exc

    async def _open_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            boto3 = importlib.import_module("boto3")
            errors = importlib.import_module("botocore.exceptions")
        except ImportError as exc:
            raise SourceError(
                SourceFailureCode.UNSUPPORTED_CONTENT, "S3 support requires boto3"
            ) from exc
        self._client_errors = (errors.ClientError,)
        self._auth_errors = (errors.NoCredentialsError, errors.PartialCredentialsError)
        self._transient_errors = (
            errors.BotoCoreError,
            errors.EndpointConnectionError,
            errors.ReadTimeoutError,
        )
        self._client = await asyncio.to_thread(
            boto3.client,
            "s3",
            aws_access_key_id=self._access_key_id,
            aws_secret_access_key=self._secret_access_key,
            aws_session_token=self._session_token or None,
            region_name=self._region,
            endpoint_url=self._endpoint_url,
        )
        return self._client

    async def _prefix_resources(self, bucket: str, selected: str) -> list[SourceResource]:
        """Discover first-level prefixes so a selection can be narrower than a bucket."""
        response = await self._call("list_objects_v2", Bucket=bucket, Delimiter="/", MaxKeys=1_000)
        resources: list[SourceResource] = []
        for item in response.get("CommonPrefixes", []):
            if not isinstance(item, dict) or not isinstance(item.get("Prefix"), str):
                continue
            resource_id = f"{bucket}:{item['Prefix']}"
            resources.append(
                SourceResource(
                    resource_id=resource_id,
                    label=resource_id,
                    resource_kind="prefix",
                    locator=resource_id,
                    selected=resource_id == selected,
                )
            )
        return resources

    def _selection(self) -> tuple[str, str]:
        if self._selected is None:
            raise SourceError(
                SourceFailureCode.UNSUPPORTED_CONTENT, "select an S3 bucket before synchronization"
            )
        return self._selected

    def _selection_locator(self) -> str:
        return "" if self._selected is None else ":".join(self._selected)


def build_native_attachment(context: dict[str, Any]) -> S3Attachment:
    return S3Attachment(
        access_key_id=str(context.get("access_key_id", "")),
        secret_access_key=str(context.get("secret_access_key", "")),
        session_token=str(context.get("session_token", "")),
        region=str(context.get("region", "")),
        endpoint_url=str(context.get("endpoint_url", "")),
    )


def _split_object_id(value: str) -> tuple[str, str]:
    bucket, separator, key = value.partition(":")
    if not separator or not bucket or not key:
        raise SourceError(SourceFailureCode.NOT_FOUND, "invalid S3 object identifier")
    return bucket, key


def _source_object(bucket: str, item: dict[str, Any]) -> SourceObject:
    key = str(item.get("Key") or "")
    if not key:
        raise SourceError(SourceFailureCode.TRANSIENT, "S3 returned an object without a key")
    version = _version(item)
    return SourceObject(
        object_id=f"{bucket}:{key}",
        locator=f"s3://{bucket}/{key}",
        kind=SourceObjectKind.FILE,
        version=version,
        content_hash=str(item.get("ETag") or "").strip('"') or None,
        size=int(item.get("Size", 0)),
        modified_at=item.get("LastModified").isoformat()
        if item.get("LastModified") is not None
        else None,
        media_type=str(
            item.get("ContentType") or mimetypes.guess_type(key)[0] or "application/octet-stream"
        ),
        metadata={
            "bucket": bucket,
            "key": key,
            "etag": version,
            "classification": str(
                item.get("Metadata", {}).get("classification") or "unclassified"
            ),
        },
    )


def _version(item: dict[str, Any]) -> str:
    return str(item.get("VersionId") or item.get("ETag") or "").strip('"')


def _retry_after(metadata: dict[str, Any]) -> float | None:
    headers = metadata.get("HTTPHeaders", {})
    value = headers.get("retry-after") if isinstance(headers, dict) else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["S3Attachment", "build_native_attachment"]
