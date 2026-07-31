# SDD: Memory Module (Markdown Memory)

**Spec ID**: S002
**Status**: PENDING
**Last Updated**: 2026-02-15

---

## 1. Architecture Overview

### 1.1 System Context

```
┌──────────────────────────────────────────────────────────────┐
│                     ArcAgent Core (Nucleus)                   │
│                                                               │
│  ┌───────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │ Config    │  │ SessionMgr   │  │ ContextManager         │ │
│  │ (+[eval]) │  │ (NEW)        │  │ (+assemble_prompt evt) │ │
│  └─────┬─────┘  └──────┬───────┘  └───────────┬────────────┘ │
│        │               │                      │               │
│  ┌─────┴───────────────┴──────────────────────┴─────────────┐ │
│  │              Agent (+ chat() method)                      │ │
│  │  startup → chat → arcrun.run(messages=...) → process      │ │
│  └─────┬─────────────────────────────────────────────┬──────┘ │
│        │                                             │        │
│  ┌─────┴──────────┐                     ┌───────────┴──────┐ │
│  │ Module Bus     │                     │ Tool Registry    │ │
│  │ (events)       │                     │ (pre/post hooks) │ │
│  └─────┬──────────┘                     └──────────────────┘ │
└────────┼─────────────────────────────────────────────────────┘
         │
    ┌────┴─────────────────────────────────────┐
    │         Memory Module (subscriber)        │
    │                                           │
    │  ┌──────────────┐  ┌──────────────────┐  │
    │  │ NoteManager  │  │ EntityExtractor  │  │
    │  │ (append-only)│  │ (async, eval LLM)│  │
    │  └──────────────┘  └──────────────────┘  │
    │  ┌──────────────┐  ┌──────────────────┐  │
    │  │ ContextGuard │  │ IdentityAuditor  │  │
    │  │ (2K budget)  │  │ (audit trail)    │  │
    │  └──────────────┘  └──────────────────┘  │
    │  ┌──────────────┐  ┌──────────────────┐  │
    │  │ HybridSearch │  │ PolicyEngine     │  │
    │  │ (BM25+vec)   │  │ (ACE framework) │  │
    │  └──────────────┘  └──────────────────┘  │
    └──────────────────────────────────────────┘
```

### 1.2 Component Dependency Order

```
1. config.py            (MODIFY: add [eval], [memory], [session] sections)
2. session_manager.py   (NEW: depends on config)
3. context_manager.py   (MODIFY: add assemble_prompt event emission)
4. agent.py             (MODIFY: add chat(), integrate SessionManager)
5. Memory module        (depends on: bus, config, telemetry, eval model)
   ├── markdown_memory.py   (main module, subscribes to bus events)
   ├── entity_extractor.py  (async extraction via eval model)
   ├── hybrid_search.py     (BM25 + sqlite-vec)
   └── policy_engine.py     (ACE Reflector + Curator)
```

### 1.3 LOC Budget

| Component | Budget | Location | Notes |
|-----------|--------|----------|-------|
| session_manager.py | ~250 | core/ | New core component |
| config.py changes | ~80 | core/ | EvalConfig, MemoryConfig, SessionConfig |
| context_manager.py changes | ~40 | core/ | assemble_prompt event |
| agent.py changes | ~80 | core/ | chat(), SessionManager integration |
| markdown_memory.py | ~400 | modules/memory/ | Main module + hook routing |
| entity_extractor.py | ~250 | modules/memory/ | LLM extraction + JSONL storage |
| hybrid_search.py | ~350 | modules/memory/ | BM25 + sqlite-vec + lazy index |
| policy_engine.py | ~300 | modules/memory/ | ACE Reflector + Curator |
| **Core additions** | **~450** | | Within 3K budget (~1,950 total) |
| **Module total** | **~1,300** | | Under 1,500 LOC budget |

---

## 2. Core Changes

### 2.1 Config Changes (config.py — MODIFY)

**Purpose**: Add configuration sections for eval model, memory, and sessions.

#### New Models

```python
class EvalConfig(BaseModel):
    """Configuration for the evaluation/background model."""
    provider: str = ""  # Empty = use same provider as agent
    model: str = ""     # Empty = use agent's model
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
    policy_net_negative_threshold: int = 3


class SessionConfig(BaseModel):
    """Configuration for session management."""
    retention_count: int = 50  # Keep last N sessions
    retention_days: int = 30   # Or sessions from last N days
    compaction_summary_max_chars: int = 2000
```

#### Root Config Update

```python
class ArcAgentConfig(BaseSettings):
    # ... existing sections ...
    eval: EvalConfig = EvalConfig()
    memory: MemoryConfig = MemoryConfig()
    session: SessionConfig = SessionConfig()
```

### 2.2 SessionManager (session_manager.py — NEW)

**Purpose**: Manage conversation sessions — message history, JSONL persistence, compaction. New core component alongside ContextManager.

#### Key Class

