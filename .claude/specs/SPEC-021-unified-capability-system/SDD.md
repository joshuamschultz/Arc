# SDD: Unified Capability System

**Spec**: SPEC-021 | **Type**: integration | **Status**: DRAFT

## Architecture Overview

```
                  ┌──────────────────────────────────────────────┐
                  │                  Agent Startup                │
                  └─────────────────────┬─────────────────────────┘
                                        │
                                        ▼
              ┌──────────────────────────────────────────────────┐
              │         CapabilityLoader.scan_and_register()       │ ◄─── reload()
              └─────────────────────────┬────────────────────────┘
                                        │
                ┌───────────────────────┴────────────────────────┐
                │                                                 │
                ▼                                                 ▼
       ┌───────────────┐                              ┌───────────────────┐
       │   Discover    │                              │     Validate      │
       │ (4 scan roots)│                              │  (AST + frontmatter│
       └───────┬───────┘                              │  + tier policy)   │
               │                                      └─────────┬─────────┘
               ▼                                                │
       ┌───────────────┐                                        ▼
       │   AST cache   │ ◄────────── MD5+mtime ──────►  ┌──────────────┐
       │  (perf gate)  │                                │  TOFU policy  │
       └───────┬───────┘                                │  layer (per-  │
               │                                        │  tier gate)   │
               ▼                                        └──────┬───────┘
       ┌───────────────┐                                       │
       │ OS sandbox    │ ◄─── enterprise/federal: seccomp ─────┘
       │  wrapper      │
       └───────┬───────┘
               │
               ▼
       ┌────────────────────────────────────────────────────────┐
       │              CapabilityRegistry (aiorwlock)             │
       │  ┌───────┐  ┌───────┐  ┌────────┐  ┌─────────────┐   │
       │  │ tools │  │ hooks │  │ tasks  │  │ capabilities │   │
       │  └───┬───┘  └───┬───┘  └────┬───┘  └──────┬──────┘   │
       └──────┼──────────┼──────────┼──────────────┼──────────┘
              │          │          │              │
              ▼          ▼          ▼              ▼
       ┌──────────────────────────────────────────────────────┐
       │              Bus event emission                        │
       │   capability:added/removed/replaced/                   │
       │   registration_failed/setup_failed                    │
       └──────────────────────────────────────────────────────┘
              │
              ▼
       ┌─────────────────────────────────────────────────┐
       │          arctrust.audit.emit (NIST AU-2)         │
       └─────────────────────────────────────────────────┘
              │
              ▼
       ┌─────────────────────────────────────────────────┐
       │   Existing agent:assemble_prompt subscribers     │
       │   (replaces _setup_tool_prompt_injection +       │
       │    _setup_skill_prompt_injection)                │
       └─────────────────────────────────────────────────┘
```

## Module Boundaries

Hard boundaries enforced. No cross-module logic bleed.

| Module | Owns | Does NOT touch |
|--------|------|-----|
| `arcagent.core.capability_loader` | scan, AST validate, instantiate, lifecycle dispatch | tool execution, prompt rendering, audit format |
| `arcagent.core.capability_registry` | in-memory registry with aiorwlock; XML manifest rendering for prompt | scan logic, tool execution, sandbox |
| `arcagent.core.os_sandbox` | seccomp/sandbox-exec wrapper; raises sandbox violation | AST validation (delegates to `_dynamic_loader`), tier policy |
| `arcagent.tools._decorator` | `@tool` / `@hook` / `@background_task` / `@capability` class definitions; metadata stamping | registration (loader does that), execution |
| `arcagent.tools._dynamic_loader` | AST validator + restricted-builtins compile + AST cache | sandbox wrapping, registration |
| `arctrust.policy` (existing) | tier policy layers; `[security.validators]` TOFU layer plugs in | discovery, prompt assembly |
| `arctrust.audit` (existing) | AuditEvent emission, sinks | event triggering (caller decides) |
| `arc` CLI (`arccli.module_commands`) | `arc module list/enable/disable/install/uninstall`, `arc trust approve/list` | runtime registration (operates on filesystem only) |

## Components

### C-001 — `CapabilityLoader`

**Responsibility**: Discover, validate, and register capabilities from four scan roots. Single self-mod entry point via `reload()`.

