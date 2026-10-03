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
    ExtensionManifest,
    HostRequirement,
    OAuthFlow,
    SecretRequirement,
    load_manifest,
)
from arcagent.extension.platforms import host_platform


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
    knowledge_mode: str = ""
    knowledge_reason: str = ""
    error: str = ""
    attachment: str = ""
    tier_floor: str = ""
    approval_default: str = ""
    secrets: tuple[SecretRequirement, ...] = ()
    host_requires: tuple[HostRequirement, ...] = ()
    tools: tuple[DeclaredTool, ...] = ()
    #: True when the manifest declares an ``[oauth]`` flow — read, not inferred.
    oauth: bool = False
    #: The deployment app slot an ``[oauth]`` flow uses ("" when there is none).
    oauth_provider: str = ""
    #: Where the operator creates that provider's OAuth app ("" when undeclared).
    oauth_console_url: str = ""
    #: The whole ``[oauth]`` flow, for surfaces that set its app slot up (tenant, clouds).
    oauth_flow: OAuthFlow | None = None
    #: True when the bundle pins a single binary Arc can place on THIS host. False for a
    #: bundle with no pin, a pin for another platform, or a tarball with no ``member`` (an
    #: npm package): an install button for those can never succeed, so a surface hides it.
    auto_installable: bool = False


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
        knowledge_mode=resolution.knowledge_mode,
        knowledge_reason=resolution.knowledge_reason,
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
        knowledge_mode=manifest.knowledge.mode,
        knowledge_reason=manifest.knowledge.reason,
        attachment=header.attachment,
        tier_floor=header.tier_floor.value,
        approval_default=manifest.approval.default,
        secrets=tuple(manifest.secrets),
        host_requires=tuple(manifest.host_requires),
        tools=tuple(manifest.tools.declared),
        oauth=manifest.oauth is not None,
        oauth_provider=manifest.oauth.provider if manifest.oauth is not None else "",
        oauth_console_url=(manifest.oauth.console_url or "") if manifest.oauth is not None else "",
        oauth_flow=manifest.oauth,
        auto_installable=_places_a_binary_here(manifest),
    )


def _places_a_binary_here(manifest: ExtensionManifest) -> bool:
    """True when ``[artifact]`` names an executable member for this host's platform."""
    if manifest.artifact is None:
        return False
    build = manifest.artifact.for_host(host_platform())
    return build is not None and bool(build.member)


__all__ = ["AuditChain", "CatalogEntry", "ClosableSink", "catalog"]