```python
class SessionManager:
    def __init__(self, config: SessionConfig, context_config: ContextConfig,
                 telemetry: AgentTelemetry, workspace: Path) -> None:
        self._config = config
        self._context_config = context_config
        self._telemetry = telemetry
        self._workspace = workspace
        self._sessions_dir = workspace / "sessions"
        self._messages: list[dict[str, Any]] = []
        self._session_id: str = ""
        self._lock = asyncio.Lock()
        self._jsonl_file: Path | None = None

    async def create_session(self) -> str:
        """Create new session with UUID4 ID. Opens JSONL file for writing."""

    async def resume_session(self, session_id: str) -> list[dict[str, Any]]:
        """Load messages from existing JSONL file. Skips malformed lines."""

    async def append_message(self, message: dict[str, Any]) -> None:
        """Thread-safe append to message list + JSONL file.
        Uses asyncio.Lock for concurrency safety.
        Appends single JSON line to file.
        """

    def get_messages(self) -> list[dict[str, Any]]:
        """Return current message list (snapshot, not reference)."""

    async def compact(self, model: Any, workspace: Path) -> None:
        """Letta-style sliding window compaction with pre-compaction flush.
        Triggered when token estimate > compact_threshold.
        0. PRE-FLUSH: Extract key facts from messages-to-summarize,
           append to context.md (OpenClaw pattern — never lose info)
        1. Split messages: oldest 30% → to_summarize, recent 70% → to_keep
        2. Summarize oldest via eval model
        3. Replace summarized messages with compaction_summary entry
        4. Write compaction_summary to JSONL
        5. Emit agent:compact event
        """

    async def _pre_compact_flush(self, messages: list[dict[str, Any]],
                                  workspace: Path, model: Any) -> None:
        """Flush key facts from messages-about-to-be-compacted to context.md.
        Uses eval model to extract important facts, decisions, and state.
        Appends to context.md (respects ContextGuard budget on next write).
        Ensures no important information is lost to compaction.
        """

    async def cleanup_old_sessions(self) -> None:
        """Remove sessions beyond retention limits."""

    @property
    def session_id(self) -> str: ...

    @property
    def message_count(self) -> int: ...
```

#### JSONL Format

```jsonl
{"type": "message", "role": "system", "content": "...", "timestamp": "2026-02-15T10:00:00Z"}
{"type": "message", "role": "user", "content": "my name is Josh", "timestamp": "2026-02-15T10:00:01Z"}
{"type": "message", "role": "assistant", "content": "Nice to meet you, Josh!", "timestamp": "2026-02-15T10:00:03Z"}
{"type": "compaction_summary", "summarized_count": 15, "summary": "...", "timestamp": "2026-02-15T11:00:00Z"}
```

### 2.3 ContextManager Changes (context_manager.py — MODIFY)

**Purpose**: Enable modules to inject content into the system prompt via event.

#### Changes

```python
class ContextManager:
    def __init__(self, config: ContextConfig, telemetry: AgentTelemetry,
                 bus: ModuleBus) -> None:  # NEW: bus parameter
        # ... existing init ...
        self._bus = bus

    async def assemble_system_prompt(self, workspace: Path) -> str:
        """Build system prompt from workspace files.
        CHANGED: Now async. Emits agent:assemble_prompt event.

        1. Read identity.md, policy.md, context.md
        2. Build PromptSections dict
        3. Emit agent:assemble_prompt with sections (handlers can inject)
        4. Concatenate sections into final string
        """
        sections: dict[str, str] = {}
        for name in self._PROMPT_FILES:
            path = workspace / name
            if path.exists():
                sections[name.removesuffix('.md')] = path.read_text()

        # Let modules inject content
        ctx = await self._bus.emit("agent:assemble_prompt", {
            "sections": sections,
            "workspace": str(workspace),
        })

        # Build final prompt from sections (order: identity, notes, policy, context)
        parts = []
        for key in ["identity", "notes", "policy", "context"]:
            if key in sections and sections[key]:
                parts.append(f"## {key.title()}\n\n{sections[key]}")

        return "\n\n".join(parts)
```

### 2.4 Agent Orchestrator Changes (agent.py — MODIFY)

**Purpose**: Add multi-turn `chat()` method and SessionManager integration.

#### Changes

```python
class ArcAgent:
    async def startup(self) -> None:
        """MODIFIED: Add SessionManager initialization after Config.
        Order: config → vault → telemetry → identity → bus → tools →
               context → session_manager → modules → emit agent:init
        """
        # ... existing startup ...
        self._session = SessionManager(
            self._config.session,
            self._config.context,
            self._telemetry,
            Path(self._config.agent.workspace),
        )

    async def chat(self, message: str, session_id: str | None = None) -> str:
        """Multi-turn conversation with persistent message history.

        1. Create or resume session
        2. Append user message to session
        3. Load LLM model
        4. Build tool list
        5. Assemble system prompt (async, with module injection)
        6. Call arcrun.run(model, tools, system, task=message,
                          messages=session.get_messages(),
                          on_event=bridge, transform_context=...)
        7. Append assistant response to session
        8. Process LoopResult
        9. Return response text
        """

    async def run(self, task: str) -> Any:
        """UNCHANGED: Single-shot task execution (no session)."""
```

