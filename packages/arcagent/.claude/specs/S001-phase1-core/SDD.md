# SDD: Phase 1 Core Components

**Spec ID**: S001
**Status**: PENDING
**Last Updated**: 2026-02-14

---

## 1. Architecture Overview

### 1.1 System Context

```
┌─────────────────────────────────────────────────┐
│                  ArcAgent Nucleus                │
│                                                  │
│  config.py ──┬──▶ identity.py                   │
│              ├──▶ telemetry.py                   │
│              ├──▶ module_bus.py ──▶ tool_registry │
│              └──▶ context_manager.py             │
│                                                  │
│  agent.py (orchestrator)                         │
│    ├── arcllm.load_model() → LLMProvider         │
│    ├── arcrun.run(model, tools, system, task)     │
│    └── processes LoopResult                      │
└─────────────────────────────────────────────────┘
         ▲               ▲
         │               │
    ┌────┘               └────┐
    │ ArcLLM                  │ ArcRun
    │ (LLM calls)             │ (agent loop)
    └─────────────────────────┘
```

### 1.2 Component Dependency Order

```
1. config.py          (no dependencies)
2. telemetry.py       (depends on: config)
3. identity.py        (depends on: config, ArcLLM VaultResolver)
4. module_bus.py      (depends on: config, telemetry)
5. tool_registry.py   (depends on: config, module_bus, telemetry)
6. context_manager.py (depends on: config, telemetry)
7. agent.py           (depends on: all above + ArcLLM + ArcRun)
```

### 1.3 LOC Budget

| Component | Budget | Notes |
|-----------|--------|-------|
| config.py | ~300 | TOML parse + Pydantic models |
| identity.py | ~350 | DID, keypair, sign/verify, vault fallback |
| telemetry.py | ~300 | OTel parent spans, structured logging |
| module_bus.py | ~400 | Event dispatch, priority, veto, lifecycle |
| tool_registry.py | ~500 | 4 transports, policy, wrapping |
| context_manager.py | ~500 | System prompt, token counting, compaction |
| agent.py | ~400 | Orchestrator, bridge hooks, startup/shutdown |
| **Total** | **~2,750** | Under 3,000 LOC budget |

---

## 2. Component Designs

### 2.1 Config (config.py)

**Purpose**: Parse `arcagent.toml`, validate with Pydantic, support env overrides.

#### Data Models

```python
from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings

class AgentConfig(BaseModel):
    name: str
    org: str = "default"
    type: str = "executor"  # executor | planner | reviewer
    workspace: str = "./workspace"

class LLMConfig(BaseModel):
    model: str  # ArcLLM model identifier
    max_tokens: int = 4096
    temperature: float = 0.7

class IdentityConfig(BaseModel):
    did: str = ""  # Auto-generated if empty
    key_dir: str = "~/.arcagent/keys"
    vault_path: str = ""  # Empty = file-based (dev mode)

class VaultConfig(BaseModel):
    backend: str = ""  # "module:Class" format, empty = disabled
    cache_ttl_seconds: int = 300

class ToolConfig(BaseModel):
    allow: list[str] = []  # Empty = allow all
    deny: list[str] = []
    timeout_seconds: int = 30

class NativeToolEntry(BaseModel):
    module: str  # "arcagent.tools.filesystem:read_file"
    description: str = ""

class MCPServerEntry(BaseModel):
    command: str  # "npx" or path to binary
    args: list[str] = []
    env: dict[str, str] = {}
    timeout_seconds: int = 30

class HTTPToolEntry(BaseModel):
    url: str
    method: str = "POST"
    headers: dict[str, str] = {}
    timeout_seconds: int = 30

class ProcessToolEntry(BaseModel):
    command: str
    args: list[str] = []
    timeout_seconds: int = 30

class ToolsConfig(BaseModel):
    native: dict[str, NativeToolEntry] = {}
    mcp_servers: dict[str, MCPServerEntry] = {}
    http: dict[str, HTTPToolEntry] = {}
    process: dict[str, ProcessToolEntry] = {}
    policy: ToolConfig = ToolConfig()

class ModuleEntry(BaseModel):
    enabled: bool = True
    priority: int = 100
    config: dict[str, object] = {}

class TelemetryConfig(BaseModel):
    enabled: bool = True
    service_name: str = "arcagent"
    log_level: str = "INFO"
    export_traces: bool = False
    exporter_endpoint: str = ""

class ContextConfig(BaseModel):
    max_tokens: int = 128000
    prune_threshold: float = 0.70   # Start pruning old tool outputs
    compact_threshold: float = 0.85  # LLM summarization
    emergency_threshold: float = 0.95
    estimate_multiplier: float = 1.1

class ArcAgentConfig(BaseSettings):
    """Root config loaded from arcagent.toml with env var overrides."""
    agent: AgentConfig
    llm: LLMConfig
    identity: IdentityConfig = IdentityConfig()
    vault: VaultConfig = VaultConfig()
    tools: ToolsConfig = ToolsConfig()
    modules: dict[str, ModuleEntry] = {}
    telemetry: TelemetryConfig = TelemetryConfig()
    context: ContextConfig = ContextConfig()
```

