"""SPEC-082 T-1075 (RED) — the connector-authoring contract, codified and enforced.

REQ-431 / COMP-012. The connector seam has an unwritten contract every ``SourceAdapter``
must honour so the 11th connector inherits the reliability fixes instead of re-earning
the bugs:

1. **Monotonic revision.** Each synced object carries a strictly non-decreasing
   ``metadata["revision"]``; ArcMemory refuses an update whose revision is not greater
   than the stored one, so a non-monotonic source silently stops re-indexing (this is
   exactly why ``arc_ext_dropbox`` synthesises one — see its ``_modified_revision``).
2. **Typed failures.** A refusal surfaces as a ``SourceError(SourceFailureCode, ...)``,
   never a bare ``Exception`` the orchestrator cannot classify or back off from.
3. **Lazy client re-creation after ``close_source``.** A call after ``close_source``
   still works — the adapter rebuilds its client rather than holding a dead handle.

This test drives a would-be checker, ``arcagent.extension.authoring.assert_connector_contract``,
which does not exist yet. The RED is the import: ``No module named
'arcagent.extension.authoring'``. It goes GREEN when T-1076 adds the module and the
shared assert helpers.

Checker contract encoded here (finalised by T-1076): ``assert_connector_contract`` is an
async helper that drives a ``SourceAdapter`` through its lifecycle against a fresh
(un-credentialed) connection and raises ``AssertionError`` naming the clause a connector
violates — a bare-exception failure, a non-monotonic revision, or a dead client after
close. A connector that fails only via typed ``SourceError`` is contract-conformant:
the clause governs *how* a failure surfaces, not that every call succeeds. That is why
a real bundle (``extensions/jira``, which raises only typed ``SourceError``) passes even
with no live backend, while the broken fakes below are flagged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.core.tier import Tier
from arcagent.extension.authoring import assert_connector_contract
from arcagent.extension.manifest import load_manifest
from arcagent.extension.source import (
    FetchSourceObject,
    InspectSource,
    ListSourceResources,
    SelectSourceResources,
    SourceAdapter,
    SourceContent,
    SourceDescription,
    SourceObject,
    SourceObjectKind,
    SourceResource,
    SyncSource,
    SyncSourcePage,
)
from arcagent.modules.connectors.attachments import build_attachment

_CONNECTION = "test-conn"
_EXTENSIONS_ROOT = Path(__file__).resolve().parents[4] / "extensions"


class _ConformingAdapter:
    """A minimal in-memory ``SourceAdapter`` that honours every contract clause.

    Revision is strictly increasing across pages; failures (there are none on the
    happy path) would be typed; and no state is held across ``close_source`` that a
    later call cannot rebuild.
    """

    def __init__(self) -> None:
        self._client: object | None = object()
        self._revision = 0

    async def inspect_source(self, request: InspectSource) -> SourceDescription:
        return SourceDescription(
            connection_id=request.connection_id, source_kind="fake", account_id="acct-1"
        )

    async def list_source_resources(
        self, request: ListSourceResources
    ) -> tuple[SourceResource, ...]:
        return (SourceResource(resource_id="root", label="Root", resource_kind="folder"),)

    async def select_source_resources(self, request: SelectSourceResources) -> None:
        return None

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if self._client is None:  # lazily rebuilt after close_source
            self._client = object()
        self._revision += 1
        obj = SourceObject(
            object_id="obj-1",
            locator="/obj-1",
            kind=SourceObjectKind.FILE,
            metadata={"revision": self._revision},
        )
        return SyncSourcePage(
            objects=(obj,), next_checkpoint=str(self._revision), has_more=False
        )

    async def fetch_source(self, request: FetchSourceObject) -> SourceContent:
        return SourceContent(
            object_id=request.object_id,
            version=request.version,
            media_type="text/plain",
            content=b"payload",
        )

    async def close_source(self) -> None:
        self._client = None


class _UntypedFailureAdapter(_ConformingAdapter):
    """Violates clause 2: a refusal escapes as a bare ``RuntimeError``, not a ``SourceError``."""

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        raise RuntimeError("the backend said no")


class _NonMonotonicRevisionAdapter(_ConformingAdapter):
    """Violates clause 1: successive objects carry a decreasing revision."""

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        objects = tuple(
            SourceObject(
                object_id=f"obj-{revision}",
                locator=f"/obj-{revision}",
                kind=SourceObjectKind.FILE,
                metadata={"revision": revision},
            )
            for revision in (5, 3, 1)  # strictly decreasing — ArcMemory would drop 3 and 1
        )
        return SyncSourcePage(objects=objects, next_checkpoint="5", has_more=False)


class _DeadClientAfterCloseAdapter(_ConformingAdapter):
    """Violates clause 3: once closed, the client is never rebuilt and calls fail."""

    async def sync_source(self, request: SyncSource) -> SyncSourcePage:
        if self._client is None:
            raise RuntimeError("client used after close_source; nothing rebuilt it")
        return await super().sync_source(request)


def _bundle_source_adapter(name: str) -> SourceAdapter:
    """Build ``extensions/<name>``'s attachment and return its source adapter, offline."""
    bundle = _EXTENSIONS_ROOT / name
    manifest = load_manifest((bundle / "extension.toml").read_text(encoding="utf-8"), tier=Tier.PERSONAL)
    attachment: Any = build_attachment(manifest, bundle, {})
    return attachment.source_adapter()


async def test_flags_a_connector_whose_failures_are_not_typed() -> None:
    """A bare ``RuntimeError`` from a sync is a contract violation the checker names."""
    with pytest.raises(AssertionError):
        await assert_connector_contract(_UntypedFailureAdapter(), connection_id=_CONNECTION)


async def test_flags_a_connector_with_a_non_monotonic_revision() -> None:
    """A decreasing revision stops re-indexing silently; the checker refuses it."""
    with pytest.raises(AssertionError):
        await assert_connector_contract(_NonMonotonicRevisionAdapter(), connection_id=_CONNECTION)


async def test_flags_a_connector_that_does_not_rebuild_its_client_after_close() -> None:
    """A dead client after ``close_source`` breaks the next run; the checker catches it."""
    with pytest.raises(AssertionError):
        await assert_connector_contract(_DeadClientAfterCloseAdapter(), connection_id=_CONNECTION)


async def test_passes_a_conforming_in_memory_connector() -> None:
    """A connector honouring all three clauses passes without raising."""
    await assert_connector_contract(_ConformingAdapter(), connection_id=_CONNECTION)


async def test_passes_a_real_conformant_bundle() -> None:
    """A shipped bundle that raises only typed ``SourceError`` (jira) is conformant.

    Proves the checker is runnable offline against a real ``extensions/*`` bundle — the
    parametrized-over-every-bundle use COMP-012 calls for — not only against test fakes.
    """
    adapter = _bundle_source_adapter("jira")
    assert isinstance(adapter, SourceAdapter)
    await assert_connector_contract(adapter, connection_id=_CONNECTION)