---

## 3. Memory Module Design

### 3.1 Main Module (markdown_memory.py)

**Purpose**: Single Module Bus subscriber that routes events to internal helper classes.

#### Key Class

```python
class MarkdownMemoryModule:
    """Module Bus subscriber providing 3-tier persistent memory.

    Implements the Module protocol. Delegates to:
    - NoteManager: append-only daily notes
    - ContextGuard: context.md token budget enforcement
    - IdentityAuditor: identity.md audit trail
    - EntityExtractor: async post-response entity extraction
    - HybridSearch: BM25 + vector search
    - PolicyEngine: ACE self-learning framework
    """

    def __init__(self, config: MemoryConfig, eval_config: EvalConfig,
                 telemetry: AgentTelemetry, workspace: Path) -> None:
        self._config = config
        self._workspace = workspace
        self._telemetry = telemetry

        # Internal helpers (injected for testability)
        self._notes = NoteManager(workspace, config)
        self._context_guard = ContextGuard(config.context_budget_tokens)
        self._identity_auditor = IdentityAuditor(workspace, telemetry)
        self._extractor = EntityExtractor(eval_config, workspace, telemetry)
        self._search = HybridSearch(workspace, config)
        self._policy = PolicyEngine(eval_config, workspace, telemetry, config)

        self._background_tasks: set[asyncio.Task[None]] = set()
        self._hook_active: bool = False
        self._eval_semaphore = asyncio.Semaphore(eval_config.max_concurrent)

    @property
    def name(self) -> str:
        return "memory"

    async def startup(self, bus: ModuleBus) -> None:
        """Register all event handlers."""
        bus.subscribe("agent:pre_tool", self._on_pre_tool, priority=10)
        bus.subscribe("agent:post_tool", self._on_post_tool, priority=100)
        bus.subscribe("agent:assemble_prompt", self._on_assemble_prompt, priority=50)
        bus.subscribe("agent:post_respond", self._on_post_respond, priority=100)

        # Register memory_search tool
        bus.subscribe("agent:init", self._register_search_tool, priority=100)

    async def shutdown(self) -> None:
        """Cancel background tasks, close search DB."""
        for task in self._background_tasks:
            task.cancel()
        await asyncio.gather(*self._background_tasks, return_exceptions=True)
        await self._search.close()

    async def _on_pre_tool(self, ctx: EventContext) -> None:
        """Route pre-tool events based on target file path."""
        if self._hook_active:
            return  # Guard re-entrancy

        tool_name = ctx.data.get("tool", "")
        args = ctx.data.get("args", {})

        # Extract path from tool args
        path = self._resolve_path(tool_name, args)
        if path is None:
            return

        self._hook_active = True
        try:
            rel = path.relative_to(self._workspace)
            rel_str = str(rel)

            # Route by path convention
            if rel_str.startswith("notes/"):
                self._notes.enforce_append_only(ctx, tool_name)
            elif rel_str == "context.md":
                await self._context_guard.enforce_budget(ctx, args)
            elif rel_str == "identity.md":
                await self._identity_auditor.capture_before(ctx, path)
        except ValueError:
            pass  # Path not under workspace — ignore
        finally:
            self._hook_active = False

    async def _on_post_tool(self, ctx: EventContext) -> None:
        """Handle post-tool events for identity audit logging."""
        tool_name = ctx.data.get("tool", "")
        args = ctx.data.get("args", {})
        path = self._resolve_path(tool_name, args)
        if path is None:
            return

        try:
            rel = str(path.relative_to(self._workspace))
            if rel == "identity.md":
                await self._identity_auditor.capture_after(ctx, path)
        except ValueError:
            pass

    async def _on_assemble_prompt(self, ctx: EventContext) -> None:
        """Inject recent notes into system prompt sections."""
        sections = ctx.data.get("sections", {})
        notes_content = await self._notes.get_recent_notes()
        if notes_content:
            sections["notes"] = notes_content

    async def _on_post_respond(self, ctx: EventContext) -> None:
        """Fire async entity extraction and periodic policy evaluation."""
        messages = ctx.data.get("messages", [])
        turn = ctx.data.get("turn", 0)

        # Async entity extraction
        if self._config.entity_extraction_enabled and messages:
            self._spawn_background(
                self._extractor.extract(messages)
            )

        # Periodic policy evaluation (ACE Reflector)
        if turn > 0 and turn % self._config.policy_eval_interval_turns == 0:
            self._spawn_background(
                self._policy.evaluate(messages)
            )

    def _spawn_background(self, coro: Coroutine[Any, Any, None]) -> None:
        """Fire-and-forget with task reference tracking and error logging."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task[None]) -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                self._telemetry.audit_event("memory.background_error", {
                    "error": str(exc),
                    "type": type(exc).__name__,
                })

        task.add_done_callback(_on_done)

    def _resolve_path(self, tool_name: str, args: dict[str, Any]) -> Path | None:
        """Extract and canonicalize file path from tool args.
        Handles: read, write, edit tools (path arg).
        Handles: bash tool (parse command for file targets).
        """
        if tool_name in ("read", "write", "edit"):
            raw = args.get("path") or args.get("file_path")
            if raw:
                return Path(raw).resolve()
        elif tool_name == "bash":
            cmd = args.get("command", "")
            return self._parse_bash_target(cmd)
        return None

    def _parse_bash_target(self, command: str) -> Path | None:
        """Parse bash command for file operations targeting workspace paths.
        Detects: echo > path, cat > path, rm path, mv path, cp path
        Returns resolved path if it targets workspace/notes/ or workspace/*.md
        """
        # Simple pattern matching for common file operations
        # Full shell parsing is out of scope — cover common cases
        import shlex
        try:
            tokens = shlex.split(command)
        except ValueError:
            return None

        for i, token in enumerate(tokens):
            if token in (">", ">>") and i + 1 < len(tokens):
                target = Path(tokens[i + 1]).resolve()
                if self._is_memory_path(target):
                    return target
            elif token in ("rm", "mv", "cp") and i + 1 < len(tokens):
                target = Path(tokens[i + 1]).resolve()
                if self._is_memory_path(target):
                    return target
        return None

    def _is_memory_path(self, path: Path) -> bool:
        """Check if path is under workspace memory directories."""
        try:
            rel = str(path.relative_to(self._workspace))
            return (rel.startswith("notes/") or
                    rel in ("identity.md", "policy.md", "context.md") or
                    rel.startswith("entities/"))
        except ValueError:
            return False
```

