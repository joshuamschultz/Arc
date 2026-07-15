"""Config parser — TOML + Pydantic 2.x validation with env var overrides.

An agent's configuration is split across THREE sibling files in one directory,
one per concern boundary (CLAUDE.md: "don't mix concerns"):

  * ``arcagent.toml`` — agent/identity/vault/tools/telemetry/context/session/
    security/capabilities/team/spawn/ui/``[modules.*]``/``[arcstore]``.
  * ``arcllm.toml``   — LLM-wire: ``[llm]`` (+ ``[llm.modules.*]``), ``[eval]``,
    ``[budget]``. (The same file also carries arcllm's own global provider
    routing at ``[defaults]``/``[modules]``/``[vault]``, read by arcllm itself —
    this loader only ever reads ``[llm]``/``[eval]``/``[budget]`` from it.)
  * ``arcrun.toml``   — the agentic-loop controls (``ArcRunConfig``, top-level).

Two-phase error handling:
1. TOML syntax errors (with line/column from tomllib)
2. Pydantic validation errors (with field paths)

Each file-family deep-merges independently (later wins):
  1. Packaged defaults   (in-code: the required ``[llm].model`` fallback)
  2. User-wide defaults   (${ARC_CONFIG_DIR:-~/.arc}/<file>.toml)
  3. Per-agent file       (<agent-dir>/<file>.toml)
The three merged results are composed into one :class:`ArcAgentConfig`; a
missing sibling falls through to packaged/Pydantic defaults. Env vars
(ARCAGENT_ prefix, ``__`` for nesting) override the composed result last.

Dicts deep-merge across layers; lists and scalars are replaced.

  ARCAGENT_LLM__MODEL=openai/gpt-4o        overrides [llm] model (arcllm.toml)
  ARCAGENT_ARCRUN__MAX_TURNS=50            overrides the loop cap (arcrun.toml)
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from arctrust import ValidatorsConfig, arc_home
from pydantic import BaseModel, ConfigDict, Field, model_validator

from arcagent.core.errors import ConfigError
from arcagent.tiers import SECURITY_CONFIG_KNOBS, resolve_tier_floor

_logger = logging.getLogger("arcagent.config")

# Packaged fallback model — the base layer of the arcllm.toml chain. Ensures an
# agent whose directory has ONLY arcagent.toml still boots with a valid LLM
# config (``[llm].model`` is otherwise required and has no field default).
DEFAULT_MODEL = "anthropic/claude-sonnet-4-5-20250929"

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


class BudgetConfig(BaseModel):
    """SPEC-038 REQ-001/004/005 — per-run token/cost/request ceilings (LLM10)."""

    # Tier-resolved at dispatch: personal relaxable/unbounded-when-unset;
    # enterprise/federal treat a set ceiling as a non-relaxable floor. ``None`` =
    # unbounded. Feeds both the arcrun circuit-breaker and the ProviderLayer.
    max_tokens: int | None = None
    max_cost_usd: float | None = None
    max_requests: int | None = None


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
    # SPEC-035 REQ-002 — operator-declared paths that are read-only to the
    # agent's mutating tools, unioned with the goal-file defaults. Resolved once
    # at agent start; the agent has no tool that can edit this config.
    protected_paths: list[str] = []
    # SPEC-035 REQ-013 — origins external-comms tools may reach through the
    # EgressProxy (deny-by-default). ``scheme://host[:port]`` entries.
    egress_allowlist: list[str] = []
    # SPEC-038 REQ-023 — per-tool resource classification label (no-read-up).
    # Tool name → classification string (e.g. ``{"read_secret" = "SECRET"}``).
    # Unlabeled tools default to UNCLASSIFIED (no gating).
    classifications: dict[str, str] = {}
    # SPEC-038 REQ-025 — per-origin destination clearance (no-exfil). Allowlisted
    # origin → clearance string. Missing → UNCLASSIFIED (external = lowest).
    egress_clearances: dict[str, str] = {}


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


class HumanGatePolicy(BaseModel):
    """SPEC-035 REQ-014/016 — lethal-trifecta human-approval gate config.

    ``auto_approve`` lists named low-risk leg-compositions that personal/
    enterprise may approve without a human (each an explicit, audited leg list,
    e.g. ``[["private_data", "external_comms", "untrusted_input"]]``). Federal
    ignores it — the gate can never be auto-satisfied at federal (ADR-019).
    """

    timeout_seconds: float = 300.0
    auto_approve: list[list[str]] = []


class ToolsConfig(BaseModel):
    """All tool configurations by transport."""

    mcp_servers: dict[str, MCPServerEntry] = {}
    http: dict[str, HTTPToolEntry] = {}
    process: dict[str, ProcessToolEntry] = {}
    policy: ToolConfig = ToolConfig()
    human_gate: HumanGatePolicy = HumanGatePolicy()
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
    # Approximate input budget (~4 chars/token) for a single eval request.
    # Over-budget input is split into sequential eval runs rather than sent as one
    # request that overflows the context window and errors. Default 100000 keeps a
    # single request safely under a 128k context with output headroom, so it
    # self-heals without operator action. 0 = unlimited (one call, model max).
    max_input_tokens: int = 100000
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
    # Wall-clock cap on the post-turn compaction summarizer LLM call. Compaction
    # is best-effort: a hung provider must never wedge the turn, so on timeout it
    # is skipped (messages left intact) rather than awaited forever. "Timeouts on
    # everything external" (CLAUDE.md).
    compaction_timeout_seconds: float = Field(default=30.0, gt=0)


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
    max_total_tokens: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Shared token pool (LLM10) across all children a run spawns. When "
            "set, each spawned child's tokens debit this pool; once exhausted, "
            "further spawns are refused. None leaves the pool uncapped."
        ),
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

    # SPEC-038 REQ-021 — the agent's max clearance, bound to identity at
    # construction (operator-authored; no agent tool can raise it). REQ-026 —
    # classification_enforced fails the ClassificationLayer closed on a missing
    # clearance context (federal posture); off by default so it never bricks.
    clearance: str = "UNCLASSIFIED"
    classification_enforced: bool = False

    # SPEC-043 REQ-020/021/024/035 — unified loop circuit-breaker thresholds.
    # ``None`` disables a breaker (personal free-run default); federal pins
    # non-relaxable floors in the tier validator below. ``loop_max_parallel``
    # bounds concurrent in-flight tool calls.
    runaway_max_repeat: int | None = None  # identical tool-call signatures → trip
    error_cascade_max: int | None = None  # consecutive tool failures → trip
    loop_max_parallel: int = 10  # semaphore ceiling on concurrent tool calls

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

    # SPEC-037 — asymmetric + FIPS signing + out-of-process key custody.
    signing_algorithm: str = Field(
        default="ed25519",
        description=(
            "Operator/WORM signing algorithm: 'ed25519' (default, "
            "personal/enterprise) or 'ecdsa-p256' (FIPS/federal path). "
            "SPEC-037 REQ-004."
        ),
    )
    custody: str = Field(
        default="in_process",
        description=(
            "Operator key custody: 'in_process' (on-disk 0600 seed, "
            "personal default) or 'vault_transit' (sign by reference; the seed "
            "never enters the agent process — enterprise default, federal "
            "mandatory). SPEC-037 REQ-006/007."
        ),
    )
    notary_keystore: str = Field(
        default="",
        description=(
            "vault_transit only: keystore for the reference out-of-process "
            "FileNotaryTransit signer (dev/CI without an HSM). Empty → "
            "<operator_key_dir>/notary. A real deployment swaps this seam for a "
            "Vault Transit / PKCS#11 HSM adapter. SPEC-037 REQ-006."
        ),
    )
    require_fips: bool = Field(
        default=False,
        description=(
            "Federal floor: when true, startup fails closed unless the crypto "
            "backend is FIPS-validated AND the algorithm is FIPS-approved "
            "(forces ecdsa-p256). Tier is stringency, not a gate. SPEC-037 "
            "REQ-008/009."
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

    @model_validator(mode="after")
    def _enforce_tier_crypto_floor(self) -> SecurityConfig:
        """Couple the crypto + breaker posture to the tier (SPEC-037/043, ADR-019).

        Every federal floor is delegated to the shared
        :func:`arcagent.tiers.resolve_tier_floor` (SPEC-047 dedup): federal forces
        FIPS + vault_transit + ecdsa-p256 and pins the loop-breaker floors, rejecting
        any explicit weaker/disabled value fail-closed. Enterprise defaults to
        vault_transit (REQ-007) but may relax to in_process; personal stays in_process.
        The enforcement policy lives in ``arcagent/tiers.py``; this validator is the hook.
        """
        if self.tier == "federal":
            for knob in SECURITY_CONFIG_KNOBS:
                resolved = resolve_tier_floor(
                    knob,
                    "federal",
                    getattr(self, knob.name),
                    was_set=knob.name in self.model_fields_set,
                )
                setattr(self, knob.name, resolved)
        elif self.tier == "enterprise" and "custody" not in self.model_fields_set:
            self.custody = "vault_transit"
        return self


class CapabilitiesConfig(BaseModel):
    """``[capabilities]`` block — relaxations for agent-authored (untrusted)
    workspace capability validation.

    Agent-authored tools under ``<workspace>/capabilities/`` pass an AST gate
    whose module-import rule is tier-resolved into one
    ``arcagent.tools._dynamic_loader.ImportPolicy`` (via
    ``resolve_workspace_import_policy``), shared by the authoring gate
    (``create_tool``/``update_tool``) and the load gate (``CapabilityLoader``):

    - personal:   allow-all — every import passes (this block is moot).
    - enterprise: blocklist — allow most, block four privileged groups
                  (filesystem: os/shutil/pathlib/tempfile/glob; process/exec:
                  subprocess/multiprocessing; interpreter: sys/ctypes/importlib/
                  pickle/marshal/shelve; network: socket/urllib/http/requests/
                  httpx). ``allow_imports`` entries are subtracted as exceptions;
                  ``allow_all_imports=True`` is honored as a blanket opt-out.
    - federal:    pure allowlist (deny by default) — ONLY ``allow_imports`` (plus
                  the always-allowed seed ``__future__``/``arcagent``) passes;
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
            "tools (e.g. ['sys', 'subprocess']). At enterprise these are exceptions "
            "subtracted from the blocklist; at federal they are the allowlist itself."
        ),
    )


