"""Immutable connector planning and operation result contracts."""

from dataclasses import dataclass
from pathlib import Path

from arcagent.core.tier import Tier
from arcagent.extension.host import HostVerdict
from arcagent.extension.manifest import ExtensionManifest, SecretRequirement


@dataclass(frozen=True)
class ConnectorPlan:
    """Resolved, non-mutating connector installation plan."""

    instance: str
    extension: str
    bundle: Path
    manifest: ExtensionManifest
    unsatisfied_host: tuple[HostVerdict, ...]
    secrets: tuple[SecretRequirement, ...]
    approval_mode: str
    tier: Tier
    extensions_root: tuple[Path, ...]
    egress_allow: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallReport:
    """Resources produced by a completed installation."""

    instance: str
    extension: str
    tools: tuple[str, ...]
    detail: str = ""


@dataclass(frozen=True)
class RemovalReport:
    """Resources actually removed by a connector removal."""

    instance: str
    removed_secrets: tuple[str, ...] = ()
    removed_config: bool = False
    removed_state: bool = False


__all__ = ["ConnectorPlan", "InstallReport", "RemovalReport"]