#### NoteManager (internal helper)

```python
class NoteManager:
    """Manages daily notes with append-only enforcement."""

    def __init__(self, workspace: Path, config: MemoryConfig) -> None:
        self._notes_dir = workspace / "notes"
        self._config = config

    def enforce_append_only(self, ctx: EventContext, tool_name: str) -> None:
        """Veto non-append operations on notes files."""
        if tool_name == "write":
            ctx.veto("Notes are append-only. Use 'edit' to append content.")
        elif tool_name in ("bash",):
            ctx.veto("Notes are append-only. Cannot modify via bash.")
        # 'edit' is allowed (append), 'read' is allowed

    async def get_recent_notes(self) -> str:
        """Read today + yesterday notes, apply token budgets."""
        from datetime import date, timedelta
        today = date.today()
        yesterday = today - timedelta(days=1)

        parts: list[str] = []

        today_file = self._notes_dir / f"{today.isoformat()}.md"
        if today_file.exists():
            content = today_file.read_text()
            # Truncate to token budget
            parts.append(f"### Today ({today.isoformat()})\n\n{content}")

        yesterday_file = self._notes_dir / f"{yesterday.isoformat()}.md"
        if yesterday_file.exists():
            content = yesterday_file.read_text()
            parts.append(f"### Yesterday ({yesterday.isoformat()})\n\n{content}")

        return "\n\n".join(parts) if parts else ""
```

#### ContextGuard (internal helper)

```python
class ContextGuard:
    """Enforces token budget on context.md writes."""

    def __init__(self, budget_tokens: int) -> None:
        self._budget = budget_tokens

    async def enforce_budget(self, ctx: EventContext, args: dict[str, Any]) -> None:
        """Check if write would exceed context.md budget.
        If over budget: auto-truncate oldest entries, keep within budget.
        """
        content = args.get("content", "")
        estimated = len(content) // 4  # ~4 chars/token heuristic
        if estimated > self._budget:
            # Truncate from the top (oldest entries), keep recent
            lines = content.split("\n")
            kept: list[str] = []
            token_count = 0
            for line in reversed(lines):
                line_tokens = len(line) // 4
                if token_count + line_tokens > self._budget:
                    break
                kept.insert(0, line)
                token_count += line_tokens
            args["content"] = "\n".join(kept)
```

#### IdentityAuditor (internal helper)

```python
class IdentityAuditor:
    """Captures before/after state for identity.md changes."""

    def __init__(self, workspace: Path, telemetry: AgentTelemetry) -> None:
        self._workspace = workspace
        self._telemetry = telemetry
        self._audit_dir = workspace / "audit"
        self._before_content: str = ""

    async def capture_before(self, ctx: EventContext, path: Path) -> None:
        """Snapshot current identity.md content before write."""
        if path.exists():
            self._before_content = path.read_text()
        else:
            self._before_content = ""

    async def capture_after(self, ctx: EventContext, path: Path) -> None:
        """Log the change after write succeeds."""
        after_content = path.read_text() if path.exists() else ""
        if after_content == self._before_content:
            return  # No actual change

        # Telemetry audit event
        self._telemetry.audit_event("identity.modified", {
            "before_length": len(self._before_content),
            "after_length": len(after_content),
            "session_id": ctx.data.get("session_id", ""),
        })

        # JSONL defense-in-depth (NIST AU-9)
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = self._audit_dir / "identity-changes.jsonl"
        import json
        from datetime import datetime, timezone
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_did": ctx.agent_did,
            "before": self._before_content,
            "after": after_content,
            "session_id": ctx.data.get("session_id", ""),
        }
        with open(audit_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
```

