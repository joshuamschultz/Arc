"""Typed contracts for configuring and rebinding ArcAgent module runtimes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from arctrust import AgentIdentity, Signer

from arcagent.core.config import EvalConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus
from arcagent.core.telemetry import AgentTelemetry
from arcagent.core.tool_registry import ToolRegistry
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


@dataclass(frozen=True)
class RuntimeModuleSpec:
    """Explicit injection contract for one module runtime."""

    dependencies: tuple[DependencyKey, ...]
    optional: bool = False

    def kwargs(self, deps: RuntimeDependencies, module_config: dict[str, Any]) -> dict[str, Any]:
        values: dict[DependencyKey, Any] = {
            DependencyKey.CONFIG: module_config,
            DependencyKey.EVAL_CONFIG: deps.eval_config,
            DependencyKey.TELEMETRY: deps.telemetry,
            DependencyKey.WORKSPACE: deps.workspace,
            DependencyKey.LLM_CONFIG: deps.llm_config,
            DependencyKey.AGENT_NAME: deps.agent_name,
            DependencyKey.TEAM_ROOT: deps.team_root,
            DependencyKey.BUS: deps.bus,
            DependencyKey.AGENT_DID: deps.agent_did,
            DependencyKey.IDENTITY: deps.identity,
            DependencyKey.CONFIG_PATH: deps.config_path,
            DependencyKey.TOOL_REGISTRY: deps.tool_registry,
            DependencyKey.TIER: deps.tier,
            DependencyKey.POLICY_PIPELINE: deps.policy_pipeline,
            DependencyKey.EGRESS_PROXY: deps.egress_proxy,
            DependencyKey.HUMAN_GATE: deps.human_gate,
            DependencyKey.OPERATOR_SIGNER: deps.operator_signer,
            DependencyKey.AGENT_RUN_FN: deps.agent_run_fn,
        }
        return {key.value: values[key] for key in self.dependencies}


class RuntimeModule(Protocol):
    """Minimal configuration surface imported from a module's ``_runtime``."""

    configure: Callable[..., None]


@runtime_checkable
class RuntimeBindable(Protocol):
    """Optional task-local state binding surface implemented by runtimes."""

    def state(self) -> object: ...

    def bind(self, state: object) -> None: ...


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
    "RuntimeModuleSpec",
]
