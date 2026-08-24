"""Typed contracts for configuring and rebinding ArcAgent module runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from arctrust import AgentIdentity, Signer

from arcagent.core.config import EvalConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_registry import ToolRegistry
from arcagent.extension.source_catalog import SourceCatalog
from arcagent.tools._egress import EgressProxy


@dataclass(frozen=True)
class RuntimeDependencies:
    """Complete dependency graph offered to enabled module runtimes."""

    workspace: Path
    config_path: Path
    eval_config: EvalConfig
    llm_config: LLMConfig
    telemetry: AgentTelemetry | None
    bus: ModuleBus | None
    tool_registry: ToolRegistry | None
    agent_name: str
    agent_did: str
    team_root: str
    tier: str
    identity: AgentIdentity | None
    operator_signer: Signer | None
    policy_pipeline: Any
    egress_proxy: EgressProxy | None
    human_gate: Any
    agent_run_fn: Callable[..., Awaitable[Any]]
    #: Supplied by the orchestration layer above the agent, absent when the
    #: agent runs alone. Every fleet-facing feature reports itself unavailable
    #: rather than building a fleet of its own.
    fleet: Any = None
    arcstore_opener: Callable[[], Awaitable[Any]] | None = None
    source_sync_store_opener: Callable[[], Awaitable[Any]] | None = None
    source_catalog: SourceCatalog = field(default_factory=SourceCatalog)

    def select_for(
        self, configure: Callable[..., None], module_config: dict[str, Any]
    ) -> dict[str, Any]:
        """The dependencies a module's ``configure`` asks for, by parameter name.

        A module declares its contract by naming keyword parameters that match
        the closed :class:`DependencyKey` vocabulary; it receives exactly those
        and nothing else (least privilege — a module that never names
        ``operator_signer`` never gets it). Core names no module: the signature
        is the whole contract, so a new module needs no registration here.

        ``config`` is the module's own ``[modules.NAME.config]`` table; every
        other key mirrors the field of the same name on this container.
        Parameters outside the vocabulary keep their own defaults.
        """
        menu: dict[str, Any] = {
            key.value: getattr(self, key.value)
            for key in DependencyKey
            if key is not DependencyKey.CONFIG
        }
        menu[DependencyKey.CONFIG.value] = module_config
        wanted = inspect.signature(configure).parameters
        return {name: value for name, value in menu.items() if name in wanted}


class DependencyKey(Enum):
    """Closed vocabulary accepted by explicit module runtime specifications."""

    CONFIG = "config"
    EVAL_CONFIG = "eval_config"
    TELEMETRY = "telemetry"
    WORKSPACE = "workspace"
    LLM_CONFIG = "llm_config"
    AGENT_NAME = "agent_name"
    TEAM_ROOT = "team_root"
    BUS = "bus"
    AGENT_DID = "agent_did"
    IDENTITY = "identity"
    CONFIG_PATH = "config_path"
    TOOL_REGISTRY = "tool_registry"
    TIER = "tier"
    POLICY_PIPELINE = "policy_pipeline"
    EGRESS_PROXY = "egress_proxy"
    HUMAN_GATE = "human_gate"
    OPERATOR_SIGNER = "operator_signer"
    AGENT_RUN_FN = "agent_run_fn"
    ARCSTORE_OPENER = "arcstore_opener"
    SOURCE_SYNC_STORE_OPENER = "source_sync_store_opener"
    SOURCE_CATALOG = "source_catalog"
    FLEET = "fleet"


class RuntimeModule(Protocol):
    """Minimal configuration surface imported from a module's ``_runtime``."""

    configure: Callable[..., None]


@runtime_checkable
class RuntimeBindable(Protocol):
    """Optional task-local state binding surface implemented by runtimes."""

    def state(self) -> object: ...

    def bind(self, state: object) -> None: ...


@runtime_checkable
class RuntimeTeardownable(Protocol):
    """Optional inverse of ``configure``.

    A module that owns resources beyond its ``@background_task`` capabilities —
    a live connection, a self-spawned poll task — implements ``teardown`` to
    release them when the module is disabled at runtime. A module without it is
    still fully unwound: its capabilities are removed by the reload and its
    task-local state is dropped. So ``teardown`` is opt-in, for effects the
    capability registry does not already own.
    """

    async def teardown(self) -> None: ...


StateT = TypeVar("StateT")


@dataclass(frozen=True)
class RuntimeBinding(Generic[StateT]):
    """One named, typed task-local runtime state binding."""

    module_name: str
    bind: Callable[[StateT], None]
    state: StateT

    def activate(self) -> None:
        self.bind(self.state)


__all__ = [
    "DependencyKey",
    "RuntimeBindable",
    "RuntimeBinding",
    "RuntimeDependencies",
    "RuntimeModule",
    "RuntimeTeardownable",
]