#### Key Functions

```python
def load_config(path: Path = Path("arcagent.toml")) -> ArcAgentConfig:
    """Two-phase loading: TOML parse (syntax errors with line numbers)
    then Pydantic validation (semantic errors with key paths).
    Env vars override with ARCAGENT_ prefix (e.g., ARCAGENT_LLM__MODEL).
    """
```

#### Error Handling

- `tomllib.TOMLDecodeError` → `ConfigError` with line/column
- `pydantic.ValidationError` → `ConfigError` with field path
- Missing file → `ConfigError` with path

### 2.2 Identity (identity.py)

**Purpose**: DID creation, Ed25519 keypair management, message signing/verification. Reuses ArcLLM's `VaultResolver` for secret resolution with file-based fallback.

#### Key Classes

```python
@dataclass(frozen=True)
class AgentIdentity:
    did: str                        # "did:arc:{org}:{type}/{id}"
    public_key: bytes               # Ed25519 public key
    _signing_key: SigningKey | None  # Private key (None if verify-only)

    def sign(self, message: bytes) -> bytes: ...
    def verify(self, message: bytes, signature: bytes) -> bool: ...

    @classmethod
    def generate(cls, org: str, agent_type: str) -> "AgentIdentity": ...

    @classmethod
    def from_config(cls, config: IdentityConfig,
                    vault_resolver: VaultResolver | None = None) -> "AgentIdentity": ...
```

#### Secret Resolution (reuses ArcLLM)

```python
from arcllm import VaultResolver

def _load_signing_key(config: IdentityConfig,
                      vault_resolver: VaultResolver | None) -> SigningKey:
    """Resolution order:
    1. Vault (if vault_resolver provided and config.vault_path is set)
    2. File-based (config.key_dir / {did}.key)
    3. Generate new keypair and save to key_dir
    """
```

The `VaultResolver` is created once by the orchestrator (`agent.py`) from the shared `[vault]` config section and passed to identity. Same resolver instance is reused across components.

#### DID Format

```
did:arc:{org}:{type}/{id}
did:arc:blackarc:executor/a1b2c3d4
```

- `{id}` derived from public key hash (first 8 hex chars of SHA-256)
- Deterministic: same keypair always produces same DID

### 2.3 Telemetry (telemetry.py)

**Purpose**: Create parent OTel spans that ArcLLM spans auto-nest under. Structured logging. Audit events.

#### Design

Builds on ArcLLM's existing OTel SDK setup. ArcAgent creates coarser parent spans; ArcLLM's `arcllm.invoke` spans auto-nest via OTel context propagation.

```python
class AgentTelemetry:
    def __init__(self, config: TelemetryConfig, identity: AgentIdentity) -> None:
        self._tracer = trace.get_tracer("arcagent", "0.1.0")
        self._logger = logging.getLogger("arcagent")
        self._identity = identity

    @contextlib.asynccontextmanager
    async def session_span(self, task: str) -> AsyncIterator[Span]:
        """Top-level span: arcagent.session. All turns nest under this."""

    @contextlib.asynccontextmanager
    async def turn_span(self, turn_number: int) -> AsyncIterator[Span]:
        """Per-turn span: arcagent.turn. LLM calls nest under this."""

    @contextlib.asynccontextmanager
    async def tool_span(self, tool_name: str, args: dict) -> AsyncIterator[Span]:
        """Per-tool-call span: arcagent.tool."""

    def audit_event(self, event_type: str, details: dict) -> None:
        """Emit structured audit log + span event. Every action."""
```