**File**: `packages/arcagent/src/arcagent/core/capability_loader.py` (new, ~300 LOC)

**Public API**:
```python
class CapabilityLoader:
    def __init__(
        self,
        scan_roots: list[ScanRoot],
        registry: CapabilityRegistry,
        validator: AstValidator,
        sandbox: OsSandbox | None,
        tier: Tier,
        bus: ModuleBus,
        telemetry: AgentTelemetry,
    ) -> None: ...

    async def scan_and_register(self) -> CapabilityDiff: ...
    async def reload(self) -> str: ...  # returns human-readable diff per R-005
    async def shutdown(self) -> None: ...
```

**Scan order** (R-001):
1. `arcagent/builtins/capabilities/` (package-internal)
2. `~/.arc/capabilities/` (global)
3. `<agent_root>/capabilities/` (per-agent, user-curated)
4. `<agent_root>/workspace/.capabilities/` (agent-authored)

**Per-file flow**:
1. Compute `MD5(source) + mtime`. If hit in AST cache, skip re-import.
2. AST validate (`AstValidator.validate(source)`). Reject → `capability:registration_failed`.
3. TOFU policy check (`arctrust.policy.PolicyContext` with `[security.validators]`). Federal: require Sigstore signature. Enterprise: TOFU prompt or persisted approval. Personal: auto-allow with toggle.
4. If sandbox required (enterprise+): `os_sandbox.run(exec_path, source)` else direct `importlib.util.module_from_spec`.
5. Find decorated functions/classes via `func._arc_capability_meta` stamp.
6. Hand to `CapabilityRegistry.register_capability(meta, source_path, scan_root)`.

**Conflict resolution** (R-004):
- Tools/skills: registry overwrites by name; emits `capability:replaced` with shadow chain.
- Hooks: registry appends to event-keyed list; ordered by `priority` (default 100, with `tryfirst=True` → 90, `trylast=True` → 110); fan-out on emission.
- Background tasks: registry stores name → task; on overwrite, drain-then-replace via `_drain_and_replace_task()`.

**Diff format** (R-005):
- Compute set diff against last successful registry snapshot.
- Render: `reload: +{N} added (...), ~{M} replaced (... v→v), -{K} removed (...), {E} errors`.
- Multi-line only when `E > 0`.

### C-002 — `CapabilityRegistry`

**Responsibility**: Thread-safe, kind-discriminated registry. XML manifest rendering for prompt injection.

**File**: `packages/arcagent/src/arcagent/core/capability_registry.py` (new, ~250 LOC)

**Public API**:
```python
class CapabilityRegistry:
    _lock: aiorwlock.RWLock
    _tools: dict[str, ToolEntry]
    _skills: dict[str, SkillEntry]
    _hooks: dict[str, list[HookEntry]]      # event-keyed list, sorted by priority
    _tasks: dict[str, BackgroundTaskEntry]   # name-keyed
    _capabilities: dict[str, LifecycleEntry] # name-keyed; setup/teardown
    _prompt_cache: str | None

    async def register_tool(self, entry: ToolEntry) -> RegisterResult: ...
    async def register_skill(self, entry: SkillEntry) -> RegisterResult: ...
    async def register_hook(self, entry: HookEntry) -> RegisterResult: ...
    async def register_task(self, entry: BackgroundTaskEntry) -> RegisterResult: ...
    async def register_capability(self, entry: LifecycleEntry) -> RegisterResult: ...

    async def unregister(self, kind: Kind, name: str) -> None: ...
    async def get_tool(self, name: str) -> ToolEntry | None: ...
    async def get_skill(self, name: str) -> SkillEntry | None: ...
    async def get_hooks(self, event: str) -> list[HookEntry]: ...

    async def format_for_prompt(self) -> str: ...   # cached XML
    async def to_arcrun_tools(self) -> list[ArcRunTool]: ...
```

**Locking semantics** (R-063):
- Tool calls / `to_arcrun_tools` / `format_for_prompt` / `get_*`: reader lock
- All `register_*` / `unregister`: writer lock
- Reload: holds writer lock for entire scan-and-swap cycle