class SandboxSettings(BaseModel):
    """``[arcrun.sandbox]`` — the loop's tool-permission boundary.

    ``allowed_tools = None`` (the default) permits every registered tool; a list
    restricts the loop to exactly those tool names. Maps to arcrun's
    ``SandboxConfig`` at dispatch; the runtime ``check`` predicate is wired in
    code, never from config.
    """

    allowed_tools: list[str] | None = None


class ArcRunConfig(BaseModel):
    """``arcrun.toml`` — the agentic-loop controls (arcrun's mechanism knobs).

    Owns the loop mechanics arcagent hands to :func:`arcrun.run_stream` /
    :func:`arcrun.run_async`: turn ceiling, per-tool timeout, strategy filter,
    the sandbox tool boundary, and the personal/enterprise approval opt-in list.

    NOT here: the per-run token/cost/request ceilings (``[budget]`` — an LLM-cost
    concern that lives in ``arcllm.toml``) and the tier-floored circuit breakers
    (``runaway_max_repeat`` / ``error_cascade_max`` / ``loop_max_parallel`` — SC
    security floors that live in ``[security]``). Both are still resolved and
    handed to the loop by ``build_loop_controls``; they are simply owned by the
    concern that governs their stringency.
    """

    max_turns: int = Field(default=25, gt=0, description="Hard cap on agentic loop turns.")
    tool_timeout: float | None = Field(
        default=None,
        gt=0,
        description="Per-tool-call wall-clock timeout (seconds). None = no loop-level timeout.",
    )
    allowed_strategies: list[str] | None = Field(
        default=None,
        description="Restrict the loop to these strategy names. None = all registered strategies.",
    )
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    approval_opt_in: list[str] = Field(
        default_factory=list,
        description=(
            "Tool names that require human approval at personal/enterprise tier "
            "even when the tier ladder would not flag them. Ignored at federal "
            "(the full surface is already gated)."
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
    budget: BudgetConfig = BudgetConfig()
    arcrun: ArcRunConfig = ArcRunConfig()


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


# LLM-wire sections composed from the arcllm.toml chain (never from arcagent.toml).
_ARCLLM_SECTIONS = ("llm", "eval", "budget")

# Base layer of the arcllm.toml chain: the one required field that has no Pydantic
# default, so an agent dir with only arcagent.toml still validates.
_PACKAGED_LLM_DEFAULTS: dict[str, Any] = {"llm": {"model": DEFAULT_MODEL}}


def _user_config_root() -> Path:
    """Return the user-wide config dir: ${ARC_CONFIG_DIR:-~/.arc}.

    Delegates to :func:`arctrust.arc_home` — the single source of truth for the
    Arc config root, shared with arcui and the CLI so the env override resolves
    identically everywhere.
    """
    return arc_home()


def _sibling_chain(filename: str, agent_dir: Path) -> dict[str, Any]:
    """Merge one file-family: user-wide ``${ARC_CONFIG_DIR}/<file>`` < per-agent.

    Each layer is optional; a missing file is a no-op. Returns the merged raw
    dict (empty when neither layer exists).
    """
    data: dict[str, Any] = {}
    user_path = _user_config_root() / filename
    if user_path.exists():
        data = _parse_toml(user_path)
    per_agent = agent_dir / filename
    if per_agent.exists():
        data = _deep_merge(data, _parse_toml(per_agent))
    return data


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


def _compose_raw_config(path: Path) -> dict[str, Any]:
    """Compose the raw config dict from the three sibling file-families.

    ``path`` is the per-agent ``arcagent.toml``; its siblings ``arcllm.toml`` and
    ``arcrun.toml`` are read from the SAME directory. Each family merges
    independently, then LLM-wire sections and the loop-control block are grafted
    onto the arcagent dict — so ``[llm]``/``[eval]``/``[budget]`` load ONLY from
    the arcllm chain and the loop controls ONLY from the arcrun chain.
    """
    agent_dir = path.parent

    # arcagent family: user-wide base < the per-agent file itself (``path`` — its
    # basename is caller-chosen, so it is parsed directly, not by fixed name).
    raw: dict[str, Any] = {}
    user_agent = _user_config_root() / "arcagent.toml"
    if user_agent.exists():
        raw = _parse_toml(user_agent)
    raw = _deep_merge(raw, _parse_toml(path))

    # arcllm family: packaged default < user-wide < per-agent. Graft the
    # LLM-wire sections on, replacing any stray copies in arcagent.toml.
    llm_raw = _deep_merge(_PACKAGED_LLM_DEFAULTS, _sibling_chain("arcllm.toml", agent_dir))
    for section in _ARCLLM_SECTIONS:
        raw.pop(section, None)
        if section in llm_raw:
            raw[section] = llm_raw[section]

    # arcrun family: the whole file IS the loop-control block.
    raw["arcrun"] = _sibling_chain("arcrun.toml", agent_dir)
    return raw


def load_config(path: Path = Path("arcagent.toml")) -> ArcAgentConfig:
    """Load and validate an agent's composed ArcAgent configuration.

    Reads ``path`` (the per-agent ``arcagent.toml``) plus its sibling
    ``arcllm.toml`` and ``arcrun.toml`` from the same directory, deep-merging
    each file-family over its user-wide (${ARC_CONFIG_DIR:-~/.arc}) base, then
    composing the three into one config. Env vars (ARCAGENT_ prefix) override
    the composed result.
    """
    if not path.exists():
        raise ConfigError(
            code="CONFIG_FILE_NOT_FOUND",
            message=f"Config file not found: {path}",
            details={"path": str(path)},
        )

    raw_data = _compose_raw_config(path)
    _apply_env_overrides(raw_data)

    try:
        return ArcAgentConfig(**raw_data)
    except Exception as exc:  # reason: re-raise after log
        raise ConfigError(
            code="CONFIG_VALIDATION",
            message=f"Config validation failed: {exc}",
            details={"path": str(path), "errors": str(exc)},
        ) from exc