### 3.2 Entity Extractor (entity_extractor.py)

**Purpose**: Async LLM-driven entity extraction from conversations.

#### Key Class

```python
class EntityExtractor:
    def __init__(self, eval_config: EvalConfig, workspace: Path,
                 telemetry: AgentTelemetry) -> None:
        self._eval_config = eval_config
        self._workspace = workspace
        self._telemetry = telemetry
        self._entities_dir = workspace / "entities"
        self._index_path = workspace / "entities" / "index.json"
        self._index_lock = asyncio.Lock()

    async def extract(self, messages: list[dict[str, Any]]) -> None:
        """Extract entities from the most recent exchange.

        1. Get last user + assistant message pair
        2. Skip trivial exchanges (< 20 chars combined)
        3. Call eval model with structured output prompt
        4. Parse extraction result
        5. For each entity:
           a. Check index for existing match (case-insensitive + alias)
           b. New → create directory, facts.jsonl, summary.md, update index
           c. Existing → append facts, detect contradictions, update index
        """

    async def _call_eval_model(self, prompt: str) -> dict[str, Any]:
        """Call eval model for entity extraction.
        Uses structured output (JSON mode).
        Respects semaphore, timeout, and fallback_behavior config.
        """

    async def _update_index(self, entity: EntityInfo) -> None:
        """Atomic index update.
        1. Acquire asyncio.Lock
        2. Read current index
        3. Merge entity (add or update)
        4. Write to index.json.tmp
        5. os.rename to index.json (atomic on POSIX)
        """

    async def _append_facts(self, entity_name: str,
                            facts: list[dict[str, Any]]) -> None:
        """Append facts to entity's facts.jsonl.
        Detect contradictions: same predicate, new value → mark old as superseded.
        """

    def _find_existing_entity(self, name: str,
                              index: dict[str, Any]) -> str | None:
        """Case-insensitive name + alias matching."""
```

#### Entity Index Format (index.json)

```json
{
  "version": 1,
  "entities": {
    "josh-schultz": {
      "name": "Josh Schultz",
      "type": "person",
      "aliases": ["Josh", "Joshua Schultz"],
      "last_updated": "2026-02-15T10:00:00Z",
      "fact_count": 5
    }
  }
}
```

#### Facts JSONL Format

```jsonl
{"predicate": "works_at", "value": "BlackArc", "confidence": 0.9, "source": "user_stated", "timestamp": "2026-02-15T10:00:00Z", "status": "active"}
{"predicate": "works_at", "value": "Anthropic", "confidence": 0.8, "source": "user_stated", "timestamp": "2026-02-16T10:00:00Z", "status": "active", "supersedes": "2026-02-15T10:00:00Z"}
```

#### Extraction Prompt Schema

```json
{
  "entities": [
    {
      "name": "canonical name",
      "type": "person|org|project|concept|location",
      "aliases": ["alternate names"],
      "facts": [
        {
          "predicate": "relationship or attribute",
          "value": "the value",
          "confidence": 0.9
        }
      ]
    }
  ]
}
```

### 3.3 Hybrid Search (hybrid_search.py)

**Purpose**: Combined BM25 keyword + vector similarity search across all memory tiers.

#### Key Class

```python
class HybridSearch:
    def __init__(self, workspace: Path, config: MemoryConfig) -> None:
        self._workspace = workspace
        self._config = config
        self._db_path = workspace / "search.db"
        self._conn: sqlite3.Connection | None = None
        self._model: Any = None  # Lazy-loaded sentence-transformers model
        self._last_indexed: dict[str, float] = {}  # path → mtime

    async def search(self, query: str, top_k: int = 10,
                     scope: str | None = None,
                     date_from: str | None = None,
                     date_to: str | None = None) -> list[SearchResult]:
        """Hybrid search across memory tiers.

        The LLM decides what to search and where via tool calling parameters.

        Args:
            query: Search query text
            top_k: Max results to return
            scope: Filter by source type. Options:
                   "notes" — daily notes only
                   "entities" — entity facts/summaries only
                   "context" — context.md only
                   "sessions" — session transcripts only
                   None — search all tiers (default)
            date_from: ISO date, filter notes/sessions from this date
            date_to: ISO date, filter notes/sessions to this date

        1. Reindex if source files changed (lazy)
        2. BM25 keyword search (filtered by scope if provided)
        3. Vector similarity search (if embeddings available)
        4. Merge results with configured weights
        5. Return top-K with source, score, content snippet
        """

    async def reindex_if_needed(self) -> None:
        """Check file modification timestamps. Reindex changed files.
        Uses WAL mode for concurrent read safety.
        Chunks at ~400 tokens, heading-boundary preferred.
        """

    async def rebuild(self) -> None:
        """Full reindex. Drop and recreate all tables."""

    async def close(self) -> None:
        """Close SQLite connection."""

    def _ensure_db(self) -> sqlite3.Connection:
        """Open or create search.db with sqlite-vec extension.
        Sets WAL mode. Creates tables if needed.
        Falls back to BM25-only if sqlite-vec unavailable.
        """

    def _load_embedding_model(self) -> Any:
        """Lazy-load all-MiniLM-L6-v2 on first vector search.
        Cold start: ~2-3 seconds, ~43MB RAM.
        """

    def _chunk_document(self, content: str, source: str) -> list[Chunk]:
        """Split content at ~400 tokens with heading-boundary preference."""

    def _bm25_search(self, query: str, top_k: int) -> list[SearchResult]:
        """SQLite FTS5 full-text search."""

    def _vector_search(self, query: str, top_k: int) -> list[SearchResult]:
        """sqlite-vec cosine similarity search."""

    def _merge_results(self, bm25: list[SearchResult],
                       vector: list[SearchResult],
                       top_k: int) -> list[SearchResult]:
        """Weighted merge. Reciprocal rank fusion or linear combination."""
```