**Manifest XML** (R-020): extends D-073/D-074 pattern. Single root `<capabilities>` with two children:
```xml
<available-tools>
  <tool name="grep" version="1.0.0" classification="read_only">
    <description>Search file contents by regex</description>
    <when-to-use>When you need to find patterns across files</when-to-use>
  </tool>
</available-tools>
<available-skills>
  <skill name="create-tool" version="1.0.0" location="<path>/SKILL.md">
    <description>Build a new Python tool, validate it, register it.</description>
    <triggers>add a new tool, extend yourself, write a tool that...</triggers>
    <tools>write, reload, bash</tools>
  </skill>
</available-skills>
```

**Cache** invalidated on every successful register/unregister. Same pattern as existing `ToolRegistry._prompt_cache`.

### C-003 — Decorator surface

**Responsibility**: Stamp metadata on functions/classes; type-hint inference for tool schemas.

**File**: `packages/arcagent/src/arcagent/tools/_decorator.py` (extend, ~+50 LOC)

**Existing**: `@tool(name, description, classification, capability_tags)` already infers JSON schema from typed signature via `_schema_from_signature`. Stamps `func._arc_tool_meta`.

**New decorators**:
```python
@tool(
    name=, description=, when_to_use=, classification=,
    capability_tags=[], requires_skill=None, version="1.0.0", examples=None, model_hint=None,
)
async def fn(...) -> str: ...

@hook(event="agent:ready", priority=100, tryfirst=False, trylast=False)
async def fn(ctx) -> None: ...

@background_task(name=..., interval=60.0)
async def fn(ctx) -> None: ...

@capability(name=..., depends_on=[])
class C:
    async def setup(self, ctx) -> None: ...
    async def teardown(self) -> None: ...
    @tool(...)  # methods support @tool — bound at registration
    async def some_method(self, arg): ...
```

All four stamp `func._arc_capability_meta = CapabilityMetadata(kind=..., ...)`. Loader reads stamps in scan; registry receives entries.

### C-004 — AST Validator (extended)

**Responsibility**: Static rejection of unsafe Python before import.

**File**: `packages/arcagent/src/arcagent/tools/_dynamic_loader.py` (extend existing AST validator)

**Existing blocklists** (retain):
- `_BLOCKED_IMPORTS`: `ctypes`, `subprocess`, `socket`, `os`, `sys`, `pickle`, `marshal`, `shelve`
- `_BLOCKED_ATTRIBUTES`: `gi_frame`, `f_back`, `f_builtins`, `f_globals`, `f_locals`, `__class__`, `__bases__`, `__subclasses__`, `__reduce__`, `__reduce_ex__`, `__mro__`, `__dict__`, `modules`
- `_BLOCKED_CALLS`: `compile`, `eval`, `exec`, `__import__`
- `_BLOCKED_ASSIGN_TARGETS`: `__builtins__`, `__loader__`, `__spec__`

**New additions** (R-040):
- Add to `_BLOCKED_ATTRIBUTES`: `gi_code`, `gi_yieldfrom`, `tb_frame`
- New visitor: `visit_FormattedValue` — detect `f"{obj.attr}"` patterns and reject (because `__format__` invokes attribute access without `getattr`)
- New visitor: `visit_ClassDef` — reject classes defining `__init_subclass__`; reject classes with metaclass that defines `__getitem__`
- Add to `_BLOCKED_ATTRIBUTES`: `__init_subclass__`, `__class_getitem__`
- New: reject access to `AttributeError.obj`, `AttributeError.name` (Python 3.10+) via attribute-on-exception checker
- Add to `_BLOCKED_ATTRIBUTES` for descriptor side-channels: `__pos__`, `__neg__`, `__get__`, `__set__` (when used in subscript `[+target.attr for target in [...]]` patterns)

**AST cache** (new, R-001 perf):
- `_ast_cache: dict[Path, tuple[str, float]]` — `path → (md5, mtime)`
- Before validation, hash + mtime check; if unchanged, skip and reuse last result
- Invalidated on `reload()` for changed files only

### C-005 — `OsSandbox`

**Responsibility**: OS-level isolation for self-executing agent code at enterprise+ tier. Wraps Python execution in seccomp (Linux) or sandbox-exec (macOS).

**File**: `packages/arcagent/src/arcagent/core/os_sandbox.py` (new, ~150 LOC)