#### Span Hierarchy

```
arcagent.session (top-level)
  └── arcagent.turn.1
      ├── arcllm.invoke (auto-nested via OTel context)
      └── arcagent.tool.read_file
          └── (tool execution)
  └── arcagent.turn.2
      ├── arcllm.invoke
      └── arcagent.tool.write_file
```

#### Audit Trail

Every `audit_event` produces:
- Structured log (JSON) with agent DID, trace ID, timestamp, event type, details
- OTel span event on current active span
- Classification-aware (event inherits classification from context)

### 2.4 Module Bus (module_bus.py)

**Purpose**: Async event system with priority ordering, veto, error isolation, and module lifecycle.

#### Core Classes

```python
@dataclass
class EventContext:
    event: str                       # "agent:pre_tool"
    data: dict[str, Any]             # Event-specific payload
    agent_did: str                   # Source agent identity
    trace_id: str                    # OTel trace ID
    _vetoed: bool = False
    _veto_reason: str = ""

    def veto(self, reason: str) -> None:
        """Veto this event. First veto wins. All handlers still run."""
        if not self._vetoed:
            self._vetoed = True
            self._veto_reason = reason

    @property
    def is_vetoed(self) -> bool: ...

    @property
    def veto_reason(self) -> str: ...


@dataclass
class HandlerRegistration:
    event: str
    handler: Callable[[EventContext], Awaitable[None]]
    priority: int = 100
    module_name: str = ""


class ModuleBus:
    def __init__(self, config: ArcAgentConfig, telemetry: AgentTelemetry) -> None:
        self._handlers: dict[str, list[HandlerRegistration]] = defaultdict(list)
        self._modules: list[Module] = []

    def subscribe(self, event: str,
                  handler: Callable[[EventContext], Awaitable[None]],
                  priority: int = 100,
                  module_name: str = "") -> None:
        """Register handler for event. Lower priority runs first."""

    async def emit(self, event: str, data: dict[str, Any]) -> EventContext:
        """Dispatch event to all handlers, grouped by priority.
        Within same priority: concurrent via asyncio.gather(return_exceptions=True).
        Across priorities: sequential (lower first).
        Returns EventContext with veto state.
        """

    async def startup(self) -> None:
        """Call module.startup() for all registered modules in order."""

    async def shutdown(self) -> None:
        """Call module.shutdown() for all modules in reverse order."""
```

#### Module Protocol

```python
class Module(Protocol):
    @property
    def name(self) -> str: ...

    async def startup(self, bus: ModuleBus) -> None: ...
    async def shutdown(self) -> None: ...
```

#### Event Dispatch Flow

```
emit("agent:pre_tool", data)
  │
  ├── Group handlers by priority: {10: [h1, h2], 100: [h3], 200: [h4]}
  │
  ├── Priority 10: asyncio.gather(h1(ctx), h2(ctx), return_exceptions=True)
  │     └── Each wrapped with asyncio.wait_for(timeout=30s)
  │
  ├── Priority 100: asyncio.gather(h3(ctx), return_exceptions=True)
  │
  ├── Priority 200: asyncio.gather(h4(ctx), return_exceptions=True)
  │
  └── Return EventContext (caller checks is_vetoed)
```

#### ArcRun Bridge

```python
def create_arcrun_bridge(bus: ModuleBus) -> Callable:
    """Returns on_event callback for arcrun.run().
    Maps ArcRun events to Module Bus events:
      tool.start  → agent:pre_tool
      tool.end    → agent:post_tool
      turn.start  → agent:pre_plan
      turn.end    → agent:post_plan
      llm.call    → (telemetry only, no bus event)
    """
```

#### Events

