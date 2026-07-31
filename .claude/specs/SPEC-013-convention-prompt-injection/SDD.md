# SDD: Convention-Driven Prompt Injection

## Architecture Overview

Two auto-injected catalogs in the system prompt, both following the existing `agent:assemble_prompt` bus event pattern established by SkillRegistry.

```
ToolRegistry.format_for_prompt()  ──→  sections["tools"]      (agent.py subscriber)
MessagingModule._build_roster()   ──→  sections["teams"]      (messaging module subscriber)
```

Both produce XML-formatted text, cached until invalidated.

## Component Design

### C1: RegisteredTool Field Additions

**File:** `packages/arcagent/src/arcagent/core/tool_registry.py`

Add three optional fields to `RegisteredTool` dataclass:

```python
@dataclass
class RegisteredTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    transport: ToolTransport
    execute: Any
    timeout_seconds: int = 30
    source: str = ""
    # NEW
    when_to_use: str = ""
    example: str = ""
    category: str = ""
```

**Rationale (D3):** Ergonomic — module authors set metadata inline at registration time.

### C2: @native_tool Decorator Updates

**File:** `packages/arcagent/src/arcagent/core/tool_registry.py`

Add three keyword arguments to `native_tool()`:

```python
def native_tool(
    *,
    name: str = "",
    description: str = "",
    source: str = "",
    timeout_seconds: int = 30,
    params: dict[str, str | dict[str, Any]] | None = None,
    required: list[str] | None = None,
    # NEW
    when_to_use: str = "",
    example: str = "",
    category: str = "",
) -> Callable[..., Any]:
```

Pass through to `RegisteredTool` constructor:
```python
tool = RegisteredTool(
    ...,
    when_to_use=when_to_use,
    example=example,
    category=category,
)
```

### C3: ToolRegistry.format_for_prompt()

**File:** `packages/arcagent/src/arcagent/core/tool_registry.py`

New method on `ToolRegistry` following `SkillRegistry.format_for_prompt()` pattern:

```python
from xml.sax.saxutils import escape as xml_escape

def format_for_prompt(self) -> str:
    """XML-formatted tool catalog for system prompt injection.

    Returns empty string if no tools are registered.
    Cached — invalidated on register().
    """
    if self._prompt_cache is not None:
        return self._prompt_cache

    if not self._tools:
        self._prompt_cache = ""
        return ""

    lines = [f"<available-tools>"]
    lines.append(f"  <preamble>{xml_escape(self._preamble)}</preamble>")

    for tool in sorted(self._tools.values(), key=lambda t: t.name):
        safe_name = xml_escape(tool.name, {'"': "&quot;"})
        safe_desc = xml_escape(tool.description)
        attrs = f'name="{safe_name}"'
        if tool.category:
            attrs += f' category="{xml_escape(tool.category, {chr(34): "&quot;"})}"'

        lines.append(f"  <tool {attrs}>")
        lines.append(f"    <description>{safe_desc}</description>")
        if tool.when_to_use:
            lines.append(f"    <when-to-use>{xml_escape(tool.when_to_use)}</when-to-use>")
        if tool.example:
            lines.append(f"    <example>{xml_escape(tool.example)}</example>")
        lines.append("  </tool>")

    lines.append("</available-tools>")
    self._prompt_cache = "\n".join(lines)
    return self._prompt_cache
```

**Cache invalidation** in `register()`:
```python
def register(self, tool: RegisteredTool) -> None:
    self._check_policy(tool.name)
    self._tools[tool.name] = tool
    self._prompt_cache = None  # Invalidate cache
    _logger.info(...)
```

**Instance variables** added to `__init__`:
```python
self._prompt_cache: str | None = None
self._preamble: str = config.preamble or _DEFAULT_PREAMBLE
```

**Default preamble:**
```python
_DEFAULT_PREAMBLE = (
    "You have the following tools available. "
    "Use them as needed to accomplish your tasks."
)
```

### C4: ToolsConfig Preamble Field

**File:** `packages/arcagent/src/arcagent/core/config.py`

Add `preamble` field to `ToolsConfig`:

```python
class ToolsConfig(BaseModel):
    native: dict[str, NativeToolEntry] = {}
    mcp_servers: dict[str, MCPServerEntry] = {}
    http: dict[str, HTTPToolEntry] = {}
    process: dict[str, ProcessToolEntry] = {}
    policy: ToolConfig = ToolConfig()
    allowed_module_prefixes: list[str] = Field(default=["arcagent."])
    preamble: str = ""  # NEW: tool catalog preamble override
```

### C5: Tool Catalog Prompt Injection (agent.py)

**File:** `packages/arcagent/src/arcagent/core/agent.py`

New method `_setup_tool_prompt_injection()`, following exact pattern of `_setup_skill_prompt_injection()`:

```python
def _setup_tool_prompt_injection(self) -> None:
    """Subscribe to agent:assemble_prompt to inject tool catalog."""
    bus = self._bus
    tool_registry = self._tool_registry
    telemetry = self._telemetry
    if bus is None or tool_registry is None:
        return

    async def _inject_tools(ctx: Any) -> None:
        sections = ctx.data.get("sections")
        if not isinstance(sections, dict):
            return
        prompt_text = tool_registry.format_for_prompt()
        if prompt_text:
            sections["tools"] = prompt_text
            # Audit rebuild if cache was empty (fresh build)
            if telemetry is not None:
                telemetry.audit_event(
                    "prompt.tools_catalog_rebuilt",
                    {"tool_count": len(tool_registry.tools)},
                )

    bus.subscribe(
        event="agent:assemble_prompt",
        handler=_inject_tools,
        priority=85,
        module_name="tool_registry",
    )
```