**Available only via** `pip install arcagent[enterprise]`. Core has zero dependency.

**Public API**:
```python
class OsSandbox(Protocol):
    async def run(self, source: str, *, scope_path: Path, timeout: float) -> Any: ...

def make_sandbox(tier: Tier) -> OsSandbox | None:
    if tier == "personal":
        return None  # no sandbox
    if sys.platform == "darwin":
        return SandboxExecSandbox()
    if sys.platform == "linux":
        return SeccompSandbox()
    raise NotImplementedError(f"OS sandbox not available on {sys.platform}")
```

**Linux**: pyseccomp profile blocking syscalls outside `read/write/openat/close/stat/mmap/munmap/brk/exit/exit_group` plus filesystem ops scoped to `scope_path`.

**macOS**: `sandbox-exec -p '(version 1) (deny default) (allow process-fork) (allow file-read* (regex "^/path/to/scope/"))...'` profile generated per scope.

**Federal tier**: `OsSandbox` invoked is moot — federal blocks unsigned agent .py at AST-load time per D-354.

### C-006 — TOFU Policy Layer

**Responsibility**: Plug into existing `arctrust.policy.PolicyPipeline` as a new layer that gates self-executing agent code per tier.

**File**: `packages/arcagent/src/arcagent/core/tool_policy.py` (extend existing) or new `packages/arctrust/src/arctrust/policy/tofu_layer.py` if arctrust naturally owns it.

**Public API**:
```python
class TofuLayer(PolicyLayer):
    def __init__(self, tier: Tier, trust_path: Path) -> None: ...

    def evaluate(self, ctx: PolicyContext, target: CapabilitySource) -> Decision:
        # Federal: require Sigstore signature on source
        # Enterprise: check approved hashes; if new, raise NEW_SIGHTING (caller prompts)
        # Personal: auto-allow if [security] auto_run_agent_code = true
```

**Trust file location** (R-043):
- Per-agent: `<agent_root>/arcagent.toml` `[security.validators]` block
- Global (optional): `~/.arc/security/trust.toml`
- Both **outside** `<agent_root>/workspace/`. Agent has no write access.

**TOML schema additions**:
```toml
[security.validators]
auto_run_agent_code = true   # personal tier only

[[security.validators.approved]]
name = "create-skill"
hash = "sha256:abc123..."
approver = "joshschultz@example.com"
timestamp = "2026-04-28T14:30:00Z"
```

### C-007 — Bus event taxonomy

**Responsibility**: Emit five lifecycle events on every capability change.

**File**: emissions live in `capability_loader.py` and `capability_registry.py`; subscribers existing.

**Events** (R-050, D-367 extended):

| Event | Emitted from | Payload |
|-------|--------------|---------|
| `capability:added` | registry register success | `{name, kind, version, source_path, scan_root}` |
| `capability:removed` | registry unregister | `{name, kind, source_path}` |
| `capability:replaced` | registry overwrite | `{name, kind, old_version, new_version, old_source, new_source, scan_root}` |
| `capability:registration_failed` | loader AST/frontmatter rejection | `{path, kind, reason, error_detail}` |
| `capability:setup_failed` | `@capability` setup() raises | `{name, kind, exception_type, exception_msg}` |

**Existing events repurposed/deleted**: `agent:extensions_loaded` and `agent:skills_loaded` are removed (subsumed by `capability:added` events). `agent:tools_reloaded` becomes redundant; replaced by the diff returned from `reload()`.

### C-008 — System Prompt Integration

**Responsibility**: Replace the existing two bus subscribers (`_inject_tools` priority 85, `_inject_skills` priority 90) with a single `_inject_capabilities` subscriber that calls `CapabilityRegistry.format_for_prompt()`.

**File**: `packages/arcagent/src/arcagent/core/agent.py` (modify `_setup_tool_prompt_injection` and `_setup_skill_prompt_injection`)