| Event | When | Veto-able | Data |
|-------|------|-----------|------|
| `agent:init` | After all components initialized | No | `{config}` |
| `agent:pre_plan` | Before LLM planning turn | Yes | `{messages, turn}` |
| `agent:post_plan` | After LLM response received | No | `{response, turn}` |
| `agent:pre_tool` | Before tool execution | Yes | `{tool, args}` |
| `agent:post_tool` | After tool execution | No | `{tool, result, duration}` |
| `agent:pre_respond` | Before sending final response | Yes | `{response}` |
| `agent:post_respond` | After response sent | No | `{response}` |
| `agent:compact` | Context compaction triggered | No | `{before_tokens, after_tokens}` |
| `agent:error` | Error occurred | No | `{error, component}` |
| `agent:shutdown` | Shutdown initiated | No | `{}` |

### 2.5 Tool Registry (tool_registry.py)

**Purpose**: Register tools from 4 transports, apply policy, wrap with audit, produce `list[arcrun.Tool]`.

#### Key Classes

```python
class ToolTransport(Enum):
    NATIVE = "native"
    MCP = "mcp"
    HTTP = "http"
    PROCESS = "process"


@dataclass
class RegisteredTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    transport: ToolTransport
    execute: Callable[..., Awaitable[Any]]
    timeout_seconds: int = 30
    source: str = ""  # MCP server name, module path, URL, etc.


class ToolRegistry:
    def __init__(self, config: ToolsConfig, bus: ModuleBus,
                 telemetry: AgentTelemetry) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._policy = config.policy
        self._bus = bus
        self._telemetry = telemetry

    def register(self, tool: RegisteredTool) -> None:
        """Register tool. Checks policy allowlist/denylist."""

    async def discover_mcp_tools(self, servers: dict[str, MCPServerEntry]) -> None:
        """Connect to MCP servers, list tools, register each.
        Uses mcp SDK: stdio_client → ClientSession → list_tools().
        """

    def register_native_tools(self, tools: dict[str, NativeToolEntry]) -> None:
        """Import and register Python function tools."""

    async def register_http_tools(self, tools: dict[str, HTTPToolEntry]) -> None:
        """Register HTTP-based tools with httpx client."""

    def register_process_tools(self, tools: dict[str, ProcessToolEntry]) -> None:
        """Register subprocess-based tools."""

    def to_arcrun_tools(self) -> list:
        """Convert all registered tools to arcrun.Tool objects.
        Each tool.execute is wrapped to:
        1. Emit agent:pre_tool (check veto)
        2. Enforce timeout
        3. Execute actual tool
        4. Emit agent:post_tool
        5. Audit event
        """

    async def shutdown(self) -> None:
        """Close MCP connections, httpx clients."""
```

#### Tool Wrapping

Every tool's execute function is wrapped:

```python
async def _wrapped_execute(tool: RegisteredTool, args: dict) -> Any:
    # 1. Pre-tool event (may veto)
    ctx = await bus.emit("agent:pre_tool", {"tool": tool.name, "args": args})
    if ctx.is_vetoed:
        raise ToolVetoedError(tool.name, ctx.veto_reason)

    # 2. Execute with timeout
    async with telemetry.tool_span(tool.name, args):
        result = await asyncio.wait_for(
            tool.execute(**args),
            timeout=tool.timeout_seconds
        )

    # 3. Post-tool event
    await bus.emit("agent:post_tool", {
        "tool": tool.name, "result": result, "duration": elapsed
    })

    # 4. Audit
    telemetry.audit_event("tool.executed", {
        "tool": tool.name, "transport": tool.transport.value
    })

    return result
```

#### MCP Integration

```python
async def _connect_mcp_server(self, name: str, entry: MCPServerEntry) -> None:
    """Uses official mcp SDK v1.26.0:
    - StdioServerParameters for command/args/env
    - stdio_client context manager for transport
    - ClientSession for protocol
    - list_tools() for discovery
    - call_tool(name, args) wrapped as RegisteredTool.execute
    """
```

### 2.6 Context Manager (context_manager.py)

**Purpose**: Assemble system prompt, monitor token budget, prune and compact conversation history.

#### Key Classes

