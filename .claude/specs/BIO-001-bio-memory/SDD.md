# SDD: Bio-Memory Module (BIO-001)

**Version**: 1.0
**Date**: 2026-02-21
**Phase**: 1 (Core)

---

## 1. Architecture

### 1.1 Module Structure

```
arcagent/modules/bio_memory/
    __init__.py              # Public API + __all__
    MODULE.yaml              # Module manifest
    bio_memory_module.py     # Facade (BioMemoryModule) — Module protocol
    config.py                # BioMemoryConfig (Pydantic, extends ModuleConfig)
    working_memory.py        # WorkingMemory helper
    identity_manager.py      # IdentityManager helper
    retriever.py             # Retriever helper (grep + wiki-link traversal)
    consolidator.py          # Consolidator helper (light consolidation)
    errors.py                # Module-specific errors
    cli.py                   # CLI commands

arcagent/utils/sanitizer.py  # Shared sanitizer (NFKC, zero-width strip, etc.)
                              # Used by bio_memory AND markdown_memory
```

Existing `modules/memory/` stays as-is (renamed conceptually to "markdown-memory" but directory unchanged). Bio-memory is a NEW module directory `modules/bio_memory/`.

### 1.2 Existing Code Impact

The existing `MarkdownMemoryModule` at `modules/memory/` is NOT modified. Key reuse:
- `arcagent/utils/io.py` — `CHARS_PER_TOKEN`, `atomic_write_text`, `sanitize_fts5_query`
- `arcagent/utils/model_helpers.py` — `get_eval_model`, `spawn_background`
- `arcagent/core/module_bus.py` — `Module` protocol, `EventContext`, `ModuleContext`
- `arcagent/core/config.py` — `EvalConfig`, `ModuleEntry`, existing config structure

New shared utility:
- `arcagent/utils/sanitizer.py` — extract `_sanitize_fact_text` logic from `entity_extractor.py` into shared module. Both memory modules import from here.

### 1.3 Dependency Graph

```
BioMemoryModule (facade)
    ├── WorkingMemory
    │     └── utils/io.py (CHARS_PER_TOKEN, atomic_write_text)
    ├── IdentityManager
    │     └── utils/io.py (atomic_write_text)
    ├── Retriever
    │     └── utils/sanitizer.py (sanitize_text)
    ├── Consolidator
    │     ├── utils/model_helpers.py (get_eval_model, spawn_background)
    │     └── utils/io.py (atomic_write_text)
    └── config.py (BioMemoryConfig)
          └── modules/base_config.py (ModuleConfig)
```

No imports from `modules/memory/`. Clean separation.

## 2. Component Design

### 2.1 BioMemoryModule (Facade)

**File**: `bio_memory_module.py`
**Implements**: `Module` protocol (`name`, `startup`, `shutdown`)

```python
class BioMemoryModule:
    """Biologically-inspired memory module.

    Facade that delegates to internal helpers:
    - WorkingMemory: scratchpad lifecycle
    - IdentityManager: how-i-work.md injection and updates
    - Retriever: grep + wiki-link graph traversal
    - Consolidator: light consolidation on shutdown
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        eval_config: EvalConfig | None = None,
        telemetry: Any = None,
        workspace: Path = Path("."),
        llm_config: Any | None = None,
    ) -> None: ...

    @property
    def name(self) -> str:
        return "bio_memory"

    async def startup(self, ctx: ModuleContext) -> None:
        """Register bus handlers + memory tools."""

    async def shutdown(self) -> None:
        """Cancel background tasks."""
```

**Bus subscriptions** (registered in `startup`):

| Event | Handler | Priority |
|-------|---------|----------|
| `agent:assemble_prompt` | `_on_assemble_prompt` | 50 |
| `agent:post_respond` | `_on_post_respond` | 100 |
| `agent:pre_tool` | `_on_pre_tool` | 10 |
| `agent:post_tool` | `_on_post_tool` | 100 |
| `agent:shutdown` | `_on_shutdown` | 100 |

**Tool registration**: Four tools via `ctx.tool_registry.register()`:
- `memory_search`
- `memory_note`
- `memory_recall`
- `memory_reflect`

**Constructor pattern**: Matches existing `MarkdownMemoryModule` — same parameter names for `ModuleLoader._instantiate` compatibility.

### 2.2 BioMemoryConfig

**File**: `config.py`