**New design**:
```python
def _setup_capability_prompt_injection(self) -> None:
    bus = self._bus
    registry = self._capability_registry
    if bus is None or registry is None:
        return

    async def _inject_capabilities(ctx):
        sections = ctx.data.get("sections")
        if not isinstance(sections, dict):
            return
        rendered = await registry.format_for_prompt()
        if rendered:
            sections["capabilities"] = rendered

    async def _inject_skill_usage_instruction(ctx):
        sections = ctx.data.get("sections")
        if not isinstance(sections, dict):
            return
        sections["skill_usage"] = SKILL_USAGE_INSTRUCTION  # 3-line constant

    bus.subscribe(event="agent:assemble_prompt", handler=_inject_capabilities, priority=85)
    bus.subscribe(event="agent:assemble_prompt", handler=_inject_skill_usage_instruction, priority=91)
```

**Constant** (R-021):
```python
SKILL_USAGE_INSTRUCTION = """\
Scan <available-skills>. If one clearly applies, read its SKILL.md at the listed location, \
then follow it. Read references only when the body cites them. \
Never read more than one skill up front; pick the most specific."""
```

### C-009 — Builtin Capabilities

**Responsibility**: Ship the core 7 tools + new self-mod tools and their skills as decorator-form Python under `arcagent/builtins/capabilities/`.

**Layout**:
```
packages/arcagent/src/arcagent/builtins/capabilities/
├── read.py            # @tool — already implemented logic in arcagent/tools/read.py, ported
├── write.py           # @tool — ported from tools/write.py
├── edit.py            # @tool — ported from tools/edit.py
├── bash.py            # @tool — ported from tools/bash.py
├── grep.py            # @tool — ported from tools/grep.py
├── find.py            # @tool — ported from tools/find.py
├── ls.py              # @tool — ported from tools/ls.py
├── reload.py          # @tool — calls CapabilityLoader.reload(), returns diff
├── create_tool.py     # @tool — wraps DynamicToolLoader; persists to workspace/.capabilities/
├── create_skill.py    # @tool — scaffolds skill folder; persists to workspace/.capabilities/skills/
├── update_tool.py     # @tool — bumps version, validates, persists
├── update_skill.py    # @tool — bumps version, validates, persists
└── skills/
    ├── create-tool/
    │   ├── SKILL.md       # frontmatter + 7 sections; teaches the convention
    │   ├── references/
    │   │   ├── decorator-fields.md
    │   │   ├── ast-blocked-list.md
    │   │   └── examples-good-and-bad.md
    │   ├── scripts/
    │   │   └── validate.py    # frontmatter + AST + tools-exist checks
    │   └── templates/
    │       └── tool.py.template
    ├── create-skill/
    │   ├── SKILL.md
    │   ├── references/
    │   │   ├── frontmatter-spec.md
    │   │   └── section-rubric.md
    │   ├── scripts/
    │   │   └── validate.py
    │   └── templates/
    │       └── skill-folder/
    │           ├── SKILL.md.template
    │           ├── references/.gitkeep
    │           └── scripts/validate.py.template
    ├── update-tool/
    │   ├── SKILL.md
    │   └── scripts/validate.py
    └── update-skill/
        ├── SKILL.md
        └── scripts/validate.py
```

**Why ship under `builtins/capabilities/`**: makes the loader treat them uniformly. No special-casing of "core" vs "user" tools in the registry. The hardcoded list in `tools/__init__.py` goes away (D-360).

### C-010 — Module Migrations

**Responsibility**: Rewrite each existing `arcagent/modules/<name>/` to decorator form. Delete `MODULE.yaml` runtime parsing.

**Per-module migration target** (from research):

| Module | Before (entry-point class + `MODULE.yaml`) | After (decorators in folder) |
|--------|--------------------------------------------|------------------------------|
| memory | `MarkdownMemoryModule.startup()` registers tools + subscribes 6 events | 6 × `@hook` + N × `@tool` + 1 × `@background_task` (entity extractor) |
| scheduler | `SchedulerModule` with engine lifecycle, 4 CRUD tools, 2 events | `@capability` class with `setup`/`teardown` (engine) + 4 × `@tool` + `@hook("agent:ready")` |
| browser | `BrowserModule` with Chrome process | `@capability` class (Chrome lifecycle) + N × `@tool` |
| voice | `VoiceModule` with provider plugins | 2 × `@tool` (transcribe, synthesize) + 2 × `@hook` |
| telegram | `TelegramModule` with poll loop, 1 tool | `@background_task` (poll) + `@tool` (notify_user) + 3 × `@hook` |
| slack | `SlackModule` with WebSocket | `@capability` class (WebSocket lifecycle) + `@tool` + `@hook` |
| policy | `PolicyModule` (pure subscriber) | 3 × `@hook` only |
| ui_reporter | `UIReporterModule` (pure subscriber) | 17 × `@hook` (or `@capability` class with internal dispatch — author choice) |