#### SearchResult

```python
@dataclass
class SearchResult:
    source: str        # File path relative to workspace
    content: str       # Matched content snippet
    score: float       # Combined relevance score
    match_type: str    # "bm25", "vector", or "hybrid"
```

#### SQLite Schema

```sql
-- Full-text search (BM25)
CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
    content,
    source,
    chunk_id
);

-- Vector search (sqlite-vec)
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    embedding float[384],
    +source TEXT,
    +chunk_id TEXT
);

-- Metadata
CREATE TABLE IF NOT EXISTS search_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- key: "model_name", value: "all-MiniLM-L6-v2"
-- key: "last_full_index", value: "2026-02-15T10:00:00Z"

-- File tracking
CREATE TABLE IF NOT EXISTS indexed_files (
    path TEXT PRIMARY KEY,
    mtime REAL,
    chunk_count INTEGER
);
```

### 3.4 Policy Engine (policy_engine.py) — ACE Framework

**Purpose**: Self-improving agent policy using the ACE framework (arXiv:2510.04618). The agent learns from experience through structured evaluation and deterministic policy updates.

#### ACE Mapping

| ACE Concept | Our Implementation |
|-------------|-------------------|
| **Generator** | The agent itself, performing tasks and producing reasoning |
| **Reflector** | Eval model critiquing the agent's work after tasks/sessions |
| **Curator** | Deterministic merge logic updating policy.md with delta bullets |
| **Playbook** | `policy.md` with structured bullets containing metadata |
| **Delta context** | Individual bullet additions/modifications (never full rewrite) |

#### Key Class

```python
class PolicyEngine:
    """ACE-based self-learning policy engine.

    After multi-step tasks or at configured intervals:
    1. Reflector (eval model) evaluates agent performance
    2. Produces structured delta: lessons learned, bullets to update
    3. Curator (deterministic) merges deltas into policy.md
    """

    def __init__(self, eval_config: EvalConfig, workspace: Path,
                 telemetry: AgentTelemetry, memory_config: MemoryConfig) -> None:
        self._eval_config = eval_config
        self._workspace = workspace
        self._telemetry = telemetry
        self._config = memory_config
        self._policy_path = workspace / "policy.md"
        self._next_bullet_id: int = 0
        self._eval_semaphore = asyncio.Semaphore(eval_config.max_concurrent)

    async def evaluate(self, messages: list[dict[str, Any]]) -> None:
        """ACE Reflector: Evaluate recent agent behavior.

        1. Build reflection prompt from recent messages
        2. Call eval model for structured evaluation
        3. Parse delta (new bullets, counter updates, rewrites)
        4. Call curator to merge into policy.md
        """
        async with self._eval_semaphore:
            try:
                delta = await self._reflect(messages)
                if delta:
                    await self._curate(delta)
            except Exception:
                if self._eval_config.fallback_behavior == "error":
                    raise
                # "skip" mode: log and continue

    async def _reflect(self, messages: list[dict[str, Any]]) -> PolicyDelta | None:
        """Call eval model to evaluate agent behavior.

        Prompt structure:
        - Recent conversation context (last N messages)
        - Current policy bullets
        - Instructions: identify what worked, what didn't, lessons learned
        - Output: structured PolicyDelta

        Returns None if no actionable insights.
        """

    async def _curate(self, delta: PolicyDelta) -> None:
        """Deterministic merge of delta into policy.md.

        Operations (no LLM calls):
        1. Parse existing policy.md into bullet list
        2. For each delta operation:
           - ADD: new bullet at score 5 with session_id source
           - UPDATE: apply score_delta (clamp 1-10)
           - REWRITE: update text, apply score_delta, update source
           - AUTO-REMOVE: delete bullets with score <= 2
        3. Apply decay: unused 30+ days → score - 1 per period
        4. De-duplicate via semantic similarity (cosine > 0.85)
        5. Sort bullets by score descending (highest confidence first)
        6. Write updated policy.md (atomic write)
        """

    def _parse_policy(self, content: str) -> list[PolicyBullet]:
        """Parse policy.md into structured bullets."""

    def _serialize_policy(self, bullets: list[PolicyBullet]) -> str:
        """Render bullets back to policy.md format."""

    def _deduplicate(self, bullets: list[PolicyBullet]) -> list[PolicyBullet]:
        """Remove semantically duplicate bullets (cosine > 0.85)."""

    def _apply_decay(self, bullets: list[PolicyBullet]) -> list[PolicyBullet]:
        """Decay unused bullets (30+ days without use → decrement)."""

    def _next_id(self) -> str:
        """Generate next bullet ID: P01, P02, ..."""
        self._next_bullet_id += 1
        return f"P{self._next_bullet_id:02d}"
```