```python
class BioMemoryConfig(ModuleConfig):
    """Bio-memory configuration. All fields have defaults."""

    # Token budgets
    total_per_turn: int = 4000
    identity_budget: int = 500
    retrieved_budget: int = 3000
    working_budget: int = 500
    overflow_strategy: str = "truncate"  # truncate | summarize | skip_least_connected

    # Consolidation
    light_on_shutdown: bool = True
    significance_model: str = "llm"

    # Paths (relative to workspace/memory/)
    working_filename: str = "working.md"
    identity_filename: str = "how-i-work.md"
    episodes_dirname: str = "episodes"
```

### 2.3 WorkingMemory

**File**: `working_memory.py`

Manages `memory/working.md` lifecycle.

```python
class WorkingMemory:
    """Working memory scratchpad — overwritten every turn, cleared on session end."""

    def __init__(self, memory_dir: Path, config: BioMemoryConfig) -> None: ...

    async def read(self) -> str:
        """Read current working.md content. Returns empty string if missing."""

    async def write(self, content: str, frontmatter: dict[str, Any]) -> None:
        """Overwrite working.md with frontmatter + body. Enforces token budget."""

    async def clear(self) -> None:
        """Clear working.md (write empty content). Called on session end."""

    def estimate_tokens(self, text: str) -> int:
        """Character-based token estimation using CHARS_PER_TOKEN."""
```

**Key behaviors**:
- `write()` generates YAML frontmatter (topics, tags, entity_refs, importance, turn_number, timestamp) + markdown body
- Budget enforcement: if body exceeds `working_budget`, truncates from top (oldest entries)
- Uses `atomic_write_text` for crash safety
- `clear()` writes empty frontmatter (preserves file for workspace detection)

### 2.4 IdentityManager

**File**: `identity_manager.py`

Manages `memory/how-i-work.md` — the agent's learned behavioral patterns.

```python
class IdentityManager:
    """Identity file (how-i-work.md) — read at start, updated during consolidation."""

    def __init__(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
        telemetry: Any,
    ) -> None: ...

    async def read(self) -> str:
        """Read how-i-work.md. Returns empty string for new agents."""

    async def inject_context(self) -> str:
        """Read and format for prompt injection. Enforces identity_budget."""

    async def update(self, new_content: str) -> None:
        """Write updated identity. Emits audit event. Uses atomic write."""

    def is_over_budget(self, content: str) -> bool:
        """Check if content exceeds identity_budget tokens."""
```

**Key behaviors**:
- `inject_context()` reads file, truncates to `identity_budget` tokens, formats with header
- `update()` emits `identity.modified` audit event (OTel + JSONL, matching existing `IdentityAuditor` pattern)
- Before/after snapshot for audit trail (NIST 800-53 AU-2)
- Uses `atomic_write_text`
- Frontmatter: `last_updated`, `token_count`, `version`

### 2.5 Retriever

**File**: `retriever.py`

Grep-based retrieval with wiki-link following.

```python
class Retriever:
    """Grep + wiki-link graph traversal across memory tiers."""

    def __init__(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
    ) -> None: ...

    async def search(
        self,
        query: str,
        top_k: int = 10,
        scope: str | None = None,
    ) -> list[RetrievalResult]:
        """Two-pass search: frontmatter grep → full-text on matches → wiki-link follow."""

    async def recall(self, name: str) -> str | None:
        """Retrieve specific entity/episode by name (exact match on slug/frontmatter)."""

    def _frontmatter_grep(self, query: str, files: list[Path]) -> list[Path]:
        """Pass 1: grep YAML frontmatter blocks for tag/entity matches."""

    def _fulltext_grep(self, query: str, files: list[Path]) -> list[ScoredMatch]:
        """Pass 2: full-text search on matched files, score by match count."""

    def _follow_wiki_links(self, content: str, depth: int = 1) -> list[Path]:
        """Extract [[wiki-links]] from content, resolve to file paths, one hop."""

    def _discover_files(self) -> list[Path]:
        """Find all indexable files: episodes/*.md, how-i-work.md, working.md."""

    def _enforce_budget(self, results: list[RetrievalResult]) -> list[RetrievalResult]:
        """Apply overflow strategy to fit within retrieved_budget tokens."""
```

**`RetrievalResult` dataclass**:
```python
@dataclass
class RetrievalResult:
    source: str       # Relative path
    content: str      # File content (possibly truncated)
    score: float      # Match relevance
    match_type: str   # "frontmatter", "fulltext", "wiki_link"
```

**Key behaviors**:
- Two-pass search (research insight): grep frontmatter first, then full-text on matched subset
- Wiki-link extraction: regex `\[\[([^\]]+)\]\]` → resolve to `{slug}.md` → read if exists
- One-hop depth limit (decision P-003)
- Entity registry: only follow links to files that exist (no auto-creation — research insight on dangling link injection)
- Boundary markers: wrap results in `<memory-result>` tags with randomized session UUID (research insight)
- Budget enforcement via configured `overflow_strategy`

