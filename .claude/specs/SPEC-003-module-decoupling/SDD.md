# SDD: Module Decoupling

**Spec**: SPEC-003
**Status**: PENDING

## Architecture

### Before

```
core/config.py
  ├── MemoryConfig          ← module-specific, shouldn't be here
  ├── SchedulerConfig       ← module-specific, shouldn't be here
  └── ArcAgentConfig
        ├── memory: MemoryConfig
        └── scheduler: SchedulerConfig

core/errors.py
  ├── AgentMemoryError      ← module-specific
  ├── EntityExtractionError ← module-specific
  ├── SearchError           ← module-specific
  └── PolicyEvalError       ← module-specific

core/context_manager.py
  ├── _PROMPT_FILES = ["identity.md", "policy.md", "context.md"]   ← hardcoded
  └── _SECTION_ORDER = ["identity", "notes", "memory_guidance", ...] ← hardcoded

modules/memory/
  ├── markdown_memory.py    ← contains PolicyEngine integration
  └── policy_engine.py      ← should be its own module
```

### After

```
core/config.py
  └── ArcAgentConfig
        └── modules: dict[str, ModuleEntry]  ← already exists, now sole path

core/errors.py
  └── (only core errors: Config, Identity, Tool, Context, ModuleBus, Session, Skill, Extension, Settings)

core/context_manager.py
  ├── _CORE_PROMPT_FILES = ["identity.md", "context.md"]
  └── _ordered_sections()  ← identity first, context last, rest sorted

core/module_loader.py
  └── _instantiate() passes ModuleEntry.config dict, not getattr(root, name)

modules/memory/
  ├── config.py             ← MemoryConfig (owned by module)
  ├── errors.py             ← AgentMemoryError, EntityExtractionError, SearchError
  ├── markdown_memory.py    ← no policy references
  └── (no policy_engine.py)

modules/policy/              ← NEW MODULE
  ├── __init__.py
  ├── config.py             ← PolicyConfig
  ├── errors.py             ← PolicyEvalError
  ├── policy_engine.py      ← moved from memory, uses PolicyConfig
  ├── policy_module.py      ← bus subscriber (new)
  └── MODULE.yaml

modules/scheduler/
  ├── config.py             ← SchedulerConfig (owned by module)
  └── (existing files updated to import from local config)
```

## Component Design

### C1: Module Config Pattern (module_loader.py)

**Change**: `_instantiate()` passes module-specific config from `ModuleEntry.config` dict instead of `getattr(ctx.config, manifest.name)`.

```python
# Before
available = {
    "config": getattr(ctx.config, manifest.name, None),
    ...
}

# After
module_entry = ctx.config.modules.get(manifest.name)
available = {
    "config": module_entry.config if module_entry else None,
    ...
}
```

Module constructors change from typed config to dict + internal validation:

```python
# Before
def __init__(self, config: MemoryConfig, ...):
    self._config = config

# After
def __init__(self, config: dict[str, Any] | None = None, ...):
    from arcagent.modules.memory.config import MemoryConfig
    self._config = MemoryConfig(**(config or {}))
```

**TOML migration**:
```toml
# Before
[memory]
context_budget_tokens = 2000

# After
[modules.memory]
enabled = true
[modules.memory.config]
context_budget_tokens = 2000
```

### C2: PolicyModule (new bus subscriber)

Extracted from `MarkdownMemoryModule._on_post_respond` and `_on_shutdown`.

**Responsibilities**:
- Subscribe to `agent:post_respond` — periodic ACE evaluation (every N turns)
- Subscribe to `agent:assemble_prompt` — inject `policy.md` into system prompt
- Subscribe to `agent:shutdown` — final policy evaluation
- Background task management for async eval calls

**Constructor** (DI via module_loader):
- `config: dict[str, Any] | None` — validated as `PolicyConfig`
- `eval_config: EvalConfig` — shared eval model config
- `telemetry: Any` — audit events
- `workspace: Path` — for policy.md location
- `llm_config: Any | None` — fallback model config

**Bus event subscriptions** (set during `startup(ctx)`):
| Event | Priority | Purpose |
|-------|----------|---------|
| `agent:post_respond` | 110 | Periodic policy evaluation (after memory at 100) |
| `agent:assemble_prompt` | 60 | Inject policy.md content |
| `agent:shutdown` | 60 | Final session evaluation |

### C3: PolicyEngine Updates

Moved from `modules/memory/policy_engine.py` to `modules/policy/policy_engine.py`.

**Changes**:
- Import `PolicyConfig` from local config instead of `MemoryConfig` from core
- Constructor takes `PolicyConfig` instead of `MemoryConfig`
- `_MAX_POLICY_BULLETS` and `_MAX_BULLET_TEXT_LENGTH` read from config instead of module constants

### C4: MarkdownMemoryModule Cleanup