#### Policy Bullet Format

```python
@dataclass
class PolicyBullet:
    id: str           # "P01", "P02", ...
    text: str         # The lesson/rule text
    score: int        # 1-10 effectiveness score
    uses: int         # Times actively referenced
    reviewed: str     # ISO date of last review
    created: str      # ISO date of creation
    source: str       # Session ID that created/last updated this bullet
```

#### Score Thresholds

| Score Range | Status | Action |
|-------------|--------|--------|
| <= 2 | **Remove** | Bullet deleted — not helpful or actively harmful |
| 3-4 | **At risk** | Needs improvement or will decay out |
| 5-7 | **Improve** | Rewrite text to be more actionable, adjust score |
| 8-10 | **Promote** | Move up in priority, high-confidence rule |

- New bullets start at score 5 (neutral, needs to prove itself)
- Positive outcome → score + 1 (cap at 10)
- Negative outcome → score - 2 (penalize faster than reward)
- Partial / needs rewrite → score stays, text updated
- Unused 30+ days → score - 1 per period (decay)
- Bullets at score <= 2 auto-removed on next evaluation

#### policy.md Format

```markdown
# Policy

- [P01] When writing code, always run tests before claiming success {score:9, uses:8, reviewed:2026-02-15, source:sess-a1b2c3}
- [P02] Prefer reading existing code before suggesting changes {score:7, uses:4, reviewed:2026-02-14, source:sess-d4e5f6}
- [P03] Ask for clarification when requirements are ambiguous {score:5, uses:3, reviewed:2026-02-13, source:sess-g7h8i9}
```

#### PolicyDelta (Reflector output)

```python
@dataclass
class PolicyDelta:
    """Structured output from the ACE Reflector."""
    additions: list[str]          # New lessons to add (start at score 5)
    updates: list[BulletUpdate]   # Score adjustments for existing bullets
    rewrites: list[BulletRewrite] # Text rewrites for existing bullets
    session_id: str               # Session that triggered this evaluation

@dataclass
class BulletUpdate:
    bullet_id: str
    score_delta: int  # +1 for positive, -2 for negative

@dataclass
class BulletRewrite:
    bullet_id: str
    new_text: str
    score_delta: int = 0  # Can also adjust score during rewrite
```

#### Reflection Prompt (Template)

```
You are evaluating an AI agent's recent behavior. Review the conversation below and identify:

1. What the agent did well (generates "helpful" increments or new lessons)
2. What the agent did poorly (generates "harmful" increments)
3. Any new generalizable lessons (generates new policy bullets)

Current policy bullets:
{current_policy}

Recent conversation (last {N} messages):
{messages}

Respond with a JSON delta:
{
  "additions": ["new lesson text", ...],
  "updates": [{"bullet_id": "P01", "score_delta": 1}, ...],
  "rewrites": [{"bullet_id": "P02", "new_text": "improved text", "score_delta": 0}, ...]
}

Score guidance:
- Bullet helped achieve the goal → score_delta: +1
- Bullet was irrelevant → score_delta: 0
- Bullet led to a mistake or wasted effort → score_delta: -2

Only include actionable, generalizable lessons. Skip trivial observations.
Return empty arrays if nothing noteworthy.
```

---

## 4. Integration Patterns

### 4.1 ArcRun Integration (messages parameter)

```python
# In arcrun/loop.py — MODIFY
async def run(
    model: Any,
    tools: list[Any],
    system_prompt: str,
    task: str,
    *,
    messages: list[dict[str, Any]] | None = None,  # NEW parameter
    on_event: Callable | None = None,
    transform_context: Callable | None = None,
) -> LoopResult:
    """If messages provided, prepend system + user to existing list.
    System prompt is always rebuilt fresh (not carried from old messages).
    """
```

### 4.2 Module Bus Event Flow

```
chat("my name is Josh")
  │
  ├── agent:assemble_prompt ──────────────── Memory: inject today/yesterday notes
  │
  ├── arcrun.run() begins
  │     │
  │     ├── agent:pre_tool(write, {path: "context.md"})
  │     │     └── Memory: ContextGuard.enforce_budget()
  │     │
  │     ├── agent:pre_tool(write, {path: "notes/2026-02-15.md"})
  │     │     └── Memory: NoteManager.enforce_append_only() [veto if overwrite]
  │     │
  │     ├── agent:pre_tool(write, {path: "identity.md"})
  │     │     └── Memory: IdentityAuditor.capture_before()
  │     │
  │     ├── agent:post_tool(write, {path: "identity.md"})
  │     │     └── Memory: IdentityAuditor.capture_after() [audit + JSONL]
  │     │
  │     └── agent:post_respond
  │           ├── Memory: EntityExtractor.extract() [async, fire-and-forget]
  │           └── Memory: PolicyEngine.evaluate() [async, every N turns]
  │
  └── Response returned to user
```

