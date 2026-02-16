# SDD: Phase 1b — Agent Runtime

**Spec ID**: S003
**Status**: PENDING
**Last Updated**: 2026-02-15

---

## 1. Architecture Overview

S003 adds five capabilities to the existing nucleus without restructuring it:

```
ArcAgent (agent.py — orchestrator)
├── ToolRegistry (tool_registry.py — already exists)
│   ├── Built-in tools: read, write, edit, bash
│   └── NEW: grep, find, ls (tools/)
├── ContextManager (context_manager.py — already exists)
│   └── NEW: Skill list injection via agent:assemble_prompt
├── ModuleBus (module_bus.py — already exists)
│   └── Extension event hooks via on()
├── SessionManager (session_manager.py — from S002)
├── NEW: ExtensionLoader (core/extensions.py)
│   └── Discovers, loads, sandboxes extensions
├── NEW: SkillRegistry (core/skill_registry.py)
│   └── Discovers, caches, formats SKILL.md files
└── NEW: SettingsManager (core/settings_manager.py)
    └── Overlay on frozen config, persists to TOML
```

### Integration Flow

```
startup():
  1. Identity, Telemetry, Bus, ToolRegistry, Context, Session (existing)
  2. Register built-in tools (existing read/write/edit/bash + NEW grep/find/ls)
  3. SettingsManager(config) → overlay initialized
  4. SkillRegistry.discover(workspace, global) → skills cached
  5. ExtensionLoader.discover_and_load(workspace, global, config_paths)
     → factory(ExtensionAPI) → register_tool(), on() calls
  6. ContextManager gets skill_registry reference for prompt injection
  7. Bus.startup() → modules started
  8. Emit agent:init

reload():
  1. ExtensionLoader.clear() → tools deregistered, hooks removed
  2. importlib.invalidate_caches()
  3. SkillRegistry.clear() → skills cache cleared
  4. Re-run steps 2-5 from startup
  5. Emit agent:extensions_loaded, agent:skills_loaded

run()/chat():
  → ContextManager.assemble_system_prompt() now includes skill list
  → ToolRegistry.to_arcrun_tools() now includes extension-registered tools
```

---

## 2. Component Designs

### 2.1 Extension System (`core/extensions.py`, ~300 LOC)

#### Data Structures

```python
@dataclass
class ExtensionManifest:
    """Metadata about a loaded extension."""
    name: str              # Module name (e.g., "custom_tools")
    source: str            # File path or entry_point name
    sandbox_mode: str      # "workspace" | "paths" | "strict"
    tools_registered: list[str]
    hooks_registered: list[str]
    load_time_ms: float

class ExtensionAPI:
    """API surface exposed to extension factory functions."""
    def __init__(
        self,
        tool_registry: ToolRegistry,
        bus: ModuleBus,
        workspace: Path,
        sandbox_mode: str,
    ) -> None: ...

    def register_tool(self, tool: RegisteredTool) -> None:
        """Register a tool. Validates sandbox_mode constraints."""

    def on(self, event: str, handler: Callable) -> None:
        """Subscribe to a Module Bus event."""

    @property
    def workspace(self) -> Path:
        """Read-only access to workspace path."""
```

#### ExtensionLoader

```python
class ExtensionLoader:
    """Discover, load, and sandbox extensions."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        bus: ModuleBus,
        telemetry: AgentTelemetry,
        config: ExtensionConfig,
    ) -> None: ...

    async def discover_and_load(
        self, workspace: Path, global_dir: Path
    ) -> list[ExtensionManifest]: ...

    def clear(self) -> None:
        """Deregister all extension-registered tools and hooks."""
```

#### Discovery Order

1. `{workspace}/extensions/*.py` — per-agent extensions
2. `~/.arcagent/extensions/*.py` — global extensions
3. Config-specified paths: `[extensions] paths = [...]`
4. Entry points: `importlib.metadata.entry_points(group="arcagent.extensions")`

#### Loading Protocol

```python
# Extension file (workspace/extensions/custom.py):
def extension(api):
    """Factory function. Receives ExtensionAPI, registers tools/hooks."""
    api.register_tool(RegisteredTool(
        name="hello",
        description="Say hello",
        input_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        transport=ToolTransport.NATIVE,
        execute=lambda name="world": f"Hello, {name}!",
        source="extension:custom",
    ))
    api.on("agent:post_respond", my_handler)
```

Loading steps per file:
1. `importlib.import_module()` from spec (not exec/eval)
2. Locate factory: `getattr(module, "extension")` (must exist)
3. Create `ExtensionAPI` with appropriate sandbox mode
4. Call `factory(api)` — tools and hooks registered
5. Record `ExtensionManifest` (tools registered, hooks registered, timing)
6. Audit event via telemetry