**Each module's directory becomes**:
```
arcagent/modules/<name>/
  capabilities.py       # all @tool/@hook/@background_task in one file (or split if large)
  capability.py         # @capability class if needed (browser, scheduler, slack)
  config.py             # Pydantic config models (existing pattern, retained)
  README.md             # author docs
  __init__.py           # re-exports for tests
  # MODULE.yaml DELETED
```

**During phase 5**, modules are *symlinked* into `~/.arc/capabilities/<name>/` by `arc module enable`. Until enabled, the module's capabilities don't load — this is the new opt-in surface.

### C-011 — `arc` CLI module + trust commands

**Responsibility**: Operator-facing commands for module install/enable/disable, trust approval. No runtime registration logic — operates on filesystem only.

**File**: `packages/arccli/src/arccli/commands/module.py` (new) and `arccli/commands/trust.py` (new)

**Commands** (R-070..R-075):

```bash
arc module list                           # show all + status
arc module enable <name>                  # symlink builtins/modules/<name>/ → ~/.arc/capabilities/<name>/
arc module disable <name>                 # remove symlink
arc module install <bundle>               # extract .tgz/.zip/dir to ~/.arc/capabilities/<name>/; verify Sigstore at federal
arc module uninstall <name>               # rm -rf ~/.arc/capabilities/<name>/

arc trust approve <hash>                  # add to arcagent.toml [security.validators.approved]
arc trust list                            # show approved entries

arc agent build                           # existing; extended to seed [security.validators] block
```

**Sigstore verification at install** (R-044):
```python
# packages/arccli/src/arccli/sigstore_verify.py
def verify_bundle(bundle_path: Path, *, cert_identity: str, oidc_issuer: str, rekor_url: str) -> bool:
    # uses sigstore-python; falls back to self-hosted rekor at rekor_url
```

Available only via `pip install arcagent[federal]` extras (sigstore + cosign deps).

## Data Model

### `CapabilityMetadata` (stamped on functions/classes)

```python
@dataclass(frozen=True)
class CapabilityMetadata:
    kind: Literal["tool", "hook", "background_task", "capability"]
    name: str
    description: str  # required for tool/skill
    version: str = "1.0.0"
    when_to_use: str | None = None  # tool only
    classification: Literal["read_only", "state_modifying"] | None = None  # tool only
    capability_tags: list[str] = field(default_factory=list)  # tool only
    requires_skill: str | None = None  # tool only
    examples: list[str] | None = None  # tool only
    model_hint: str | None = None  # tool/skill optional
    event: str | None = None  # hook only
    priority: int = 100  # hook only
    tryfirst: bool = False  # hook only
    trylast: bool = False  # hook only
    interval: float | None = None  # background_task only
    depends_on: list[str] = field(default_factory=list)  # capability class only
```

### `SkillEntry`

```python
@dataclass
class SkillEntry:
    name: str
    description: str
    triggers: list[str]
    tools: list[str]
    version: str
    model_hint: str | None
    skill_md_path: Path
    folder_path: Path
    source_path: Path
    scan_root: ScanRoot
    references: list[Path]      # auto-discovered from references/
    scripts: list[Path]         # auto-discovered from scripts/
    templates: list[Path]       # auto-discovered from templates/
    assets: list[Path]          # auto-discovered from assets/
```

### `[security.validators]` TOML

```toml
[security]
tier = "enterprise"

[security.validators]
auto_run_agent_code = false

[[security.validators.approved]]
name = "create-skill"
hash = "sha256:abc123..."
approver = "joshschultz@example.com"
timestamp = "2026-04-28T14:30:00Z"

[[security.validators.approved]]
name = "format-date"
hash = "sha256:def456..."
approver = "joshschultz@example.com"
timestamp = "2026-04-28T15:42:00Z"
```