**Removed**:
- `PolicyEngine` import and instantiation
- `self._session_messages` field (only used for policy)
- `self._turn_count` field (only used for policy eval interval)
- `_on_shutdown` bus handler (only did policy eval)
- `agent:shutdown` subscription
- Policy evaluation block in `_on_post_respond`
- `policy_eval_interval_turns` from config

**Kept**:
- Entity extraction in `_on_post_respond`
- `_get_eval_model()` (used by entity extraction)
- `_spawn_background()` (used by entity extraction)
- `_on_pre_compaction` handler
- `shutdown()` Module protocol method (background task cleanup)

### C5: Dynamic Prompt Assembly (context_manager.py)

**Before**:
```python
_PROMPT_FILES = ["identity.md", "policy.md", "context.md"]
_SECTION_ORDER = ["identity", "notes", "memory_guidance", "skills", "policy", "context"]
```

**After**:
```python
_CORE_PROMPT_FILES = ["identity.md", "context.md"]
# No _SECTION_ORDER constant — dynamic ordering
```

Assembly logic:
1. Read core files (`identity.md`, `context.md`) into sections dict
2. Emit `agent:assemble_prompt` — modules inject their sections
3. Order: `identity` first, `context` last, everything else alphabetically

### C6: Core Errors Cleanup

**Removed from `core/errors.py`**:
- `AgentMemoryError`
- `EntityExtractionError`
- `SearchError`
- `PolicyEvalError`

**Kept** (core component errors):
- `ArcAgentError` (base)
- `ConfigError`, `IdentityError`, `ToolError`, `ToolVetoedError`
- `ContextError`, `ModuleBusError`, `SessionError`
- `SkillError`, `ExtensionError`, `SettingsError`

## Data Flow

### Module Loading (updated)

```
arcagent.toml
  └── [modules.memory]
        ├── enabled = true
        └── [modules.memory.config]
              └── context_budget_tokens = 2000
                        ↓
ArcAgentConfig.modules["memory"].config → {"context_budget_tokens": 2000}
                        ↓
module_loader._instantiate()
  available["config"] = {"context_budget_tokens": 2000}
                        ↓
MarkdownMemoryModule.__init__(config={"context_budget_tokens": 2000})
  self._config = MemoryConfig(**config)  → validated Pydantic model
```

### Prompt Assembly (updated)

```
context_manager.assemble_system_prompt()
  1. Read identity.md → sections["identity"]
  2. Read context.md → sections["context"]
  3. Emit agent:assemble_prompt(sections)
     ├── memory module (priority 50): inject "notes", "memory_guidance"
     ├── policy module (priority 60): inject "policy"
     └── skill_registry (priority 90): inject "skills"
  4. Order: identity → memory_guidance → notes → policy → skills → context
     (alphabetical between identity and context)
```

## Files Changed

### New Files (9)
| File | Purpose |
|------|---------|
| `modules/memory/config.py` | MemoryConfig (module-owned) |
| `modules/memory/errors.py` | Memory error hierarchy |
| `modules/policy/__init__.py` | Policy module exports |
| `modules/policy/config.py` | PolicyConfig |
| `modules/policy/errors.py` | PolicyEvalError |
| `modules/policy/policy_engine.py` | Moved from memory, updated imports |
| `modules/policy/policy_module.py` | Bus subscriber (new) |
| `modules/policy/MODULE.yaml` | Module manifest |
| `modules/scheduler/config.py` | SchedulerConfig (module-owned) |

### Modified Files (~18)
| File | Change |
|------|--------|
| `core/config.py` | Remove MemoryConfig, SchedulerConfig, remove from ArcAgentConfig |
| `core/errors.py` | Remove 4 memory/policy error classes |
| `core/context_manager.py` | Dynamic sections, remove hardcoded policy.md |
| `core/module_loader.py` | Pass ModuleEntry.config dict, clean comments |
| `core/protocols.py` | Clean docstring references |
| `core/agent.py` | Clean memory-specific comments |
| `modules/memory/__init__.py` | Remove policy exports |
| `modules/memory/markdown_memory.py` | Remove all policy code, use local config |
| `modules/memory/hybrid_search.py` | Import from local config |
| `modules/scheduler/__init__.py` | Import from local config |
| `modules/scheduler/scheduler.py` | Import from local config |
| `modules/scheduler/tools.py` | Import from local config |
| Tests (6+ files) | Update imports |

### Deleted Files (1)
| File | Reason |
|------|--------|
| `modules/memory/policy_engine.py` | Moved to `modules/policy/` |

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Import path breakage | Grep all `from arcagent.core.config import MemoryConfig` and update |
| Test failures from moved classes | Update all test imports; verify with full test run |
| TOML config migration | Document new `[modules.X.config]` pattern |
| Circular imports | Module configs import only from `pydantic`; errors import only `ArcAgentError` from core |
| Section ordering change | Alphabetical sorting produces same order: memory_guidance, notes, policy, skills (matches current explicit order minus identity/context bookends) |