#### Sandbox Modes

| Mode | Allowed | Blocked |
|------|---------|---------|
| `workspace` (default) | Full access within workspace | Paths outside workspace |
| `paths` | Access to workspace + configured additional paths | Everything else |
| `strict` | register_tool(), on(), workspace path read-only | open(), Path.read_text(), subprocess, urllib, httpx |

**Strict mode implementation**: Module-level patching within the extension's namespace. Block `builtins.open`, `pathlib.Path.read_text`, `subprocess.run`, `subprocess.Popen`, and common HTTP clients. Applied before factory call, removed after.

#### Error Isolation

Each extension loads in a try/except. A failing extension:
- Logs the error with full traceback
- Emits audit event (`extension.load_failed`)
- Does not prevent other extensions from loading
- Does not crash the agent

#### Hot Reload

`ExtensionLoader.clear()`:
1. Remove all extension-registered tools from ToolRegistry (tracked by source prefix `extension:`)
2. Remove all extension-registered bus handlers (tracked by module_name prefix `ext:`)
3. Call `importlib.invalidate_caches()`
4. Re-run full discovery

### 2.2 Skill Registry (`core/skill_registry.py`, ~150 LOC)

#### Data Structures

```python
@dataclass
class SkillMeta:
    """Parsed SKILL.md frontmatter — lightweight for prompt injection."""
    name: str               # Required
    description: str        # Required
    version: str
    author: str
    requires: list[str]     # Tool dependencies
    tags: list[str]
    category: str
    file_path: Path         # For on-demand full content read

class SkillRegistry:
    """Discover, cache, and format SKILL.md files for prompt injection."""

    def __init__(self, telemetry: AgentTelemetry) -> None: ...

    def discover(
        self, workspace: Path, global_dir: Path
    ) -> list[SkillMeta]: ...

    def format_for_prompt(self) -> str:
        """XML-formatted skill list for system prompt injection."""

    def get_skill(self, name: str) -> SkillMeta | None: ...

    def clear(self) -> None:
        """Clear cached skills for re-discovery."""

    @property
    def skills(self) -> list[SkillMeta]: ...
```

#### Discovery

1. `{workspace}/skills/*.md` — per-agent skills
2. `{workspace}/skills/_agent-created/*.md` — agent-created skills
3. `~/.arcagent/skills/*.md` — global skills

Each file must have YAML frontmatter between `---` delimiters. Files with parse errors are skipped with a warning (never crash).

#### Frontmatter Parsing

```yaml
---
name: code-review
description: Review code for quality, security, and best practices
version: 1.0.0
author: arcagent
requires: [read, grep, find]
tags: [quality, security]
category: development
---

# Code Review Skill

(Full content loaded on demand via read tool)
```

Parser uses `yaml.safe_load()` on the frontmatter block. Required fields: `name`, `description`. All other fields optional with empty defaults.

#### Prompt Injection

`format_for_prompt()` returns:

```xml
<available-skills>
  <skill name="code-review">Review code for quality, security, and best practices</skill>
  <skill name="test-writer">Generate comprehensive test suites from code analysis</skill>
</available-skills>
```

This is injected into the system prompt by subscribing to `agent:assemble_prompt` and adding a `skills` section to the sections dict. Section ordering in ContextManager's `_SECTION_ORDER` will be extended to include `skills` between `notes` and `policy`.

#### Progressive Disclosure

Only name + description go into the prompt. When the agent needs the full skill content, it uses the existing `read` tool to read the SKILL.md file path. This keeps prompt tokens minimal while making full skill content available on demand.

#### Agent-Created Skills

When the agent writes a new SKILL.md to `workspace/skills/_agent-created/`, a targeted re-scan of that directory adds the skill without full re-discovery. This supports the self-extension pattern where the agent creates its own skills.

### 2.3 Additional Tools (`tools/grep.py`, `tools/find.py`, `tools/ls.py`, ~200 LOC total)

All three tools follow the identical pattern from `tools/read.py`:

```python
def create_tool(workspace: Path) -> RegisteredTool:
    ws = workspace.resolve()
    async def execute(**kwargs: Any) -> str:
        # validate paths with resolve_workspace_path()
        # execute with size limits
        # return formatted results
    return RegisteredTool(
        name="...",
        description="...",
        input_schema=INPUT_SCHEMA,
        transport=ToolTransport.NATIVE,
        execute=execute,
        source="arcagent.tools.{name}",
    )
```

