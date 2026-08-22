"""ArcAgent: Enterprise-grade autonomous agent nucleus."""

from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import arcrun

from arcagent.capabilities.artifact_signing import (
    load_signature,
    sidecar_path,
    verify_file,
    write_signature,
)
from arcagent.capabilities.capability_loader import CapabilityLoader
from arcagent.capabilities.capability_registry import CapabilityRegistry
from arcagent.capabilities.capability_signing import revoke as revoke_capability
from arcagent.capabilities.capability_signing import sign as sign_capability
from arcagent.capabilities.capability_signing import trust_bundled_capabilities
from arcagent.capabilities.inventory import (
    GatedItem,
    append_module_scan_roots,
    collect_agent_capability_inventory,
    global_capabilities_root,
    list_gated,
    pin_name_for,
    read_capability_source,
)
from arcagent.capabilities.skill_validator import validate_skill_folder
from arcagent.connections import (
    NOT_INSTALLED,
    AttachmentFactory,
    AuditChain,
    Authorization,
    CatalogEntry,
    ClosableSink,
    Connection,
    ConnectorControl,
    ConnectorMutation,
    ConnectorReconcileResult,
    Connections,
    ConnectionWorld,
    ConnectorPlan,
    ExtensionError,
    HostPrerequisiteDirector,
    HostVerdict,
    InstallReport,
    Tier,
    ToolSpec,
    catalog,
    deployment_tier,
    resolve_deployment,
    resolve_roots,
)
from arcagent.core.agent import ArcAgent
from arcagent.core.agent_security import operator_key_path
from arcagent.core.arcteam_bootstrap import make_backend
from arcagent.core.config import ArcAgentConfig, SecurityConfig, deep_merge, load_config
from arcagent.core.errors import (
    ArcAgentError,
    ConfigError,
    ContextError,
    IdentityError,
    ModuleBusError,
    ToolError,
    ToolVetoedError,
)
from arcagent.core.module_discovery import discover_modules, module_root
from arcagent.core.prompt_context import build_prompt_resolver
from arcagent.extension.inspect import inspect_extensions
from arcagent.keys import KeyStatus, KeyStore, default_env_file
from arcagent.knowledge import (
    KnowledgeAccess,
    KnowledgeDocument,
    KnowledgeDraft,
    KnowledgeHit,
    KnowledgeRef,
    PersonalKnowledgePort,
    PromotionSource,
    SharedKnowledgePort,
)
from arcagent.modules.capability_import.archive import intake as intake_capability_archive
from arcagent.modules.capability_import.errors import CapabilityImportError
from arcagent.modules.capability_import.manifest import manifest_dict
from arcagent.modules.capability_import.models import (
    CapabilityImportLimits,
    CapabilityImportManifest,
    CapabilityImportResult,
    CapabilityImportReview,
    CapabilityImportStatus,
)
from arcagent.modules.capability_import.service import CapabilityImportService
from arcagent.modules.scheduler.models import ScheduleEntry, ScheduleMetadata, generate_schedule_id
from arcagent.modules.scheduler.store import ScheduleStore
from arcagent.modules.session.identity_graph import IdentityGraph
from arcagent.orchestration.spawn import make_spawn_tool
from arcagent.orchestration.token_budget import RootTokenBudget
from arcagent.streaming import (
    DeliveryStreamEvent,
    DeliveryStreamSource,
    DeliveryTerminalEvent,
    DeliveryTextEvent,
    DeliveryToolEvent,
)
from arcagent.tiers import (
    SECURITY_CONFIG_KNOBS,
    audit_tier_relaxations,
    stricter_tier,
    tier_rank,
)
from arcagent.tools._decorator import tool
from arcagent.tools._dynamic_loader import resolve_workspace_import_policy
from arcagent.tools._secret_guard import find_secret
from arcagent.utils.toml_writer import dumps_toml


def stream_token_text(event: object) -> str | None:
    """Return token text for an ArcAgent stream event, else ``None``.

    Messaging surfaces consume this semantic adapter instead of depending on
    ArcRun's concrete stream-event classes.
    """
    return event.text if isinstance(event, arcrun.TokenEvent) else None