```python
class ContextManager:
    def __init__(self, config: ContextConfig, telemetry: AgentTelemetry) -> None:
        self._config = config
        self._telemetry = telemetry
        self._estimated_tokens: int = 0
        self._reported_tokens: int = 0

    def assemble_system_prompt(self, workspace: Path) -> str:
        """Build system prompt from workspace files:
        1. identity.md (WHO the agent is — read-only)
        2. policy.md (HOW the agent behaves — agent R/W)
        3. context.md (WHAT the agent knows — working memory)
        Returns concatenated content with section headers.
        """

    def estimate_tokens(self, text: str) -> int:
        """Client-side token estimation with conservative 1.1x multiplier.
        Uses character-based heuristic (~4 chars per token) as baseline.
        """

    def update_reported_usage(self, usage) -> None:
        """Update from LLMResponse.usage (provider-reported, accurate).
        Tracks input_tokens, output_tokens, cache_read/creation.
        """

    def transform_context(self, messages: list) -> list:
        """Callback for arcrun.run(transform_context=...).
        Called before each LLM call. Applies token management:
        1. Estimate current token usage
        2. If > prune_threshold: mask old tool outputs
        3. If > compact_threshold: flag for compaction
        4. If > emergency_threshold: force truncation
        Returns modified messages list.
        """

    def _prune_observations(self, messages: list) -> list:
        """Observation masking (JetBrains Research):
        Replace old tool outputs with '[output pruned — {n} tokens]'
        while preserving tool call metadata and reasoning.
        Protects recent 40K tokens from pruning.
        """

    async def compact(self, messages: list, model) -> list:
        """LLM-based summarization when pruning is exhausted.
        Emits agent:compact event via Module Bus.
        """
```

#### Token Budget States

```
0% ──────── 70% ──────── 85% ──────── 95% ──── 100%
   normal     prune        compact       emergency
              (mask old    (LLM          (force
              tool         summarize)    truncate)
              outputs)
```

### 2.7 Agent Orchestrator (agent.py)

**Purpose**: Wire all components, prepare inputs for `arcrun.run()`, process outputs.

#### Key Class

```python
class ArcAgent:
    def __init__(self, config: ArcAgentConfig) -> None:
        self._config = config
        # Components initialized in startup()
        self._telemetry: AgentTelemetry
        self._identity: AgentIdentity
        self._bus: ModuleBus
        self._tool_registry: ToolRegistry
        self._context: ContextManager
        self._vault_resolver: VaultResolver | None = None

    async def startup(self) -> None:
        """Initialize components in dependency order:
        1. Config (already loaded in __init__)
        2. Vault resolver (from ArcLLM, if vault.backend configured)
        3. Telemetry
        4. Identity (uses vault resolver)
        5. Module Bus
        6. Tool Registry (discovers MCP tools)
        7. Context Manager
        8. Emit agent:init event
        """

    async def run(self, task: str) -> Any:
        """Execute a single task:
        1. Load LLM model via arcllm.load_model(config.llm.model)
        2. Build tool list via tool_registry.to_arcrun_tools()
        3. Assemble system prompt via context_manager
        4. Create ArcRun bridge via module_bus
        5. Call arcrun.run(model, tools, system, task,
                          on_event=bridge, transform_context=...)
        6. Process LoopResult
        7. Emit agent:post_respond
        """

    async def shutdown(self) -> None:
        """Reverse-order teardown:
        1. Emit agent:shutdown
        2. Module Bus shutdown (modules in reverse)
        3. Tool Registry shutdown (close MCP connections)
        4. Telemetry flush
        """
```

#### Startup/Shutdown Sequence

```
Startup:                          Shutdown:
  config ──▶                      ◀── telemetry.flush()
  vault_resolver ──▶              ◀── tool_registry.shutdown()
  telemetry ──▶                   ◀── bus.shutdown() (modules reverse)
  identity ──▶                    ◀── emit agent:shutdown
  module_bus ──▶
  tool_registry ──▶
  context_manager ──▶
  emit agent:init ──▶
```

---

## 3. Integration Patterns

### 3.1 ArcLLM Integration