#### grep tool (`tools/grep.py`, ~80 LOC)

| Parameter | Type | Description |
|-----------|------|-------------|
| `pattern` | string (required) | Regex pattern to search for |
| `path` | string | Directory or file to search (default: workspace root) |
| `glob_filter` | string | File glob filter (e.g., `"*.py"`) |
| `max_results` | integer | Maximum matches to return (default: 100) |

Returns: `file_path:line_number: matching_line` format, one per line.

Implementation: `Path.rglob()` for file discovery + `re.search()` per line. Skips binary files (null byte check on first 8KB). Max file size 5MB. Workspace-scoped via `resolve_workspace_path()`.

#### find tool (`tools/find.py`, ~60 LOC)

| Parameter | Type | Description |
|-----------|------|-------------|
| `pattern` | string (required) | Glob pattern (e.g., `"**/*.py"`) |
| `path` | string | Directory to search (default: workspace root) |
| `max_results` | integer | Maximum files to return (default: 200) |

Returns: Matching file paths relative to workspace, sorted by modification time (newest first).

Implementation: `Path.glob(pattern)` with workspace scope. Returns paths relative to workspace root for readability.

#### ls tool (`tools/ls.py`, ~60 LOC)

| Parameter | Type | Description |
|-----------|------|-------------|
| `path` | string | Directory to list (default: workspace root) |

Returns: Directory entries with type indicators:
```
 d  extensions/
 d  skills/
 f  identity.md (1.2 KB)
 f  policy.md (0.8 KB)
```

Implementation: `Path.iterdir()` sorted (dirs first, then files). Shows `d` for directories, `f` for files with size. Workspace-scoped.

#### Tool Scope Expansion

Default: all paths must resolve within workspace. Expandable via `[tools.policy]` config:

```toml
[tools.policy]
allowed_paths = ["/shared/data", "/opt/reference"]
```

The `resolve_workspace_path()` utility will be extended with an optional `allowed_paths` parameter. Tools pass this through from config.

### 2.4 Settings Manager (`core/settings_manager.py`, ~150 LOC)

#### Design Pattern

Overlay on frozen config. The original `ArcAgentConfig` is never mutated.

```python
class SettingsManager:
    """Runtime settings overlay on frozen Pydantic config."""

    # Keys that can be changed at runtime
    MUTABLE_KEYS: ClassVar[dict[str, type]] = {
        "model": str,
        "compaction_threshold": float,
        "tool_timeout": int,
        "log_level": str,
    }

    # Keys blocked from runtime change (security-sensitive)
    BLOCKED_KEYS: ClassVar[set[str]] = {
        "identity", "vault", "keys", "did", "key_dir",
    }

    def __init__(
        self,
        config: ArcAgentConfig,
        telemetry: AgentTelemetry,
        bus: ModuleBus,
        config_path: Path,
    ) -> None:
        self._config = config          # Frozen, never mutated
        self._overlay: dict[str, Any] = {}
        self._telemetry = telemetry
        self._bus = bus
        self._config_path = config_path

    def get(self, key: str) -> Any:
        """Get setting. Overlay first, then config."""
        if key in self._overlay:
            return self._overlay[key]
        return self._resolve_from_config(key)

    async def set(self, key: str, value: Any) -> None:
        """Set runtime override. Validates type, persists to TOML."""
        self._validate_key(key)
        self._validate_type(key, value)
        old_value = self.get(key)
        self._overlay[key] = value
        self._persist_to_toml()
        self._telemetry.audit_event("settings.changed", {
            "key": key, "old_value": old_value, "new_value": value,
        })
        await self._bus.emit("agent:settings_changed", {
            "key": key, "old_value": old_value, "new_value": value,
        })
```

#### Config Resolution

Flat key names map to nested config paths:

| Key | Config Path | Type |
|-----|-------------|------|
| `model` | `config.llm.model` | str |
| `compaction_threshold` | `config.context.compact_threshold` | float |
| `tool_timeout` | `config.tools.policy.timeout_seconds` | int |
| `log_level` | `config.telemetry.log_level` | str |

#### TOML Persistence

`set()` writes the overlay to a `[settings]` section in `arcagent.toml`:

```toml
[settings]
model = "anthropic/claude-opus"
log_level = "DEBUG"
```

On next startup, `load_config()` reads the `[settings]` section and pre-populates the overlay. The frozen config values remain as defaults.

Implementation: Read TOML, update `[settings]` section, write back. Uses `tomllib` for read + string manipulation for write (stdlib tomllib is read-only; we write a simple `[settings]` block).