### 2.6 Consolidator

**File**: `consolidator.py`

Light consolidation on session end.

```python
class Consolidator:
    """Light consolidation — significance evaluation + identity update on shutdown."""

    def __init__(
        self,
        memory_dir: Path,
        config: BioMemoryConfig,
        identity: IdentityManager,
        working: WorkingMemory,
        telemetry: Any,
    ) -> None: ...

    async def light_consolidate(
        self,
        messages: list[dict[str, Any]],
        model: Any,
    ) -> None:
        """Run light consolidation sequence:
        1. Evaluate session significance (LLM judgment)
        2. If significant: create episode file
        3. Evaluate identity update need
        4. If identity changed: update how-i-work.md
        5. Clear working.md
        """

    async def _evaluate_significance(
        self,
        messages: list[dict[str, Any]],
        model: Any,
    ) -> bool:
        """LLM judges if session was significant enough to record."""

    async def _create_episode(
        self,
        messages: list[dict[str, Any]],
        model: Any,
    ) -> None:
        """Create episode file with frontmatter + LLM narrative body."""

    async def _evaluate_identity_update(
        self,
        messages: list[dict[str, Any]],
        current_identity: str,
        model: Any,
    ) -> str | None:
        """LLM evaluates if how-i-work.md needs updating. Returns new content or None."""
```

**Key behaviors**:
- Runs via `spawn_background` (non-blocking, failure-tolerant)
- Episode naming: `YYYY-MM-DD-{llm-slug}.md`
- LLM prompts use separate system/user messages (not combined — prevents injection)
- Content-hash: hash working.md content, skip if identical to previous consolidation
- Atomic writes for episode creation and identity updates
- Emits telemetry: `memory.consolidated`, `memory.episode_created`, `memory.identity_updated`

### 2.7 Shared Sanitizer

**File**: `arcagent/utils/sanitizer.py`

Extracted from `entity_extractor.py._sanitize_fact_text` to share between modules.

```python
def sanitize_text(text: str, max_length: int = 2000) -> str:
    """Sanitize text for memory storage.

    1. NFKC normalization (collapses confusable characters)
    2. Strip zero-width characters
    3. Strip ASCII control characters
    4. Enforce length limit
    """

def sanitize_wiki_link(link: str) -> str | None:
    """Sanitize a wiki-link target.

    Rejects:
    - Path traversal (../)
    - Non-alphanumeric slugs after normalization
    - Links exceeding max length
    Returns normalized slug or None if invalid.
    """
```

### 2.8 MODULE.yaml

```yaml
name: bio_memory
version: 0.1.0
description: Biologically-inspired memory with working memory, identity, episodes, and graph retrieval
author: ArcAgent
entry_point: arcagent.modules.bio_memory:BioMemoryModule
cli_entry: arcagent.modules.bio_memory.cli:cli_group
dependencies:
  - arcagent.core.module_bus
  - arcagent.core.config
events:
  subscribes:
    - agent:assemble_prompt
    - agent:post_respond
    - agent:pre_tool
    - agent:post_tool
    - agent:shutdown
  emits:
    - memory.consolidated
    - memory.episode_created
    - memory.identity_updated
    - memory.retrieval
    - identity.modified
```

## 3. Mutual Exclusivity Enforcement

Both modules register as `name: "memory"` or `name: "bio_memory"` via MODULE.yaml. The exclusivity check happens at config validation time:

**Option**: Add validation in `BioMemoryConfig.__init__` or in the module's `startup()`:

```python
async def startup(self, ctx: ModuleContext) -> None:
    # Check mutual exclusivity
    if ctx.bus.get_module("memory") is not None:
        raise ConfigError(
            code="CONFIG_MODULE_CONFLICT",
            message="bio_memory and memory modules are mutually exclusive. "
                    "Disable one in [modules] config.",
        )
    # ... register handlers
```

This is simpler than adding core validation logic — the module itself enforces the constraint.

## 4. Tool Schemas

### 4.1 memory_search