## Lifecycle Sequence Diagrams

### Agent startup

```
agent.start()
  └─► CapabilityLoader.scan_and_register()
       ├─► for each scan root in precedence order:
       │    ├─► find .py and .md files
       │    ├─► AST cache check (MD5+mtime)
       │    ├─► AstValidator.validate() if changed
       │    ├─► TofuLayer.evaluate() per source
       │    ├─► OsSandbox.run() if enterprise+
       │    └─► CapabilityRegistry.register_*()
       │         ├─► writer lock
       │         ├─► emit capability:added
       │         └─► audit_event("capability.added")
       ├─► toposort lifecycle capabilities
       ├─► for each (in topo order): await capability.setup(ctx)
       │    └─► on raise: emit capability:setup_failed; rollback prior setups
       └─► invalidate prompt cache
```

### Agent self-modification

```
LLM:
  write workspace/.capabilities/format-date.py
  write workspace/.capabilities/skills/format-date/SKILL.md
  ...
  call reload()

reload():
  └─► CapabilityLoader.reload()
       ├─► writer lock
       ├─► snapshot current registry
       ├─► scan_and_register()  (full re-scan)
       │    └─► same flow as startup; AST cache hits unchanged files
       ├─► compute diff vs snapshot
       ├─► for removed lifecycle caps: teardown in reverse-topo
       ├─► for added lifecycle caps: setup in topo
       ├─► render diff string per R-005
       └─► return string to LLM
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| AST validator rejects file | Skip, audit `capability:registration_failed`, continue |
| Frontmatter missing required field | Skip, audit `capability:registration_failed`, continue |
| Tool name collision | Last-wins, audit `capability:replaced` with shadow path |
| Hook collision (same event+name) | Treated as same hook; latest version replaces; audit `capability:replaced` |
| `setup()` raises | Audit `capability:setup_failed`, rollback prior setups in reverse, agent fails to start (or `reload()` fails with diff containing error) |
| Background task `cancel()` doesn't return within drain timeout (5s) | Force-cancel with warning audit; new task starts |
| Federal: unsigned agent .py at any scan root | Skip, audit, continue |
| Enterprise: new agent .py without prior approval | Skip during non-interactive `reload()`; in interactive (TUI), prompt user via TOFU dialog |
| Personal: `[security] auto_run_agent_code = false` | Skip agent-authored .py, audit |
| `aiorwlock` writer starvation under heavy tool-call load | Acceptable; `reload()` is rare and operator-initiated |

## Testing Strategy

| Component | Test type | Location | Coverage target |
|-----------|-----------|----------|-----------------|
| C-001 CapabilityLoader | unit + integration | `tests/unit/core/test_capability_loader.py`, `tests/integration/test_capability_discovery.py` | 95% |
| C-002 CapabilityRegistry | unit (lock semantics, manifest XML) | `tests/unit/core/test_capability_registry.py` | 95% |
| C-003 Decorators | unit (metadata stamping, schema inference) | `tests/unit/tools/test_decorator.py` | 100% |
| C-004 AST Validator | unit (every new bypass category gets POC test) | `tests/unit/tools/test_dynamic_loader.py`, `tests/security/test_ast_bypasses.py` | 100% on blocklists |
| C-005 OsSandbox | integration (Linux + macOS, ctypes escape attempt) | `tests/security/test_os_sandbox.py` | 90% |
| C-006 TofuLayer | integration (3 tiers, approval persistence) | `tests/integration/test_tofu_per_tier.py` | 95% |
| C-007 Bus events | integration (5 events fire correctly) | `tests/integration/test_capability_lifecycle_events.py` | 100% events covered |
| C-008 Prompt injection | unit (manifest XML golden test, instruction injection) | `tests/unit/core/test_prompt_assembly.py` | 100% |
| C-009 Builtins | per-tool unit tests (existing patterns retained) | `tests/unit/builtins/` | 95% |
| C-010 Module migrations | each module's existing test suite must pass on migrated form | `tests/unit/modules/<name>/` | unchanged |
| C-011 arc CLI | unit + e2e | `tests/unit/arccli/`, `tests/e2e/test_module_lifecycle.sh` | 90% |

**Security tests** (R-040, R-042):
- POC exploit per CVE category (CVE-2023-37271 generators, CVE-2026-0863 AttributeError.obj, CVE-2025-68668 ctypes via FFI), each must be rejected
- Three integration tests for tier-specific TOFU behavior
- Sigstore verification with valid + tampered + unsigned bundles at federal tier

**Performance tests** (R-086):
- Cold-start benchmark: 50-capability discovery + register in <500ms
- AST cache hit ratio: ≥95% on second `reload()` of unchanged tree

## Tier Variations Summary

| Concern | Federal | Enterprise | Personal |
|---------|---------|------------|----------|
| Agent-authored .py reload | Sigstore-verified only | TOFU prompt → policy persisted | Auto-run (toml toggle) |
| Module install | Refuse unsigned | Warn on unsigned, allow | Accept unsigned, info log |
| Validator scripts (`scripts/validate.py`) | Signed only | TOFU on first sight; OS sandbox enforced | Auto-run (toml toggle) |
| OS sandbox layer | Required (`[federal]` extras) | Required (`[enterprise]` extras) | Off |
| Capability lifecycle audit | Required, hard error on failure | Required, warn on failure | Optional, info-level |
| Audit log integrity | Tamper-evident chain (SignedChainSink) | Append-only (JsonlSink) | Best-effort |
| Skill `tools` field mismatch | Blocks registration | Warns, registers | Info-level |
| `arcagent.toml [security.validators]` | Required, externally managed | Seeded by `arc agent build`, user-managed | Empty default |

## What Gets Ripped Out (D-360, same edit)

```
DELETE entirely:
  packages/arcagent/src/arcagent/core/extensions.py            # 611 LOC
  packages/arcagent/src/arcagent/core/module_loader.py          # 257 LOC (runtime path)
  packages/arcagent/src/arcagent/tools/tool_tools.py            # in-memory create_tool wrapper
  packages/arcagent/src/arcagent/modules/*/MODULE.yaml          # all module YAMLs
  Hardcoded list in packages/arcagent/src/arcagent/tools/__init__.py
  [tools.native] block in packages/arcagent/src/arcagent/core/config.py
  [extensions] block in core/config.py
  ExtensionEntry/ExtensionConfig in core/config.py
  agent.py:_load_modules_by_convention (~30 LOC)
  agent.py:376-444 four-path startup (~70 LOC)
  agent.py:_setup_tool_prompt_injection + _setup_skill_prompt_injection (replaced by C-008)
  tool_registry.py:register_native_tools + _validate_module_path (~40 LOC)
  skill_registry.py (replaced by SkillEntry kind in CapabilityRegistry)

REPURPOSE:
  agent.py:reload() — simplified to delegate to CapabilityLoader.reload()
  packages/arcagent/src/arcagent/modules/*/MODULE.yaml — moved to arcmodules/<name>/MODULE.yaml as packaging metadata for `arc module install` only