Called from `start()` alongside `_setup_skill_prompt_injection()`.

**Priority 85** — after messaging (50), before skills (90). Tools come before skills in alphabetical section ordering.

**Audit optimization:** Only emit audit event when cache was actually rebuilt (check `_prompt_cache` state before format call). Avoid audit noise on every prompt assembly.

### C6: Team Roster Injection (messaging module)

**File:** `packages/arcagent/src/arcagent/modules/messaging/__init__.py`

Modify `_on_assemble_prompt()` to:
1. Change section key from `"messaging"` to `"teams"`
2. Add team roster as XML block
3. Cache roster with TTL

New instance variables in `__init__`:
```python
self._roster_cache: str | None = None
self._roster_cache_time: float = 0.0
```

New method `_build_roster()`:
```python
def _build_roster(self) -> str:
    """Build XML roster from EntityRegistry with TTL caching."""
    now = time.monotonic()
    ttl = self._config.roster_ttl_seconds

    if self._roster_cache is not None and (now - self._roster_cache_time) < ttl:
        return self._roster_cache

    if self._registry is None:
        return ""

    # Sync call — EntityRegistry.list_entities() reads from disk
    # We're in an async context but the registry uses sync file I/O
    entities = self._registry.list_entities()
    if not entities:
        self._roster_cache = ""
        self._roster_cache_time = now
        return ""

    lines = ["<team-roster>"]
    for entity in entities:
        # Dynamic field rendering — all non-default values
        data = entity.model_dump(exclude_defaults=True)
        safe_name = xml_escape(str(data.get("name", "")), {'"': "&quot;"})
        attrs = f'name="{safe_name}"'
        if "id" in data:
            safe_id = xml_escape(str(data["id"]), {'"': "&quot;"})
            attrs += f' id="{safe_id}"'

        lines.append(f"  <entity {attrs}>")
        for key, value in data.items():
            if key in ("name", "id"):
                continue  # Already in attributes
            if isinstance(value, list):
                safe_val = xml_escape(", ".join(str(v) for v in value))
            else:
                safe_val = xml_escape(str(value))
            lines.append(f"    <{key}>{safe_val}</{key}>")
        lines.append("  </entity>")

    lines.append("</team-roster>")

    self._roster_cache = "\n".join(lines)
    self._roster_cache_time = now

    # Audit roster rebuild
    if self._telemetry is not None:
        self._telemetry.audit_event(
            "prompt.roster_rebuilt",
            {"entity_count": len(entities)},
        )

    return self._roster_cache
```

Modified `_on_assemble_prompt()`:
```python
async def _on_assemble_prompt(self, ctx: EventContext) -> None:
    """Inject team context into the system prompt."""
    sections = ctx.data.get("sections", {})

    # ... existing messaging context lines ...

    # Add team roster
    roster = self._build_roster()
    if roster:
        lines.append("")
        lines.append(roster)

    sections["teams"] = "\n".join(lines)  # Changed from "messaging"
```

### C7: MessagingConfig TTL Field

**File:** `packages/arcagent/src/arcagent/modules/messaging/config.py`

Add `roster_ttl_seconds` field:

```python
class MessagingConfig(ModuleConfig):
    # ... existing fields ...
    roster_ttl_seconds: float = 60.0  # NEW: team roster cache TTL
```

## Data Flow

```
Startup:
  agent.start()
    → tool_registry registers tools
    → _setup_tool_prompt_injection() subscribes to bus
    → messaging module startup()
      → registers messaging tools
      → subscribes _on_assemble_prompt

Each Turn:
  context_manager.assemble_system_prompt()
    → emits agent:assemble_prompt
    → _inject_tools handler:
        → tool_registry.format_for_prompt()
        → if cache hit: return cached string
        → if cache miss: build XML, cache, return
        → sections["tools"] = result
    → _on_assemble_prompt handler:
        → build messaging context (existing)
        → _build_roster()
        → if TTL not expired: return cached roster
        → if TTL expired: read EntityRegistry, build XML, cache
        → sections["teams"] = combined result
    → context_manager orders sections alphabetically
    → final prompt: identity → (alpha: skills, teams, tools, ...) → context

Tool Registration (mid-session):
  tool_registry.register(new_tool)
    → self._prompt_cache = None  # invalidate
    → next assemble_prompt rebuilds catalog
```

## Security Considerations

| Threat | Mitigation | Reference |
|--------|------------|-----------|
| Tag Confusion (XML injection in descriptions) | `xml_escape()` on all string values | D13, SkillRegistry pattern |
| Semantic Content Injection in descriptions | Module signing prevents unauthorized tool registration | ASI04, CLAUDE.md |
| MCP tool description poisoning | `source` field tracks provenance; XML-escape applied | Deepen research |
| Prompt leakage via catalog | No secrets in tool metadata; descriptions are operational, not confidential | LLM07 |
| Audit gaps | `prompt.tools_catalog_rebuilt` and `prompt.roster_rebuilt` events | NIST 800-53 AU-2 |

## Files Modified

| File | Change | LOC Delta |
|------|--------|-----------|
| `core/tool_registry.py` | Add fields, `format_for_prompt()`, cache invalidation | +55 |
| `core/agent.py` | Add `_setup_tool_prompt_injection()` | +20 |
| `core/config.py` | Add `preamble` to ToolsConfig | +1 |
| `modules/messaging/__init__.py` | Add `_build_roster()`, change section key | +45 |
| `modules/messaging/config.py` | Add `roster_ttl_seconds` | +1 |
| **Total** | | **+122** |

All within core LOC budget (<3,500).
