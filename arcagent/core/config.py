"""Config parser — TOML + Pydantic 2.x validation with env var overrides.

Two-phase error handling:
1. TOML syntax errors (with line/column from tomllib)
2. Pydantic validation errors (with field paths)

Environment variable overrides use ARCAGENT_ prefix with __ for nesting:
  ARCAGENT_LLM__MODEL=openai/gpt-4o overrides [llm] model
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from arcagent.core.errors import ConfigError

_logger = logging.getLogger("arcagent.config")

# --- Nested config models ---


class AgentConfig(BaseModel):
    """Agent identity and workspace configuration."""

    name: str
    org: str = "default"
    type: str = "executor"
    workspace: str = "./workspace"


class LLMConfig(BaseModel):
    """LLM provider configuration for ArcLLM."""

    model: str
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = 0.7


class IdentityConfig(BaseModel):
    """Identity and key management configuration."""

    did: str = ""
    key_dir: str = "~/.arcagent/keys"
    vault_path: str = ""


class VaultConfig(BaseModel):
    """Vault backend configuration (reuses ArcLLM VaultResolver)."""

    backend: str = ""
    cache_ttl_seconds: int = 300


class ToolConfig(BaseModel):
    """Tool policy — allowlist/denylist and timeout."""

    allow: list[str] = []
    deny: list[str] = []
    timeout_seconds: int = 30
    allowed_paths: list[str] = []


class NativeToolEntry(BaseModel):
    """Python function tool entry."""

    module: str
    description: str = ""


class MCPServerEntry(BaseModel):
    """MCP server tool entry."""

    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    timeout_seconds: int = 30


class HTTPToolEntry(BaseModel):
    """HTTP-based tool entry."""

    url: str
    method: str = "POST"
    headers: dict[str, str] = {}
    timeout_seconds: int = 30


class ProcessToolEntry(BaseModel):
    """Subprocess-based tool entry."""

    command: str
    args: list[str] = []
    timeout_seconds: int = 30


class ToolsConfig(BaseModel):
    """All tool configurations by transport."""

    native: dict[str, NativeToolEntry] = {}
    mcp_servers: dict[str, MCPServerEntry] = {}
    http: dict[str, HTTPToolEntry] = {}
    process: dict[str, ProcessToolEntry] = {}
    policy: ToolConfig = ToolConfig()
    allowed_module_prefixes: list[str] = Field(default=["arcagent."])


class ModuleEntry(BaseModel):
    """Module configuration entry."""

    enabled: bool = True
    priority: int = 100
    config: dict[str, Any] = {}


class TelemetryConfig(BaseModel):
    """OpenTelemetry and logging configuration."""

    enabled: bool = True
    service_name: str = "arcagent"
    log_level: str = "INFO"
    export_traces: bool = False
    exporter_endpoint: str = ""


class ContextConfig(BaseModel):
    """Context window management thresholds."""

    max_tokens: int = Field(default=128000, gt=0)
    prune_threshold: float = 0.70
    compact_threshold: float = 0.85
    emergency_threshold: float = 0.95
    estimate_multiplier: float = 1.1


class EvalConfig(BaseModel):
    """Configuration for the evaluation/background model.

    Used for entity extraction, policy evaluation, and compaction
    summarization. Separate from agent's primary model for cost control.
    """

    provider: str = ""  # Empty = use same provider as agent
    model: str = ""  # Empty = use agent's model
    max_tokens: int = 1024
    temperature: float = 0.2  # Low for evaluation consistency
    timeout_seconds: int = 30
    fallback_behavior: str = "skip"  # "skip" | "error"
    max_concurrent: int = 2  # Semaphore limit


class MemoryConfig(BaseModel):
    """Configuration for the memory module."""

    context_budget_tokens: int = 2000
    notes_budget_today_tokens: int = 1000
    notes_budget_yesterday_tokens: int = 500
    search_weight_bm25: float = 0.7
    search_weight_vector: float = 0.3
    embedding_model: str = "all-MiniLM-L6-v2"
    entity_extraction_enabled: bool = True
    policy_eval_interval_turns: int = 10


class SessionConfig(BaseModel):
    """Configuration for session management."""

    retention_count: int = 50  # Keep last N sessions
    retention_days: int = 30  # Or sessions from last N days
    compaction_summary_max_chars: int = 2000


class ExtensionEntry(BaseModel):
    """Per-extension configuration."""

    sandbox_mode: str = "workspace"  # workspace | paths | strict
    enabled: bool = True
    allowed_paths: list[str] = []  # Additional paths for 'paths' sandbox mode


class ExtensionConfig(BaseModel):
    """Extension system configuration."""

    paths: list[str] = []
    extensions: dict[str, ExtensionEntry] = {}
    global_dir: str = "~/.arcagent/extensions"
    workspace_tools_dir: str = "tools"


# --- Root config ---


class ArcAgentConfig(BaseModel):
    """Root configuration loaded from arcagent.toml with env var overrides.

    Priority (highest to lowest):
    1. Environment variables (ARCAGENT_ prefix, __ for nesting)
    2. TOML file values
    3. Pydantic defaults
    """

    agent: AgentConfig
    llm: LLMConfig
    identity: IdentityConfig = IdentityConfig()
    vault: VaultConfig = VaultConfig()
    tools: ToolsConfig = ToolsConfig()
    modules: dict[str, ModuleEntry] = {}
    telemetry: TelemetryConfig = TelemetryConfig()
    context: ContextConfig = ContextConfig()
    eval: EvalConfig = EvalConfig()
    memory: MemoryConfig = MemoryConfig()
    session: SessionConfig = SessionConfig()
    extensions: ExtensionConfig = ExtensionConfig()


_ENV_PREFIX = "ARCAGENT_"
_ENV_DELIMITER = "__"

# Security-sensitive config paths that cannot be overridden via env vars.
# These require explicit TOML config changes by a trusted admin.
_ENV_DENYLIST_PREFIXES = frozenset(
    {
        "vault__backend",
        "tools__native",
        "tools__process",
        "identity__key_dir",
    }
)


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Override TOML values with environment variables.

    ARCAGENT_LLM__MODEL=openai/gpt-4o  ->  data["llm"]["model"] = "openai/gpt-4o"
    ARCAGENT_AGENT__ORG=test-org        ->  data["agent"]["org"] = "test-org"

    Security-sensitive keys (vault backend, native tools, identity paths)
    are blocked from env var override to prevent injection attacks.
    """
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        env_path = key[len(_ENV_PREFIX) :].lower()

        # Block security-sensitive overrides
        if any(env_path.startswith(prefix) for prefix in _ENV_DENYLIST_PREFIXES):
            _logger.warning("Blocked env var override for security-sensitive key: %s", key)
            continue

        parts = env_path.split(_ENV_DELIMITER)
        target = data
        for part in parts[:-1]:
            if part not in target:
                target[part] = {}
            if not isinstance(target[part], dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
    return data


def load_config(path: Path = Path("arcagent.toml")) -> ArcAgentConfig:
    """Load and validate ArcAgent configuration.

    Two-phase error handling:
    1. TOML parse (syntax errors with line/column)
    2. Pydantic validation (semantic errors with field paths)

    Environment variables override TOML values (ARCAGENT_ prefix).
    """
    if not path.exists():
        raise ConfigError(
            code="CONFIG_FILE_NOT_FOUND",
            message=f"Config file not found: {path}",
            details={"path": str(path)},
        )

    raw_text = path.read_text(encoding="utf-8")

    # Phase 1: TOML syntax
    try:
        raw_data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            code="CONFIG_SYNTAX",
            message=f"TOML syntax error: {exc}",
            details={"path": str(path), "error": str(exc)},
        ) from exc

    # Apply env var overrides (higher priority than TOML)
    _apply_env_overrides(raw_data)

    # Phase 2: Pydantic validation
    try:
        return ArcAgentConfig(**raw_data)
    except Exception as exc:
        raise ConfigError(
            code="CONFIG_VALIDATION",
            message=f"Config validation failed: {exc}",
            details={"path": str(path), "errors": str(exc)},
        ) from exc