KEEP and EXTEND:
  arcagent/tools/_decorator.py (extend per C-003)
  arcagent/tools/_dynamic_loader.py (extend per C-004)
  arctrust.policy.PolicyPipeline (TofuLayer plugs in per C-006)
  arctrust.audit (events emit through existing surface)
```

## Migration Validation Matrix

After all migrations, the following must be true:

| Assertion | How to verify |
|-----------|---------------|
| All 8 modules' existing test suites pass on migrated form | `pytest tests/unit/modules/` green |
| No `MODULE.yaml` files remain in arcagent.modules | `find packages/arcagent/src/arcagent/modules -name MODULE.yaml` returns empty |
| `arcagent/core/extensions.py` does not exist | `test -f` fails |
| `arcagent/core/` total LOC < 3,500 | `find packages/arcagent/src/arcagent/core -name '*.py' \| xargs wc -l` |
| Agent boots, registers all builtins + enabled modules | `arc agent run --dry-run` lists expected capabilities |
| Agent calls `reload()` and sees diff | integration test |
| Federal tier rejects unsigned `<agent_root>/workspace/.capabilities/foo.py` | integration test with `tier = "federal"` |
| Enterprise tier prompts on first sight, persists, auto-allows on second sight | integration test |
| Quality gates green | `mypy --strict`, `ruff check`, `pytest --cov=arcagent`, `pip-audit` |
