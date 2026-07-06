"""Config parser — TOML + Pydantic 2.x validation with env var overrides.

Two-phase error handling:
1. TOML syntax errors (with line/column from tomllib)
2. Pydantic validation errors (with field paths)

Layered precedence (later wins):
  1. User-wide defaults  (${ARC_CONFIG_DIR:-~/.arc}/arcagent.toml)
  2. Per-agent file      (the path argument — REQUIRED, supplies identity)
  3. Env var overrides   (ARCAGENT_ prefix with __ for nesting)

Dicts deep-merge across layers; lists and scalars are replaced. Missing
user-wide file = no-op (current behavior preserved).

  ARCAGENT_LLM__MODEL=openai/gpt-4o overrides [llm] model
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
    # Per-agent arcllm module overrides. Keyed by arcllm module name
    # (``queue``, ``retry``, ``rate_limit``, ``telemetry``, …); each value
    # is merged into that module's defaults at model load. Lets an agent
    # tune e.g. ``[llm.modules.queue] call_timeout = 600`` from its own
    # ``arcagent.toml`` without editing arcllm's global ``config.toml``.
    # Unknown module names are rejected at load time so a typo
    # (``[llm.modules.qeue]``) fails loudly.
    modules: dict[str, dict[str, Any]] = Field(default_factory=dict)


class UIConfig(BaseModel):
    """ArcUI display metadata — read by arcui's Fleet/Team Chat surfaces.

    This block is metadata-only: nothing inside arcagent core reads it.
    Declared here (instead of accepted via ``extra="allow"``) so a typo
    like ``role_lable = "intake"`` fails loudly at config-load time
    rather than silently vanishing. Demo kits (BlackArc's team_roster_writer,
    AccessGuard's per-stage agent.toml) emit this block; arcui's Fleet
    panel and Team Chat tab read it via the roster API.

    All fields default to empty so existing arcagent.toml files that
    omit ``[ui]`` keep validating unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = ""
    role_label: str = ""
    color: str = ""
    hidden: bool = False


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

    mcp_servers: dict[str, MCPServerEntry] = {}
    http: dict[str, HTTPToolEntry] = {}
    process: dict[str, ProcessToolEntry] = {}
    policy: ToolConfig = ToolConfig()
    allowed_module_prefixes: list[str] = Field(default=["arcagent."])
    preamble: str = ""


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
    # Persist raw tool arguments + results to the operational spool so the
    # ArcRun observability surface can show each tool call's input/output. Bodies
    # may carry sensitive data — federal/enterprise deployments set this False to
    # keep only digests + sizes (NFR-2: raw capture is an explicit opt-in).
    capture_tool_io: bool = True


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
    background_queue_size: int = 10  # Per-module background task queue depth
    background_task_timeout: int = 120  # Seconds before background task timeout


class SessionConfig(BaseModel):
    """Configuration for session management."""

    retention_count: int = 50  # Keep last N sessions
    retention_days: int = 30  # Or sessions from last N days
    compaction_summary_max_chars: int = 2000


class TeamSection(BaseModel):
    """Team coordination config — shared by all team modules.

    Any module that participates in the team (messaging, file sharing,
    shared knowledge, etc.) reads its root from here rather than
    declaring its own ``team_root`` field.
    """

    root: str = ""


class SpawnConfig(BaseModel):
    """Spawn / orchestration configuration.

    Controls whether the agent registers ``spawn_task`` as a tool the
    LLM can call. Spawn is the LLM-driven decomposition mechanism —
    the model decides at runtime to fan out into child agents.

    Code-level fan-out (agent code calling ``arcrun.run`` multiple
    times via ``asyncio.gather``) is always available regardless of
    this setting; it does not require a tool.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Register spawn_task as an LLM-callable tool. When False, "
            "the model cannot drive decomposition itself — the agent "
            "must orchestrate fan-out in code."
        ),
    )
    max_depth: int = Field(
        default=3,
        ge=0,
        description="Maximum nesting depth for spawned children.",
    )
    max_concurrent: int = Field(
        default=5,
        ge=1,
        description="Maximum concurrent child runs.",
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        description="Wall-clock timeout per child run.",
    )


class ValidatorEntry(BaseModel):
    """A single TOFU-approved validator script (R-042 / R-043).

    Persisted under ``[[security.validators.approved]]`` in
    ``arcagent.toml``. Written only by the human user via
    ``arc trust approve`` — the agent has no write access.

    ``hash`` is the sha256 digest of the validator source body, prefixed
    ``sha256:``. ``timestamp`` is RFC3339 UTC.
    """

    name: str = Field(description="Validator script logical name")
    hash: str = Field(description="sha256:<digest> of approved source")
    approver: str = Field(description="Identity that approved (email or DID)")
    timestamp: str = Field(description="RFC3339 UTC timestamp of approval")


class ValidatorsConfig(BaseModel):
    """``[security.validators]`` block — TOFU policy state.

    Lives at agent-root, never inside workspace (R-043). Default is
    federal-safe: ``auto_run_agent_code = False`` and zero approved
    entries. Personal-tier templates seed it to ``True``; enterprise +
    federal templates leave it ``False``.
    """

    auto_run_agent_code: bool = Field(
        default=False,
        description=(
            "Personal tier only — auto-run agent-authored Python after "
            "AST validation. Enterprise/federal must approve via TOFU."
        ),
    )
    approved: tuple[ValidatorEntry, ...] = Field(
        default=(),
        description="Persisted TOFU approvals; appended by `arc trust approve`",
    )


class SecurityConfig(BaseModel):
    """Security and tier configuration.

    Tier controls credential resolution policy across the entire agent:
    - federal:    vault required; hard error if unreachable; no env/file fallback
    - enterprise: vault preferred; warn + env fallback if unreachable
    - personal:   vault optional; env or ~/.arc/secrets/{name} fallback

    This is the canonical read location for config.security.tier (SDD §4.1).
    """

    # Canonical tier name — read by vault resolver, memory ACL, executor
    # selection, and any other policy-aware component.
    tier: str = Field(
        default="personal",
        description=(
            "Deployment tier: 'federal', 'enterprise', or 'personal'. "
            "Controls credential resolution, memory ACL defaults, and "
            "executor selection."
        ),
    )

    # SPEC-021 — TOFU approvals for self-executing agent code.
    validators: ValidatorsConfig = Field(default_factory=ValidatorsConfig)

    # SPEC-034 — durable WORM chain file for policy-decision audit records.
    # Relative paths resolve against the workspace; None → <workspace>/audit/
    # policy-chain.jsonl. The pipeline routes every ALLOW/DENY here as one
    # tamper-evident, Ed25519-signed record (AU-9/AU-10).
    policy_audit_log: str | None = Field(
        default=None,
        description=(
            "Path to the WORM audit chain for policy decisions. Relative to the "
            "workspace; defaults to <workspace>/audit/policy-chain.jsonl."
        ),
    )

    # SPEC-053 — operator-key custody. The operator key is the deployment audit
    # authority that signs every WORM chain; it is distinct from the agent DID
    # (the audited subject must not be the audit authority) and is loaded
    # read-only from OUTSIDE the workspace tool-sandbox (AU-9(2)/AU-10).
    operator_key_dir: str = Field(
        default="~/.arc/operator",
        description=(
            "Directory holding the deployment operator key (audit authority). "
            "Kept outside the agent workspace; private key is 0600, dir 0700. "
            "SPEC-053."
        ),
    )
    operator_vault_path: str = Field(
        default="",
        description=(
            "When set (and a vault backend is configured), the operator key is "
            "resolved via the vault instead of the on-disk file (SPEC-037 seam)."
        ),
    )

    # SPEC-053 — federal witness anchor (REQ-009/010). Federal tier submits each
    # operator-signed checkpoint head to an EXTERNAL append-only witness so a
    # rollback past the last anchor is detectable even by a holder of the
    # operator key. Other tiers ignore these.
    witness_medium_path: str = Field(
        default="~/.arc/witness/anchor.log",
        description=(
            "Append-only medium the federal witness writes operator-signed heads "
            "to. MUST live outside operator_key_dir — the operator-key holder must "
            "not also own the witness, or the rollback check is illusory. Federal "
            "deployments SHOULD point this at a separate host or removable WORM "
            "medium (a deployment concern, like the vault seam). SPEC-053 REQ-009."
        ),
    )
    witness_mode: str = Field(
        default="offline",
        description=(
            "Federal witness backend: 'offline' (air-gapped append-only medium) "
            "or 'transparency_log' (online Rekor-style). SPEC-053 REQ-010."
        ),
    )
    witness_log_url: str = Field(
        default="",
        description="Transparency-log endpoint when witness_mode='transparency_log'.",
    )


class CapabilitiesConfig(BaseModel):
    """``[capabilities]`` block — relaxations for agent-authored (untrusted)
    workspace capability validation.

    Agent-authored tools under ``<workspace>/capabilities/`` pass an AST gate
    that blocks privileged imports (``sys``, ``os``, ``subprocess``, ...). These
    knobs permit specific imports WITHOUT moving the tool out of the protected
    workspace root. They are tier-gated (resolved in
    ``arcagent.tools._dynamic_loader.resolve_workspace_import_policy``):

    - personal:   all imports allowed (this block is moot — everything passes).
    - enterprise: deny by default; opt in via ``allow_all_imports`` OR ``allow_imports``.
    - federal:    deny by default; ONLY ``allow_imports`` is honored;
                  ``allow_all_imports`` is IGNORED (no blanket relaxation).

    Sandbox-escape protections (eval/exec, frame/class traversal) are always
    enforced regardless of this block — only module imports are relaxable.
    """

    allow_all_imports: bool = Field(
        default=False,
        description=(
            "Permit any import in workspace-authored tools. Honored at "
            "enterprise; IGNORED at federal; moot at personal."
        ),
    )
    allow_imports: list[str] = Field(
        default_factory=list,
        description=(
            "Specific otherwise-blocked modules to permit in workspace-authored "
            "tools (e.g. ['sys', 'subprocess']). Honored at enterprise + federal."
        ),
    )


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
    team: TeamSection = TeamSection()
    telemetry: TelemetryConfig = TelemetryConfig()
    context: ContextConfig = ContextConfig()
    eval: EvalConfig = EvalConfig()
    session: SessionConfig = SessionConfig()
    security: SecurityConfig = SecurityConfig()
    capabilities: CapabilitiesConfig = CapabilitiesConfig()
    spawn: SpawnConfig = SpawnConfig()
    ui: UIConfig = UIConfig()


_ENV_PREFIX = "ARCAGENT_"
_ENV_DELIMITER = "__"

# Security-sensitive config paths that cannot be overridden via env vars.
# These require explicit TOML config changes by a trusted admin.
_ENV_DENYLIST_PREFIXES = frozenset(
    {
        "vault__backend",
        "tools__process",
        "tools__preamble",
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


def _user_config_path() -> Path:
    """Return the user-wide override path: ${ARC_CONFIG_DIR:-~/.arc}/arcagent.toml."""
    base = os.environ.get("ARC_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".arc"
    return root / "arcagent.toml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base. Dicts merge; lists & scalars replace."""
    result = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _parse_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file, raising a ConfigError with consistent details on syntax errors."""
    raw_text = path.read_text(encoding="utf-8")
    try:
        return tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            code="CONFIG_SYNTAX",
            message=f"TOML syntax error: {exc}",
            details={"path": str(path), "error": str(exc)},
        ) from exc


def load_config(path: Path = Path("arcagent.toml")) -> ArcAgentConfig:
    """Load and validate ArcAgent configuration.

    Layered: ${ARC_CONFIG_DIR:-~/.arc}/arcagent.toml is the user-wide base
    (when present); ``path`` is the per-agent file and supplies identity.
    Env vars (ARCAGENT_ prefix) override both.
    """
    if not path.exists():
        raise ConfigError(
            code="CONFIG_FILE_NOT_FOUND",
            message=f"Config file not found: {path}",
            details={"path": str(path)},
        )

    raw_data: dict[str, Any] = {}

    user_path = _user_config_path()
    if user_path.exists():
        raw_data = _parse_toml(user_path)

    raw_data = _deep_merge(raw_data, _parse_toml(path))

    _apply_env_overrides(raw_data)

    try:
        return ArcAgentConfig(**raw_data)
    except Exception as exc:  # reason: re-raise after log
        raise ConfigError(
            code="CONFIG_VALIDATION",
            message=f"Config validation failed: {exc}",
            details={"path": str(path), "errors": str(exc)},
        ) from exc