def model_config_path() -> Path:
    """Return the model configuration edited by ArcAgent-facing surfaces."""
    return arcrun.model_config_path()


def iter_model_modules(
    instance: Any,
) -> Iterator[tuple[arcrun.ModelModuleKind, Any]]:
    """Yield observable model modules for ArcAgent-facing status surfaces."""
    return arcrun.iter_model_modules(instance)


def set_workflow_runner(runner: Any) -> None:
    """Publish the gateway-hosted workflow runner to ArcAgent's workflow tool."""
    from arcagent.modules.workflows import _runtime

    _runtime.set_runner(runner)


def builtin_capabilities_path() -> Path:
    """Return the packaged built-in capability directory."""
    return Path(__file__).parent / "builtins" / "capabilities"


def modules_path() -> Path:
    """Return the packaged ArcAgent modules directory."""
    return Path(__file__).parent / "modules"


__all__ = [
    "NOT_INSTALLED",
    "SECURITY_CONFIG_KNOBS",
    "ArcAgent",
    "ArcAgentConfig",
    "ArcAgentError",
    "AttachmentFactory",
    "AuditChain",
    "Authorization",
    "CapabilityImportError",
    "CapabilityImportLimits",
    "CapabilityImportManifest",
    "CapabilityImportResult",
    "CapabilityImportReview",
    "CapabilityImportService",
    "CapabilityImportStatus",
    "CapabilityLoader",
    "CapabilityRegistry",
    "CatalogEntry",
    "ClosableSink",
    "ConfigError",
    "Connection",
    "ConnectorControl",
    "ConnectorMutation",
    "ConnectorReconcileResult",
    "ConnectionWorld",
    "Connections",
    "ConnectorPlan",
    "ContextError",
    "DeliveryStreamEvent",
    "DeliveryStreamSource",
    "DeliveryTerminalEvent",
    "DeliveryTextEvent",
    "DeliveryToolEvent",
    "ExtensionError",
    "GatedItem",
    "HostPrerequisiteDirector",
    "HostVerdict",
    "IdentityError",
    "IdentityGraph",
    "InstallReport",
    "KeyStatus",
    "KeyStore",
    "KnowledgeAccess",
    "KnowledgeDocument",
    "KnowledgeDraft",
    "KnowledgeHit",
    "KnowledgeRef",
    "ModuleBusError",
    "PersonalKnowledgePort",
    "PromotionSource",
    "RootTokenBudget",
    "ScheduleEntry",
    "ScheduleMetadata",
    "ScheduleStore",
    "SecurityConfig",
    "SharedKnowledgePort",
    "Tier",
    "ToolError",
    "ToolSpec",
    "ToolVetoedError",
    "append_module_scan_roots",
    "audit_tier_relaxations",
    "build_prompt_resolver",
    "builtin_capabilities_path",
    "catalog",
    "collect_agent_capability_inventory",
    "deep_merge",
    "default_env_file",
    "deployment_tier",
    "discover_modules",
    "dumps_toml",
    "find_secret",
    "generate_schedule_id",
    "global_capabilities_root",
    "inspect_extensions",
    "intake_capability_archive",
    "iter_model_modules",
    "list_gated",
    "load_config",
    "load_signature",
    "make_backend",
    "make_spawn_tool",
    "manifest_dict",
    "model_config_path",
    "module_root",
    "modules_path",
    "operator_key_path",
    "pin_name_for",
    "read_capability_source",
    "resolve_deployment",
    "resolve_roots",
    "resolve_workspace_import_policy",
    "revoke_capability",
    "set_workflow_runner",
    "sidecar_path",
    "sign_capability",
    "stream_token_text",
    "stricter_tier",
    "tier_rank",
    "tool",
    "trust_bundled_capabilities",
    "validate_skill_folder",
    "verify_file",
    "write_signature",
]

try:
    __version__ = version("arc-agent")
except PackageNotFoundError:  # reason: source checkout without an installed distribution
    __version__ = "0.17.0"