### 4.3 Eval Model Integration

```python
import arcllm

# Load eval model (separate from agent's primary model)
eval_model = arcllm.load_model(config.eval.model or config.llm.model)

# Entity extraction
result = await arcllm.invoke(
    eval_model,
    messages=[{"role": "user", "content": extraction_prompt}],
    max_tokens=config.eval.max_tokens,
    temperature=config.eval.temperature,
    response_format={"type": "json_object"},  # Structured output
)

# Policy evaluation (same eval model, different prompt)
result = await arcllm.invoke(
    eval_model,
    messages=[{"role": "user", "content": reflection_prompt}],
    max_tokens=config.eval.max_tokens,
    temperature=config.eval.temperature,
    response_format={"type": "json_object"},
)
```

---

## 5. Error Handling

### 5.1 New Error Types

```python
class MemoryError(ArcAgentError):
    """Base for memory module errors."""
    code = "MEMORY_*"
    component = "memory"

class SessionError(ArcAgentError):
    """Session creation, load, or compaction failure."""
    code = "SESSION_*"
    component = "session_manager"

class EntityExtractionError(MemoryError):
    """Entity extraction or index update failure."""
    code = "MEMORY_EXTRACTION"

class SearchError(MemoryError):
    """Search index or query failure."""
    code = "MEMORY_SEARCH"

class PolicyEvalError(MemoryError):
    """Policy evaluation or merge failure."""
    code = "MEMORY_POLICY_EVAL"
```

### 5.2 Error Philosophy

- **Background task failures** (entity extraction, policy eval) → logged, never crash agent
- **Hook failures** (notes enforcement, context guard) → audit event, handler exception caught by bus
- **Session failures** (JSONL corruption) → skip malformed lines, warn, continue
- **Search failures** (sqlite-vec missing) → fallback to BM25-only, warn
- **Eval model unavailable** → skip evaluation (default fallback_behavior)

---

## 6. Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Identity injection | Audit trail captures triggering context, admin can revert |
| Notes tampering via bash | Pre-tool hook parses bash commands for file targets |
| Context.md overflow | Hard budget enforcement with auto-truncation |
| Symlink bypass | Path.resolve() + verify under workspace |
| JSONL tampering | Append-only files, telemetry audit trail as backup |
| Eval model prompt injection | Eval prompts constructed server-side, not from user input |
| Entity data poisoning | Facts are append-only with status tracking, not overwritten |
| NIST AU-9 compliance | Dual audit: telemetry events + JSONL files |

---

## 7. File Map

```
arcagent/
├── arcagent/
│   ├── core/
│   │   ├── config.py            # MODIFY: Add EvalConfig, MemoryConfig, SessionConfig
│   │   ├── session_manager.py   # NEW: Session lifecycle, JSONL persistence, compaction
│   │   ├── context_manager.py   # MODIFY: Async assemble_prompt, event emission
│   │   ├── agent.py             # MODIFY: chat() method, SessionManager integration
│   │   └── errors.py            # MODIFY: Add MemoryError, SessionError
│   │
│   └── modules/
│       └── memory/
│           ├── __init__.py          # NEW: Module entry point
│           ├── markdown_memory.py   # NEW: Main module (hook routing, event handling)
│           ├── entity_extractor.py  # NEW: Async LLM entity extraction
│           ├── hybrid_search.py     # NEW: BM25 + sqlite-vec search
│           ├── policy_engine.py     # NEW: ACE Reflector + Curator
│           └── MODULE.yaml          # NEW: Module manifest
│
├── tests/
│   ├── unit/
│   │   ├── core/
│   │   │   ├── test_session_manager.py    # NEW
│   │   │   ├── test_config.py             # MODIFY: test new config sections
│   │   │   ├── test_context_manager.py    # MODIFY: test assemble_prompt event
│   │   │   └── test_agent.py              # MODIFY: test chat() method
│   │   └── modules/
│   │       └── memory/
│   │           ├── test_markdown_memory.py   # NEW
│   │           ├── test_entity_extractor.py  # NEW
│   │           ├── test_hybrid_search.py     # NEW
│   │           └── test_policy_engine.py     # NEW
│   ├── integration/
│   │   ├── test_memory_module.py       # NEW: End-to-end memory tests
│   │   ├── test_session_lifecycle.py   # NEW: Session create/resume/compact
│   │   └── test_policy_learning.py     # NEW: ACE evaluation cycle
│   └── cassettes/                      # NEW: VCR.py recorded LLM responses
│       └── memory/
│
└── workspace/                          # Runtime (not in repo)
    ├── identity.md
    ├── policy.md
    ├── context.md
    ├── notes/
    ├── entities/
    ├── sessions/
    ├── audit/
    └── search.db
```
