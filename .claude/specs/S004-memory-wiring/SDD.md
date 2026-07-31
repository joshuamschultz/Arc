# SDD: Memory Wiring

**Spec ID**: S004
**Status**: PENDING

---

## 1. Architecture Overview

This spec wires up 5 disconnected gaps in the existing memory system and introduces 4 architectural refinements. No new memory logic is created — only wiring, DI improvements, and trigger mechanisms.

### Component Interaction (After Wiring)

```
startup():
  Agent creates → ModuleContext(bus, tool_registry, config, telemetry, workspace, llm_config)
                       ↓
  ConventionLoader scans → arcagent/modules/*/MODULE.yaml
                       ↓
  For each enabled module → import entry_point → module.startup(ctx)
                       ↓
  MarkdownMemoryModule.startup(ctx):
    ├── Subscribes to bus events (pre_tool, post_tool, assemble_prompt, post_respond)
    ├── Registers memory_search tool via ctx.tool_registry
    └── Stores ctx.llm_config for eval model fallback

chat():
  Session.append_message(user_msg)
    ↓
  _execute_loop() → arcrun → Module Bus events fire
    ↓
  post_respond → _on_post_respond():
    ├── lazy-init eval model (MW-001, MW-012)
    ├── entity extraction (async, semaphore-limited)
    └── policy evaluation (periodic)
    ↓
  Session.append_message(assistant_msg)
    ↓
  Session.check_compaction():
    ├── context ratio > 0.85? → compact()
    │   ├── pre-flush heuristics (MAGMA fast path)
    │   ├── LLM summarization (slow path, 5s timeout)
    │   └── replace oldest 30% with summary
    └── context ratio < 0.85? → no-op
```

## 2. New Components

### 2.1 ModuleContext (MW-004)

**File**: `arcagent/core/module_bus.py` (add to existing file)

```python
@dataclass(frozen=True)
class ModuleContext:
    """Dependency injection container for module startup."""
    bus: ModuleBus
    tool_registry: Any  # ToolRegistry (avoid circular import)
    config: ArcAgentConfig
    telemetry: Any  # AgentTelemetry
    workspace: Path
    llm_config: LLMConfig
```

- Frozen: modules cannot mutate shared references
- `llm_config` enables eval model fallback (MW-012)
- References are immutable but the objects themselves are mutable (modules can call `tool_registry.register()`)

### 2.2 Convention Module Loader (MW-007)

**File**: `arcagent/core/module_loader.py` (NEW, ~120 LOC)

```python
class ModuleManifest(BaseModel):
    """Validated MODULE.yaml schema."""
    name: str                           # Required - fail if missing
    entry_point: str                    # Required - fail if missing
    version: str = "0.0.0"             # Optional - warn if missing
    description: str = ""              # Optional
    dependencies: list[str] = []       # Optional
    events: dict[str, list[str]] = {}  # Optional

class ModuleLoader:
    """Convention-based module discovery and loading."""

    def discover(self, modules_dir: Path, config: ArcAgentConfig) -> list[ModuleManifest]:
        """Scan modules/*/MODULE.yaml, validate, filter by config allowlist."""

    def load(self, manifest: ModuleManifest, ctx: ModuleContext) -> Module:
        """Import entry_point, instantiate with ModuleContext."""

    def load_all(self, modules_dir: Path, ctx: ModuleContext) -> list[Module]:
        """Discover + load all enabled modules."""
```