```python
import arcllm

# Load model via ArcLLM registry
model = arcllm.load_model(config.llm.model)

# ArcLLM's OTel spans auto-nest under ArcAgent's active span
# via standard OTel context propagation (no explicit wiring needed)

# Reuse ArcLLM's VaultResolver for secret management
from arcllm import VaultResolver
vault = VaultResolver.from_config(config.vault.backend, config.vault.cache_ttl_seconds)
```

### 3.2 ArcRun Integration

```python
import arcrun

# Prepare bridge callbacks
on_event = create_arcrun_bridge(bus)
transform = context_manager.transform_context

# ArcRun IS the loop
result = await arcrun.run(
    model=model,
    tools=tool_registry.to_arcrun_tools(),
    system_prompt=context_manager.assemble_system_prompt(workspace),
    task=task_message,
    on_event=on_event,
    transform_context=transform,
)

# Process result
# result.messages, result.usage, result.tool_calls, etc.
```

### 3.3 Vault Integration (Reuses ArcLLM)

```python
# ArcAgent shares ArcLLM's VaultResolver
# Same [vault] config section, different vault_path values

# Identity keys:
#   vault_path = "secret/data/arcagent/identity/{did}"
#   Fallback: file at config.identity.key_dir / {did}.key

# No new vault implementation. Import and reuse.
```

---

## 4. Error Handling

### 4.1 Error Hierarchy

```python
class ArcAgentError(Exception):
    """Base for all ArcAgent errors."""
    code: str
    component: str

class ConfigError(ArcAgentError):
    """TOML parse or Pydantic validation failure."""
    code = "CONFIG_*"

class IdentityError(ArcAgentError):
    """Key generation, signing, or verification failure."""
    code = "IDENTITY_*"

class ToolError(ArcAgentError):
    """Tool execution, timeout, or policy violation."""
    code = "TOOL_*"

class ToolVetoedError(ToolError):
    """Tool execution vetoed by pre_tool handler."""
    code = "TOOL_VETOED"

class ContextError(ArcAgentError):
    """Token budget exceeded, compaction failure."""
    code = "CONTEXT_*"

class ModuleBusError(ArcAgentError):
    """Handler failure, timeout, lifecycle error."""
    code = "MODULE_*"
```

### 4.2 Error Flow

All errors → audit event → telemetry span → `agent:error` bus event → structured log.

Handler errors are isolated: one handler failure doesn't crash others, doesn't crash the bus.

---

## 5. Security Considerations (Phase 1)

| Concern | Mitigation |
|---------|-----------|
| Key storage (dev mode) | File permissions 0600, key_dir permissions 0700 |
| Tool policy | Config-driven allow/deny lists checked before registration |
| MCP server trust | Sandbox subprocess, deny-by-default tool access |
| Secret leakage | `SecretStr` for in-memory key material, never log keys |
| Audit completeness | Every tool call, every LLM request, every error is an audit event |
| Handler isolation | 30s timeout, exception capture, no shared mutable state |

---

## 6. File Map

```
arcagent/
├── arcagent/
│   ├── __init__.py              # MODIFY: Export ArcAgent class
│   └── core/
│       ├── __init__.py          # NEW: Core package init
│       ├── config.py            # NEW: TOML + Pydantic config
│       ├── identity.py          # NEW: DID, Ed25519, vault fallback
│       ├── telemetry.py         # NEW: OTel spans, audit events
│       ├── module_bus.py        # NEW: Async event system
│       ├── tool_registry.py     # NEW: 4 transports, policy, wrapping
│       ├── context_manager.py   # NEW: System prompt, token mgmt
│       ├── agent.py             # NEW: Orchestrator
│       └── errors.py            # NEW: Error hierarchy
├── tests/
│   └── unit/
│       └── core/
│           ├── test_config.py        # NEW
│           ├── test_identity.py      # NEW
│           ├── test_telemetry.py     # NEW
│           ├── test_module_bus.py    # NEW
│           ├── test_tool_registry.py # NEW
│           ├── test_context_manager.py # NEW
│           └── test_agent.py         # NEW
└── arcagent.toml.example        # NEW: Example config
```
