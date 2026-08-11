"""Translate connector secret declarations into safe runtime placements."""

from __future__ import annotations

from collections.abc import Mapping

from arcagent.extension.manifest import ExtensionManifest
from arcagent.extension.secrets import Secret


def placement_environment(
    manifest: ExtensionManifest, secrets: Mapping[str, Secret]
) -> dict[str, Secret]:
    """Map declared environment placements to still-wrapped credentials."""
    return {
        declared.placement.variable: secrets[declared.name]
        for declared in manifest.secrets
        if declared.placement is not None and declared.name in secrets
    }


def visible_values(manifest: ExtensionManifest, secrets: Mapping[str, Secret]) -> dict[str, str]:
    """Reveal only fields explicitly declared non-sensitive for argv use."""
    return {
        declared.name: secrets[declared.name].reveal()
        for declared in manifest.secrets
        if not declared.sensitive and declared.name in secrets
    }


def unplaced_secrets(manifest: ExtensionManifest) -> list[str]:
    """Return declared CLI fields that have no safe delivery destination."""
    delivered = manifest.fields_named_by_commands()
    return [
        declared.name
        for declared in manifest.secrets
        if declared.placement is None and declared.name not in delivered
    ]


__all__ = ["placement_environment", "unplaced_secrets", "visible_values"]