**Error handling** (MW-007 deepening):
- Missing `name` or `entry_point` → `ConfigError`, fail startup
- Missing optional fields → `WARNING` log, use defaults
- Import failure → `ModuleLoadError`, log and skip (don't crash agent for one bad module)
- MODULE.yaml parse error → `ConfigError` with file path in message

### 2.3 Compaction Trigger in chat() (MW-006)

**File**: `arcagent/core/agent.py` (modify `chat()` method)

After appending the assistant message, check context ratio:

```python
async def chat(self, message: str, *, session_id: str | None = None) -> Any:
    # ... existing code ...
    await session.append_message({"role": "assistant", "content": response_text})

    # NEW: Check compaction threshold
    await self._maybe_compact(session)
    return result

async def _maybe_compact(self, session: SessionManager) -> None:
    """Trigger compaction if context ratio exceeds compact_threshold."""
    context = self._context
    if context is None:
        return
    ratio = context.token_ratio()
    if ratio >= self._config.context.compact_threshold:
        eval_model = self._get_eval_model()
        await session.compact(eval_model, self._workspace)
```

**Race condition protection** (OpenClaw bug #5457): Session's `compact()` method already uses `asyncio.Lock`. The `_maybe_compact` call is sequential within `chat()`, not concurrent.

## 3. Modifications to Existing Components

### 3.1 Module Protocol Change (MW-004)

**File**: `arcagent/core/module_bus.py`

```python
# BEFORE
class Module(Protocol):
    @property
    def name(self) -> str: ...
    async def startup(self, bus: ModuleBus) -> None: ...
    async def shutdown(self) -> None: ...

# AFTER
class Module(Protocol):
    @property
    def name(self) -> str: ...
    async def startup(self, ctx: ModuleContext) -> None: ...
    async def shutdown(self) -> None: ...
```

**ModuleBus.startup()** must pass ModuleContext instead of self:

```python
async def startup(self, ctx: ModuleContext) -> None:
    for module in self._modules:
        await module.startup(ctx)
```

This requires ModuleBus.startup() signature to change from `startup(self)` to `startup(self, ctx: ModuleContext)`.

### 3.2 MarkdownMemoryModule Changes

**File**: `arcagent/modules/memory/markdown_memory.py`

#### 3.2.1 Eval Model Lazy Init (MW-001, MW-012)

```python
def _get_eval_model(self) -> Any:
    """Lazy-init eval model from config, fallback to agent's LLM config."""
    if self._eval_model is not None:
        return self._eval_model

    eval_cfg = self._eval_config
    if eval_cfg.provider and eval_cfg.model:
        model_id = f"{eval_cfg.provider}/{eval_cfg.model}"
    else:
        # Fallback to agent's LLM config (MW-012)
        model_id = self._llm_config.model

    self._eval_model = _load_model(model_id)
    return self._eval_model
```

The `_on_post_respond` method changes:
```python
# BEFORE
model = self._eval_model
if model is None:
    return

# AFTER
model = self._get_eval_model()
```

#### 3.2.2 startup() Signature (MW-004)

```python
# BEFORE
async def startup(self, bus: ModuleBus) -> None:
    bus.subscribe(...)

# AFTER
async def startup(self, ctx: ModuleContext) -> None:
    ctx.bus.subscribe(...)
    self._llm_config = ctx.llm_config
    # Register memory_search tool (MW-003)
    self._register_search_tool(ctx.tool_registry)
```

#### 3.2.3 Constructor Simplification

With ModuleContext, the constructor no longer needs all params passed individually. It receives them from ModuleContext during startup:

```python
def __init__(self, config: MemoryConfig, workspace: Path) -> None:
    # Only config and workspace needed at construction
    # eval_config, telemetry, llm_config come via ModuleContext.startup()
```

Wait — the loader needs to construct the module before calling startup(). The module manifest provides the entry_point class. Construction needs minimal args; startup() provides the rest via ModuleContext.

Revised approach:
- Constructor takes `config: MemoryConfig` and `workspace: Path` (from ModuleContext fields during load)
- `startup(ctx)` stores references to `ctx.bus`, `ctx.telemetry`, `ctx.llm_config`, registers tools and hooks
- Internal helpers (EntityExtractor, PolicyEngine) initialized in startup with ctx references

#### 3.2.4 memory_search Tool Registration (MW-003)

```python
def _register_search_tool(self, tool_registry: Any) -> None:
    """Register memory_search as a callable tool."""
    from arcagent.core.tool_registry import Tool

    tool = Tool(
        name="memory_search",
        description="Search agent memory across notes, entities, and context.",
        parameters={
            "query": {"type": "string", "description": "Search query", "required": True},
            "scope": {"type": "string", "description": "Filter: notes, entities, context", "required": False},
            "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)", "required": False},
            "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)", "required": False},
        },
        handler=self._handle_memory_search,
    )
    tool_registry.register(tool)
```

#### 3.2.5 Memory Guidance Injection (MW-009)

In `_on_assemble_prompt`:

```python
async def _on_assemble_prompt(self, ctx: EventContext) -> None:
    sections = ctx.data.get("sections", {})

    # Inject notes (existing)
    notes_content = await self._notes.get_recent_notes()
    if notes_content:
        sections["notes"] = notes_content

    # NEW: Inject memory guidance if not overridden in identity.md
    identity_content = sections.get("identity", "")
    if "## Memory" not in identity_content:
        sections["memory_guidance"] = self._default_memory_guidance()
```

Default guidance text:
```
## Memory

You have persistent memory across sessions. Your memory has three tiers:

1. **Working Memory** (context.md) — Edit freely to track current state, key facts, and reminders
2. **Daily Notes** (notes/) — Append observations and session logs. These are append-only.
3. **Entity Knowledge** (entities/) — Automatically extracted. Search with memory_search.

Use `memory_search` to find past conversations, entities, or notes.
Use `write` and `edit` tools to update context.md with important information.
Use `write` to append to today's notes (notes/YYYY-MM-DD.md).
```

### 3.3 HybridSearch Date Filtering (MW-011)

**File**: `arcagent/modules/memory/hybrid_search.py`

Add `date_from` and `date_to` parameters to `search()`:

```python
async def search(
    self,
    query: str,
    top_k: int = 10,
    scope: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[SearchResult]:
```

FTS5 schema update (add UNINDEXED column):
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    source,
    created_date UNINDEXED,
    source_path UNINDEXED
);
```

Query with date filter:
```sql
SELECT * FROM memory_fts
WHERE memory_fts MATCH ?
AND (created_date IS NULL OR created_date BETWEEN ? AND ?)
```

### 3.4 Agent.py Startup Changes (MW-007, MW-008)

**File**: `arcagent/core/agent.py`

Replace step 11 (`_register_modules`) with convention loader:

```python
# 11. Convention-based module loading (replaces _register_modules)
from arcagent.core.module_loader import ModuleLoader

loader = ModuleLoader()
modules_dir = Path(__file__).parent.parent / "modules"
module_ctx = ModuleContext(
    bus=self._bus,
    tool_registry=self._tool_registry,
    config=self._config,
    telemetry=self._telemetry,
    workspace=workspace,
    llm_config=self._config.llm,
)
loaded = loader.load_all(modules_dir, module_ctx)
for mod in loaded:
    self._bus.register_module(mod)

# 12. Start modules with context
await self._bus.startup(module_ctx)
```

Delete `_register_modules()` method entirely.

### 3.5 Session-Owns-Context Wiring (MW-008)

**File**: `arcagent/core/agent.py`

SessionManager constructor updated to accept ContextManager:

```python
# 6. Context Manager
self._context = ContextManager(
    config=self._config.context,
    telemetry=self._telemetry,
    bus=self._bus,
)

# 7. Session Manager (owns context)
self._session = SessionManager(
    config=self._config.session,
    context_config=self._config.context,
    telemetry=self._telemetry,
    workspace=workspace,
    context_manager=self._context,  # NEW: session owns context
)
```

**File**: `arcagent/core/session_manager.py`

Add `context_manager` parameter:

```python
def __init__(self, ..., context_manager: Any = None) -> None:
    ...
    self._context_manager = context_manager

@property
def context_manager(self) -> Any:
    return self._context_manager
```

This enables the session to check token ratio for compaction:

```python
def token_ratio(self) -> float:
    if self._context_manager is None:
        return 0.0
    return self._context_manager.token_ratio()
```

### 3.6 Config Enablement (MW-010)

**File**: `Basic_Agent/arcagent.toml`

Add:
```toml
[modules.memory]
enabled = true
```

## 4. File Change Summary

```
arcagent/
├── core/
│   ├── module_bus.py           # MODIFY: Add ModuleContext, change Module.startup signature
│   ├── module_loader.py        # NEW: Convention-based module loader (~120 LOC)
│   ├── agent.py                # MODIFY: Replace _register_modules, add _maybe_compact, pass ModuleContext
│   └── session_manager.py      # MODIFY: Accept context_manager param, add token_ratio()
├── modules/
│   └── memory/
│       ├── markdown_memory.py  # MODIFY: startup(ctx), lazy eval model, register tool, inject guidance
│       └── hybrid_search.py    # MODIFY: Add date_from/date_to, FTS5 UNINDEXED column
tests/
├── unit/
│   └── core/
│       ├── test_module_loader.py   # NEW: Convention loader tests
│       └── test_module_context.py  # NEW: ModuleContext tests
├── integration/
│   └── test_memory_wiring.py       # NEW: Full flow integration test
Basic_Agent/
└── arcagent.toml               # MODIFY: Add [modules.memory] enabled = true
```

**Estimated LOC changes**: ~250 new, ~80 modified, ~30 deleted

## 5. Error Handling

| Error | Handler | Recovery |
|-------|---------|----------|
| MODULE.yaml missing entry_point | ModuleLoader raises ConfigError | Startup fails with clear message |
| MODULE.yaml parse error | ModuleLoader raises ConfigError | Startup fails with file path |
| Module import failure | ModuleLoader logs ERROR, skips module | Agent starts without that module |
| Eval model init failure | _get_eval_model catches, logs WARNING | Falls back to skip (MW-012 fallback_behavior) |
| memory_search failure | Tool returns error result | Agent sees error, can retry |
| Compaction during shutdown | Session.compact checks _shutting_down | Skip compaction, preserve messages |
| Pre-flush LLM timeout (>5s) | _pre_compact_flush catches, uses heuristics | Heuristic extraction only |

## 6. Security Considerations

| Concern | Mitigation | Decision |
|---------|-----------|----------|
| Indirect prompt injection via memory guidance | Validate injected content structure (markdown headings only) | MW-009 |
| Memory search scope | Workspace-scoped, no cross-agent leakage | MW-013 |
| PII in memory | Handled at ArcLLM layer, not memory module | MW-014 |
| Module loading from untrusted sources | Config allowlist prevents unauthorized modules | MW-002 |
| Eval model credential exposure | Uses same vault-backed credential path as primary model | MW-012 |

## 7. Testing Strategy

### Unit Tests (MW-016)

| Test File | What It Covers |
|-----------|---------------|
| `test_module_context.py` | ModuleContext creation, frozen immutability, field access |
| `test_module_loader.py` | Discovery, manifest validation, config filtering, error handling |
| `test_memory_wiring.py` | Eval model lazy init, fallback chain, memory_search registration |
| `test_compaction_trigger.py` | chat() triggers compaction at threshold, pre-flush fires |

### Integration Test

| Test | What It Covers |
|------|---------------|
| `test_memory_wiring_integration.py` | Agent startup → convention loader → memory module loaded → chat() → entity extraction fires → memory_search returns results → compaction triggers at threshold |
