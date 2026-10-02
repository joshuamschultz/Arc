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
    write_signature_with_signer,
)
from arcagent.capabilities.capability_loader import CapabilityLoader, SkillArtifactResolver
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
    Connections,
    ConnectionWorld,
    ConnectorControl,
    ConnectorMutation,
    ConnectorPlan,
    ConnectorReconcileResult,
    ExtensionError,
    HostPrerequisiteDirector,
    HostVerdict,
    InstallReport,
    RemoteLoginLedger,
    RemoteLoginStart,
    Tier,
    ToolSpec,
    catalog,
    deployment_tier,
    resolve_deployment,
    resolve_roots,
)
from arcagent.core.agent import MEMORY_PROMOTION_MAX_ITEMS, ArcAgent
from arcagent.core.agent_security import operator_key_path
from arcagent.core.config import ArcAgentConfig, SecurityConfig, deep_merge, load_config
from arcagent.core.errors import (
    ArcAgentError,
    CapabilityUnavailableError,
    ConfigError,
    ContextError,
    IdentityError,
    ModuleBusError,
    ToolError,
    ToolVetoedError,
)
from arcagent.core.module_config import validate_module_configs
from arcagent.core.module_discovery import discover_modules, module_root
from arcagent.core.prompt_context import build_prompt_resolver, signed_workspace_files
from arcagent.core.tool_policy import (
    ToolPolicyState,
    ToolPolicySummary,
    summarize_tool_policy,
)
from arcagent.extension import ProbeResult, ToolOutcome, ToolResult
from arcagent.extension.inspect import inspect_extensions
from arcagent.keys import KeyStatus, KeyStore, classifier_models, default_env_file
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
from arcagent.modules.capability_import.pack_signing import (
    SkillPackError,
    revoke_skill_folder,
    sign_skill_folder,
)
from arcagent.modules.capability_import.service import CapabilityImportService
from arcagent.modules.connected_data import SourceRefusedError, SourceUnreachableError
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
from arcagent.utils import config_render
from arcagent.utils.toml_writer import dumps_toml

CallJob = arcrun.CallJob
CallQueueCoordinator = arcrun.CallQueueCoordinator
QueueCancellation = arcrun.QueueCancellation
QueueControlSnapshot = arcrun.QueueControlSnapshot
QueueLimits = arcrun.QueueLimits
QueueMetadataPage = arcrun.QueueMetadataPage
QueueReadScope = arcrun.QueueReadScope


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


def build_mcp_door(agent: Any) -> Any:
    """Assemble a serving MCP door from a started agent (SPEC-082 T-1112).

    The ``mcp_server`` module is imported lazily so ``import arcagent`` never pulls
    the optional, default-off door in, and the module stays folder-removable.
    Returns a ``BuiltDoor`` (``.server`` — the ``mcp`` SDK server stdio drives — and
    ``.http_app``); raises ``ValueError`` when ``[modules.mcp_server]`` is disabled.
    """
    from arcagent.modules.mcp_server.serving import build_door_from_started_agent

    return build_door_from_started_agent(agent)


async def serve_mcp_stdio(server: Any) -> None:
    """Serve an MCP door's SDK server over stdio (SPEC-082 T-1111 / SPEC-084 T-1146).

    Drives the door's ``mcp`` SDK :class:`~mcp.server.lowlevel.Server` over the
    process's stdin/stdout with the real MCP handshake, until EOF.
    """
    from arcagent.modules.mcp_server.stdio_transport import serve_stdio

    await serve_stdio(server)


def builtin_capabilities_path() -> Path:
    """Return the packaged built-in capability directory."""
    return Path(__file__).parent / "builtins" / "capabilities"


def modules_path() -> Path:
    """Return the packaged ArcAgent modules directory."""
    return Path(__file__).parent / "modules"