### 2.5 Config Changes (`core/config.py`)

New config models to support S003:

```python
class ExtensionEntry(BaseModel):
    """Per-extension configuration."""
    sandbox_mode: str = "workspace"  # workspace | paths | strict
    enabled: bool = True

class ExtensionConfig(BaseModel):
    """Extension system configuration."""
    paths: list[str] = []            # Additional discovery paths
    extensions: dict[str, ExtensionEntry] = {}
    global_dir: str = "~/.arcagent/extensions"

class SettingsConfig(BaseModel):
    """Runtime settings overlay (loaded from [settings] in TOML)."""
    model: str = ""
    compaction_threshold: float = 0.0
    tool_timeout: int = 0
    log_level: str = ""
```

Added to `ArcAgentConfig`:
```python
class ArcAgentConfig(BaseModel):
    # ... existing fields ...
    extensions: ExtensionConfig = ExtensionConfig()
    settings: SettingsConfig = SettingsConfig()
```

Also: `ToolConfig` gains `allowed_paths: list[str] = []` for tool scope expansion.

### 2.6 Agent Changes (`core/agent.py`)

#### New Instance Variables

```python
self._extension_loader: ExtensionLoader | None = None
self._skill_registry: SkillRegistry | None = None
self._settings: SettingsManager | None = None
```

#### startup() additions (after existing step 7)

```python
# 8. Settings Manager
self._settings = SettingsManager(
    config=self._config,
    telemetry=self._telemetry,
    bus=self._bus,
    config_path=config_path,
)

# 9. Skill Registry
self._skill_registry = SkillRegistry(telemetry=self._telemetry)
workspace = Path(self._config.agent.workspace).resolve()
global_skills = Path("~/.arcagent/skills").expanduser()
self._skill_registry.discover(workspace, global_skills)

# 10. Extension Loader
self._extension_loader = ExtensionLoader(
    tool_registry=self._tool_registry,
    bus=self._bus,
    telemetry=self._telemetry,
    config=self._config.extensions,
)
global_ext = Path(self._config.extensions.global_dir).expanduser()
await self._extension_loader.discover_and_load(workspace, global_ext)
```

#### Skill prompt injection

SkillRegistry subscribes to `agent:assemble_prompt` during its constructor or via a setup method called by ArcAgent. The handler injects `sections["skills"] = self.format_for_prompt()`.

`_SECTION_ORDER` in context_manager.py is extended:
```python
_SECTION_ORDER = ["identity", "notes", "skills", "policy", "context"]
```

#### reload() method (new)

```python
async def reload(self) -> None:
    """Re-discover extensions and skills. Hot reload."""
    if self._extension_loader:
        self._extension_loader.clear()
    if self._skill_registry:
        self._skill_registry.clear()

    workspace = Path(self._config.agent.workspace).resolve()

    # Re-register built-in tools (grep/find/ls may have been cleared)
    # Note: built-in tools are NOT cleared on reload, only extension tools

    # Re-discover skills
    global_skills = Path("~/.arcagent/skills").expanduser()
    self._skill_registry.discover(workspace, global_skills)

    # Re-discover and load extensions
    global_ext = Path(self._config.extensions.global_dir).expanduser()
    await self._extension_loader.discover_and_load(workspace, global_ext)
```

#### Properties (new)

```python
@property
def skills(self) -> list[SkillMeta]:
    return self._skill_registry.skills if self._skill_registry else []

@property
def settings(self) -> SettingsManager | None:
    return self._settings
```

### 2.7 Error Types (additions to `core/errors.py`)

```python
class ExtensionError(ArcAgentError):
    """Extension load, sandbox violation, or factory error."""
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(code=code, message=message, component="extensions", details=details)

class SkillError(ArcAgentError):
    """Skill discovery, parse, or format error."""
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(code=code, message=message, component="skill_registry", details=details)

class SettingsError(ArcAgentError):
    """Settings validation, persistence, or access error."""
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(code=code, message=message, component="settings_manager", details=details)
```

---

## 3. File Map

### New Files

| File | Component | Est. LOC | Location |
|------|-----------|----------|----------|
| `core/extensions.py` | ExtensionLoader, ExtensionAPI, ExtensionManifest | ~300 | Core |
| `core/skill_registry.py` | SkillRegistry, SkillMeta | ~150 | Core |
| `core/settings_manager.py` | SettingsManager | ~150 | Core |
| `tools/grep.py` | grep tool | ~80 | Tools |
| `tools/find.py` | find tool | ~60 | Tools |
| `tools/ls.py` | ls tool | ~60 | Tools |

