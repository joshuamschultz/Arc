"""Connector audit-chain ownership and read-only extension catalog views."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from arctrust.audit import AuditEvent, AuditSink, NullSink
from pydantic import ValidationError

from arcagent.core.errors import ExtensionError
from arcagent.core.tier import Tier
from arcagent.extension.catalog import (
    MANIFEST_NAME,
    ExtensionCatalog,
    ExtensionResolution,
)
from arcagent.extension.manifest import (
    DeclaredTool,
    HostRequirement,
    SecretRequirement,
    load_manifest,
)


class ClosableSink(Protocol):
    """Audit sink whose lifetime can be handed to an operation."""

    def write(self, event: AuditEvent) -> None: ...

    def close(self) -> None: ...


class AuditChain:
    """Define whether connector operations hold or open their audit sink."""

    def __init__(
        self,
        *,
        sink: AuditSink | None = None,
        opener: Callable[[], ClosableSink] | None = None,
    ) -> None:
        self._sink = sink
        self._opener = opener

    @classmethod
    def held(cls, sink: AuditSink) -> AuditChain:
        return cls(sink=sink)

    @classmethod
    def opened_by(cls, opener: Callable[[], ClosableSink]) -> AuditChain:
        return cls(opener=opener)

    @contextlib.contextmanager
    def open(self) -> Iterator[AuditSink]:
        if self._opener is None:
            yield self._sink if self._sink is not None else NullSink()
            return
        sink = self._opener()
        try:
            yield sink
        finally:
            sink.close()


@dataclass(frozen=True)
class CatalogEntry:
    """One readable or explicitly unreadable extension bundle."""

    name: str
    path: Path
    official: bool
    display_name: str = ""
    version: str = ""
    description: str = ""
    error: str = ""
    attachment: str = ""
    tier_floor: str = ""
    approval_default: str = ""
    secrets: tuple[SecretRequirement, ...] = ()
    host_requires: tuple[HostRequirement, ...] = ()
    tools: tuple[DeclaredTool, ...] = ()


def catalog(
    *,
    roots: Sequence[Path],
    tier: Tier = Tier.PERSONAL,
    audit_sink: AuditSink | None = None,
) -> tuple[CatalogEntry, ...]:
    """List every bundle without letting one unreadable bundle blank the view."""
    listing = ExtensionCatalog(
        roots=roots,
        tier=tier,
        audit_sink=audit_sink if audit_sink is not None else NullSink(),
    )
    return tuple(_catalog_entry(resolution, tier) for resolution in listing.available())


def _catalog_entry(resolution: ExtensionResolution, tier: Tier) -> CatalogEntry:
    entry = CatalogEntry(
        name=resolution.name,
        path=resolution.path,
        official=resolution.official,
        display_name=resolution.display_name or resolution.name,
        version=resolution.version,
        description=resolution.description,
        error=resolution.error,
    )
    if resolution.error:
        return entry
    try:
        manifest = load_manifest(
            (resolution.path / MANIFEST_NAME).read_text(encoding="utf-8"), tier=tier
        )
    except (OSError, ValueError, ValidationError, ExtensionError) as exc:
        return CatalogEntry(
            name=resolution.name,
            path=resolution.path,
            official=resolution.official,
            display_name=resolution.name,
            error=f"{type(exc).__name__}: {exc}",
        )
    header = manifest.extension
    return CatalogEntry(
        name=resolution.name,
        path=resolution.path,
        official=resolution.official,
        display_name=header.label,
        version=header.version,
        description=header.description,
        attachment=header.attachment,
        tier_floor=header.tier_floor.value,
        approval_default=manifest.approval.default,
        secrets=tuple(manifest.secrets),
        host_requires=tuple(manifest.host_requires),
        tools=tuple(manifest.tools.declared),
    )


__all__ = ["AuditChain", "CatalogEntry", "ClosableSink", "catalog"]