def __getattr__(name: str) -> Any:
    """Load optional ArcAgent exports only when requested."""
    if name in {
        "LedgerRunOwner",
        "RunIntentLedger",
        "RunIntentUnavailableError",
        "VerifiedRunAuthorization",
    }:
        from arcagent.modules import run_intents

        return getattr(run_intents, name)
    if name in {"AcceptedRunOwner", "CanonicalRunRequest", "RunTriggerIssuer"}:
        from arcagent.core import run_contract

        return getattr(run_contract, name)
    if name == "DeliveryUnavailableError":
        from arcagent.core.run_contract import DeliveryUnavailableError

        return DeliveryUnavailableError
    if name in {
        "ControlArtifactAuthority",
        "ControlActionProofSource",
        "ControlArtifactRefusedError",
        "ControlArtifactUnavailableError",
        "SignedControlRevision",
    }:
        from arcagent.core import control_contract

        return getattr(control_contract, name)
    if name in {"MemoryConfig", "MemoryPromotionConfig"}:
        # The memory module is removable; importing arcagent must never pull it.
        from arcagent.modules.memory import config as memory_config

        return getattr(memory_config, name)
    if name == "register_schedule_revision":
        from arcagent.modules.scheduler.registration import register_schedule_revision

        return register_schedule_revision
    if name in {
        "AnchoredSkillRevisionResolver",
        "OperatorSkillRevisionWriter",
        "ReviewedSkillBundle",
        "SkillRuntime",
        "reviewed_bundle_digest",
        "skill_revision_scope",
    }:
        from arcagent.modules.capability_import import revisions

        return getattr(revisions, name)
    if name in {"LiveSkillRevisionResolver", "build_skill_revision_anchor_factory"}:
        from arcagent.modules.capability_import import authority_factory

        return getattr(authority_factory, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "MEMORY_PROMOTION_MAX_ITEMS",
    "NOT_INSTALLED",
    "SECURITY_CONFIG_KNOBS",
    "AcceptedRunOwner",
    "AnchoredSkillRevisionResolver",
    "ArcAgent",
    "ArcAgentConfig",
    "ArcAgentError",
    "AttachmentFactory",
    "AuditChain",
    "Authorization",
    "CallJob",
    "CallQueueCoordinator",
    "CanonicalRunRequest",
    "CapabilityImportError",
    "CapabilityImportLimits",
    "CapabilityImportManifest",
    "CapabilityImportResult",
    "CapabilityImportReview",
    "CapabilityImportService",
    "CapabilityImportStatus",
    "CapabilityLoader",
    "CapabilityRegistry",
    "CapabilityUnavailableError",
    "CatalogEntry",
    "ClosableSink",
    "ConfigError",
    "Connection",
    "ConnectionWorld",
    "Connections",
    "ConnectorControl",
    "ConnectorMutation",
    "ConnectorPlan",
    "ConnectorReconcileResult",
    "ContextError",
    "ControlActionProofSource",
    "ControlArtifactAuthority",
    "ControlArtifactRefusedError",
    "ControlArtifactUnavailableError",
    "DeliveryStreamEvent",
    "DeliveryStreamSource",
    "DeliveryTerminalEvent",
    "DeliveryTextEvent",
    "DeliveryToolEvent",
    "DeliveryUnavailableError",
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
    "LedgerRunOwner",
    "LiveSkillRevisionResolver",
    "MemoryConfig",
    "MemoryPromotionConfig",
    "ModuleBusError",
    "OperatorSkillRevisionWriter",
    "PersonalKnowledgePort",
    "ProbeResult",
    "PromotionSource",
    "QueueCancellation",
    "QueueControlSnapshot",
    "QueueLimits",
    "QueueMetadataPage",
    "QueueReadScope",
    "RemoteLoginLedger",
    "RemoteLoginStart",
    "ReviewedSkillBundle",
    "RootTokenBudget",
    "RunIntentLedger",
    "RunIntentUnavailableError",
    "RunTriggerIssuer",
    "ScheduleEntry",
    "ScheduleMetadata",
    "ScheduleStore",
    "SecurityConfig",
    "SharedKnowledgePort",
    "SignedControlRevision",
    "SkillArtifactResolver",
    "SkillPackError",
    "SkillRuntime",
    "SourceRefusedError",
    "SourceUnreachableError",
    "Tier",
    "ToolError",
    "ToolOutcome",
    "ToolPolicyState",
    "ToolPolicySummary",
    "ToolResult",
    "ToolSpec",
    "ToolVetoedError",
    "VerifiedRunAuthorization",
    "append_module_scan_roots",
    "audit_tier_relaxations",
    "build_mcp_door",
    "build_prompt_resolver",
    "build_skill_revision_anchor_factory",
    "builtin_capabilities_path",
    "catalog",
    "classifier_models",
    "collect_agent_capability_inventory",
    "config_render",
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
    "make_spawn_tool",
    "manifest_dict",
    "model_config_path",
    "module_root",
    "modules_path",
    "operator_key_path",
    "pin_name_for",
    "read_capability_source",
    "register_schedule_revision",
    "resolve_deployment",
    "resolve_roots",
    "resolve_workspace_import_policy",
    "reviewed_bundle_digest",
    "revoke_capability",
    "revoke_skill_folder",
    "serve_mcp_stdio",
    "set_workflow_runner",
    "sidecar_path",
    "sign_capability",
    "sign_skill_folder",
    "signed_workspace_files",
    "skill_revision_scope",
    "stream_token_text",
    "stricter_tier",
    "summarize_tool_policy",
    "tier_rank",
    "tool",
    "trust_bundled_capabilities",
    "validate_module_configs",
    "validate_skill_folder",
    "verify_file",
    "write_signature",
    "write_signature_with_signer",
]

try:
    __version__ = version("arc-agent")
except PackageNotFoundError:  # reason: source checkout without an installed distribution
    __version__ = "0.19.0"
