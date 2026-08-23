"""Build connector attachments and place only explicitly declared credentials."""

from __future__ import annotations

import contextlib
import importlib
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import ExtensionAttachment
from arcagent.extension.cli_attachment import CliAttachment, CliCommand, CliResilience
from arcagent.extension.launcher import ProcessDefinition, ProcessLauncher, sandbox_policy_for
from arcagent.extension.manifest import ExtensionManifest
from arcagent.extension.mcp_policy import McpResilience, McpToolPolicy
from arcagent.extension.native_attachment import NativeAttachment
from arcagent.extension.secrets import Secret
from arcagent.extension.source import SourceAdapter
from arcagent.modules.connectors.credential_placement import (
    placement_environment,
    unplaced_secrets,
    visible_values,
)


def _refuse(message: str, **details: Any) -> ExtensionError:
    return ExtensionError(
        code="CONNECTOR_INSTALL_FAILED",
        message=f"probe: {message}",
        details={"step": "probe", **details},
    )


@contextlib.contextmanager
def _importable(bundle: Path) -> Iterator[None]:
    """Expose a bundle for exactly one native entrypoint resolution."""
    entry = str(bundle)
    sys.path.insert(0, entry)
    try:
        yield
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(entry)


class _NativeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entrypoint: str


class _CliConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    binary: str
    commands: list[CliCommand] = Field(default_factory=list)
    probe_argv: list[str] = Field(default_factory=lambda: ["--version"])
    install_instruction: str = ""
    resilience: CliResilience = Field(default_factory=CliResilience)


class _McpConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    transport: Literal["stdio"]
    argv: list[str] = Field(min_length=1)
    client_name: str = "arc"
    install_instruction: str = ""
    resilience: McpResilience = Field(default_factory=McpResilience)
    tools: dict[str, McpToolPolicy] = Field(default_factory=dict)


class _SourceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entrypoint: str


class _SourceEnabledAttachment:
    """Preserve an attachment's interactive hook while exposing its source adapter."""

    def __init__(self, delegate: ExtensionAttachment, source: SourceAdapter) -> None:
        self._delegate = delegate
        self._source = source

    def requirements(self) -> Any:
        return self._delegate.requirements()

    async def probe(self) -> Any:
        return await self._delegate.probe()

    async def describe_tools(self) -> Any:
        return await self._delegate.describe_tools()

    async def invoke(self, tool: str, args: dict[str, Any]) -> Any:
        return await self._delegate.invoke(tool, args)

    def source_adapter(self) -> SourceAdapter:
        return self._source


class _MultiSourceEnabledAttachment(_SourceEnabledAttachment):
    """One grant may expose isolated source streams such as Outlook and OneDrive."""

    def __init__(self, delegate: ExtensionAttachment, sources: dict[str, SourceAdapter]) -> None:
        first = next(iter(sources.values()))
        super().__init__(delegate, first)
        self._sources = dict(sources)

    def source_adapters(self) -> dict[str, SourceAdapter]:
        return dict(self._sources)


def build_attachment(
    manifest: ExtensionManifest, bundle: Path, secrets: Mapping[str, Secret]
) -> ExtensionAttachment:
    """Build the declared attachment at the sole credential-reveal boundary."""
    kind = manifest.extension.attachment
    if kind == "native":
        entrypoint = _NativeConfig.model_validate(manifest.config.get("native", {})).entrypoint
        context: dict[str, Any] = {"bundle": str(bundle)}
        context.update({name: secret.reveal() for name, secret in secrets.items()})
        with _importable(bundle):
            return NativeAttachment(entrypoint, context)
    if kind == "cli":
        unplaced = unplaced_secrets(manifest)
        if unplaced:
            raise _refuse(
                f"{manifest.extension.name} declares credential(s) {', '.join(unplaced)} "
                "with no [secrets.placement], and attaches as 'cli', which can only "
                "deliver a credential the bundle names a destination for",
                extension=manifest.extension.name,
                attachment=kind,
                unplaced=unplaced,
            )
        declared = _CliConfig.model_validate(manifest.config.get("cli", {}))
        attachment = CliAttachment(
            binary=declared.binary,
            commands=declared.commands,
            probe_argv=declared.probe_argv,
            install_instruction=declared.install_instruction,
            resilience=declared.resilience,
            env=placement_environment(manifest, secrets),
            values=visible_values(manifest, secrets),
        )
        return _with_source_adapter(manifest, bundle, attachment)
    if kind == "mcp":
        from arcagent.extension.mcp_attachment import McpAttachment, StdioTransport

        unplaced = unplaced_secrets(manifest)
        if unplaced:
            raise _refuse(
                f"{manifest.extension.name} declares credential(s) {', '.join(unplaced)} "
                "with no [secrets.placement], and attaches as 'mcp', which can only "
                "deliver a credential the bundle names a destination for",
                extension=manifest.extension.name,
                attachment=kind,
                unplaced=unplaced,
            )
        mcp_config = _McpConfig.model_validate(manifest.config.get("mcp", {}))
        launcher = ProcessLauncher(policy=sandbox_policy_for(manifest.extension.tier_floor))
        transport = StdioTransport(
            launcher=launcher,
            definition=ProcessDefinition(
                key=manifest.extension.name,
                argv=mcp_config.argv,
                env={
                    name: secret.reveal()
                    for name, secret in placement_environment(manifest, secrets).items()
                },
            ),
            install_instruction=mcp_config.install_instruction,
        )
        attachment = McpAttachment(
            transport,
            tools=mcp_config.tools,
            resilience=mcp_config.resilience,
            client_name=mcp_config.client_name,
        )
        return _with_source_adapter(manifest, bundle, attachment)
    raise _refuse(f"unknown attachment kind {kind!r}", attachment=kind)


def _with_source_adapter(
    manifest: ExtensionManifest, bundle: Path, attachment: ExtensionAttachment
) -> ExtensionAttachment:
    source_config = manifest.config.get("source")
    if source_config is None:
        return attachment
    entrypoint = _SourceConfig.model_validate(source_config).entrypoint
    with _importable(bundle):
        module = importlib.import_module(entrypoint)
        factories = getattr(module, "build_source_adapters", None)
        if callable(factories):
            sources = factories({"attachment": attachment})
            if (
                not isinstance(sources, dict)
                or not sources
                or not all(isinstance(adapter, SourceAdapter) for adapter in sources.values())
            ):
                raise _refuse(
                    "source entrypoint did not return SourceAdapters", entrypoint=entrypoint
                )
            return _MultiSourceEnabledAttachment(attachment, sources)
        factory = getattr(module, "build_source_adapter", None)
        source = factory({"attachment": attachment}) if callable(factory) else None
    if not isinstance(source, SourceAdapter):
        raise _refuse("source entrypoint did not return a SourceAdapter", entrypoint=entrypoint)
    return _SourceEnabledAttachment(attachment, source)


__all__ = ["build_attachment", "placement_environment", "visible_values"]
