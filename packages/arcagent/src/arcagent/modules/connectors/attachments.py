"""Build connector attachments and place only explicitly declared credentials."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import ExtensionAttachment
from arcagent.extension.cli_attachment import CliAttachment, CliCommand, CliResilience
from arcagent.extension.manifest import ExtensionManifest
from arcagent.extension.native_attachment import NativeAttachment
from arcagent.extension.secrets import Secret
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
        return CliAttachment(
            binary=declared.binary,
            commands=declared.commands,
            probe_argv=declared.probe_argv,
            install_instruction=declared.install_instruction,
            resilience=declared.resilience,
            env=placement_environment(manifest, secrets),
            values=visible_values(manifest, secrets),
        )
    raise _refuse(f"unknown attachment kind {kind!r}", attachment=kind)


__all__ = ["build_attachment", "placement_environment", "visible_values"]