```json
{
  "name": "memory_search",
  "description": "Search agent memory using grep + wiki-link graph traversal.",
  "input_schema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "Search query (keywords or entity names)",
        "maxLength": 500
      },
      "scope": {
        "type": "string",
        "description": "Filter: episodes, identity, working",
        "enum": ["episodes", "identity", "working"]
      },
      "top_k": {
        "type": "integer",
        "description": "Maximum results",
        "default": 5
      }
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

### 4.2 memory_note

```json
{
  "name": "memory_note",
  "description": "Record a note — appends to working memory or creates an episode.",
  "input_schema": {
    "type": "object",
    "properties": {
      "content": {
        "type": "string",
        "description": "Note content to record",
        "maxLength": 2000
      },
      "target": {
        "type": "string",
        "description": "Where to write: working (default) or episode",
        "enum": ["working", "episode"],
        "default": "working"
      }
    },
    "required": ["content"],
    "additionalProperties": false
  }
}
```

### 4.3 memory_recall

```json
{
  "name": "memory_recall",
  "description": "Recall a specific memory by name (episode or entity).",
  "input_schema": {
    "type": "object",
    "properties": {
      "name": {
        "type": "string",
        "description": "Name or slug of the memory to recall",
        "maxLength": 200
      }
    },
    "required": ["name"],
    "additionalProperties": false
  }
}
```

### 4.4 memory_reflect

```json
{
  "name": "memory_reflect",
  "description": "Trigger a reflection on recent memories to update identity patterns.",
  "input_schema": {
    "type": "object",
    "properties": {
      "focus": {
        "type": "string",
        "description": "Optional focus area for reflection",
        "maxLength": 500
      }
    },
    "additionalProperties": false
  }
}
```

## 5. Data Flow

### 5.1 Session Lifecycle

```
agent:assemble_prompt
    → IdentityManager.inject_context()  → inject how-i-work.md (≤500 tokens)
    → WorkingMemory.read()              → inject working.md (≤500 tokens)

agent:post_respond  (each turn)
    → WorkingMemory.write(turn_summary, frontmatter)

agent:pre_tool
    → Bash veto if targeting memory/ paths

agent:post_tool
    → Audit event if memory file modified

agent:shutdown
    → Consolidator.light_consolidate(messages, model)
        → spawn_background (non-blocking)
        → evaluate significance → create episode if yes
        → evaluate identity update → update how-i-work.md if yes
        → WorkingMemory.clear()
```

### 5.2 Search Flow

```
memory_search("project timeline")
    → Retriever.search(query)
        → Pass 1: _frontmatter_grep → filter files by tag/entity match
        → Pass 2: _fulltext_grep → score matched files
        → _follow_wiki_links → one-hop expansion
        → _enforce_budget → trim to retrieved_budget
        → Wrap in boundary markers
    → Return formatted results
```

## 6. Disk Layout

```
{workspace}/memory/
    working.md              # Scratchpad (overwritten/turn)
    how-i-work.md           # Learned identity (LLM-maintained)
    episodes/               # Significant moments (append-only)
        2026-02-21-deadline-change.md
        2026-02-20-pricing-correction.md
```

Under existing workspace convention (decision A-006).

## 7. Error Handling

| Error | Type | Recovery |
|-------|------|----------|
| Eval model unavailable | Skip consolidation | Log warning, session ends normally |
| Disk write failure | `BioMemoryError` | Atomic write prevents partial writes |
| Token budget exceeded | Truncation | Apply overflow strategy |
| LLM call timeout | Skip that step | Background task timeout catches it |
| Module conflict | `ConfigError` | Startup fails with clear message |
| Corrupted frontmatter | Skip file | Log warning, exclude from results |

## 8. Telemetry Events

| Event | When | Data |
|-------|------|------|
| `memory.retrieval` | Each search | files_count, tokens, path_type, query_hash |
| `memory.consolidated` | After light consolidation | significant (bool), episode_created (bool), identity_updated (bool) |
| `memory.episode_created` | Episode written | episode_name, entities_touched, significance |
| `memory.identity_updated` | how-i-work.md changed | before_tokens, after_tokens |
| `identity.modified` | how-i-work.md written | before_length, after_length (NIST AU-2) |

## 9. Security Considerations

### 9.1 Memory Poisoning Defense (from research)

| Layer | What | Implementation |
|-------|------|----------------|
| L0 | Input sanitization | `sanitize_text()` on all writes (NFKC, zero-width strip) |
| L1 | Write validation | Pydantic schema on frontmatter, field length limits |
| L4 | Graph traversal safety | Only follow links to existing files (no auto-creation), depth limit=1, sanitize link targets |
| L5 | Boundary markers | Randomized per-session UUID in `<memory-result>` tags |

L2 (crypto) and L3 (consolidation security) are Phase 4 and Phase 2 respectively.

### 9.2 Prompt Injection Defense

- Memory search results wrapped in boundary markers (existing pattern from `MarkdownMemoryModule`)
- LLM prompts for consolidation use system/user message separation
- Extraction prompt includes anti-injection preamble (existing pattern from `EntityExtractor`)
- Working.md content is agent-generated (lower injection risk than user-supplied)