### Modified Files

| File | Changes | Est. Delta |
|------|---------|------------|
| `core/agent.py` | Add extensions/skills/settings to startup, add reload(), add properties | +80 |
| `core/config.py` | Add ExtensionConfig, ExtensionEntry, SettingsConfig, allowed_paths | +40 |
| `core/context_manager.py` | Add "skills" to _SECTION_ORDER | +1 |
| `core/errors.py` | Add ExtensionError, SkillError, SettingsError | +30 |
| `tools/__init__.py` | Add grep/find/ls to create_builtin_tools | +6 |
| `tools/_validation.py` | Add allowed_paths parameter to resolve_workspace_path | +15 |

### New Test Files

| File | Tests |
|------|-------|
| `tests/unit/test_extensions.py` | Discovery, factory, sandbox, error isolation, hot reload |
| `tests/unit/test_skill_registry.py` | Discovery, frontmatter parsing, format, cache |
| `tests/unit/test_settings_manager.py` | Get/set, overlay, type validation, blocked keys, persistence |
| `tests/unit/test_grep.py` | Pattern matching, workspace scope, size limits |
| `tests/unit/test_find.py` | Glob matching, sort order, workspace scope |
| `tests/unit/test_ls.py` | Directory listing, type indicators, workspace scope |
| `tests/integration/test_self_extension.py` | Write extension → reload → tool available → execute |
| `tests/integration/test_skill_discovery.py` | Multi-source discovery → prompt injection → read full content |

### LOC Budget

| Component | New LOC | Category |
|-----------|---------|----------|
| core/extensions.py | ~300 | Core |
| core/skill_registry.py | ~150 | Core |
| core/settings_manager.py | ~150 | Core |
| core/agent.py delta | ~80 | Core |
| core/config.py delta | ~40 | Core |
| core/errors.py delta | ~30 | Core |
| core/context_manager.py delta | ~1 | Core |
| **Core subtotal** | **~751** | |
| tools/grep.py | ~80 | Tools |
| tools/find.py | ~60 | Tools |
| tools/ls.py | ~60 | Tools |
| tools/__init__.py delta | ~6 | Tools |
| tools/_validation.py delta | ~15 | Tools |
| **Tools subtotal** | **~221** | |

Current core LOC (from S001+S002): ~1,608
Projected core after S003: ~2,359
Budget: 3,000 → **641 LOC headroom remaining**

---

## 4. Dependency Flow

```
agent.py
  ├── config.py (ExtensionConfig, SettingsConfig) — existing dependency
  ├── extensions.py (new)
  │   ├── tool_registry.py — existing dependency
  │   ├── module_bus.py — existing dependency
  │   └── telemetry.py — existing dependency
  ├── skill_registry.py (new)
  │   └── telemetry.py — existing dependency
  ├── settings_manager.py (new)
  │   ├── config.py — existing dependency
  │   ├── module_bus.py — existing dependency
  │   └── telemetry.py — existing dependency
  └── tools/ (grep, find, ls)
      └── _validation.py — existing dependency
```

No circular dependencies. All new components depend only on existing core interfaces.

---

## 5. Security Considerations

### Extension Loading

- **No exec/eval**: Extensions loaded exclusively via `importlib.import_module()`
- **Source tracking**: Every tool registered by an extension carries `source="extension:{name}"`
- **Audit trail**: Every extension load, tool registration, and hook registration is logged
- **Error isolation**: One bad extension cannot crash the agent or block other extensions
- **Sandbox enforcement**: Strict mode blocks filesystem and network at the builtins level

### Settings Protection

- **Blocked keys**: Identity, vault, and key-related settings cannot be changed at runtime
- **Type validation**: Every `set()` validates the value type matches the expected type
- **Audit trail**: Every settings change emits an audit event with old/new values

### Tool Scope

- **Workspace boundary**: All new tools (grep/find/ls) use `resolve_workspace_path()`
- **Symlink rejection**: Symlinks blocked by default (existing behavior)
- **Size limits**: grep has 5MB file limit and 100 result cap; find has 200 result cap

---

## 6. Integration with S002

S003 components integrate with S002's SessionManager and MemoryModule without duplication:

| S002 Component | S003 Integration Point |
|----------------|----------------------|
| SessionManager | ArcAgent.chat() already uses it. No changes needed. |
| MemoryModule | Subscribes to bus events. Extension hooks can interact via bus. |
| Compaction | SettingsManager can update `compaction_threshold` at runtime. |
| ContextManager | Skills inject via `agent:assemble_prompt` event (same pattern as memory notes). |
