# Decisions Log

Centralized log of all design decisions across all features and phases.

---

## Phase 1b: Agent Runtime — Build Decisions (2026-02-15)

**Phase**: build | **Status**: complete | **Total decisions**: 20
**Informed by**: pi-coding-agent comparison research, v3 design doc, S002 spec

### Summary

Phase 1b adds self-extensibility, skill discovery, session persistence, compaction, additional tools, and settings management to the arcagent core. Extensions use a factory function pattern loaded via importlib from known directories. Skills use YAML-frontmatter SKILL.md files with progressive disclosure (name + description in prompt, full content loaded on demand via read tool). Sessions persist as typed JSONL with DID-signed tool calls. Compaction follows Letta-style sliding window triggered by ContextManager, executed by SessionManager. All new components are core (not modules). arccli wires through ArcAgent for full lifecycle management.

### Architecture

#### B1b-001: Extension System Location
**Decision**: Core component at `arcagent/core/extensions.py` (~300 LOC).
**Alternatives**: Module Bus module (more indirection); separate package (more files for ~300 LOC).
**Rationale**: Extensions need direct access to ToolRegistry, ModuleBus, and ContextManager. Core avoids indirection.

#### B1b-002: Extension Loading Mechanism
**Decision**: `importlib.import_module()` for local files + `importlib.metadata.entry_points(group="arcagent.extensions")` for installed packages. Same factory interface either way.
**Alternatives**: Pluggy framework (dependency, overkill); entry_points only (can't load workspace files).
**Rationale**: Single API surface. Local dev and marketplace packages work identically. Entry points are stdlib.

#### B1b-003: Extension API Scope (Phase 1b)
**Decision**: `register_tool()` + `on(event, handler)` + workspace path access. Commands, providers, active tool changes deferred to Phase 2+.
**Alternatives**: Full pi-coding-agent parity (too much API surface to stabilize); tools only (no lifecycle awareness).
**Rationale**: Minimum viable self-extensibility. LLM writes extension, registers tools, hooks events. Expand API later.

#### B1b-004: Skill Registry Location
**Decision**: Core component at `arcagent/core/skill_registry.py` (~150 LOC).
**Alternatives**: Module Bus module (indirection for a simple scan-and-inject).
**Rationale**: Direct integration with ContextManager for prompt injection.

#### B1b-005: Session Manager Location
**Decision**: Core component at `arcagent/core/session_manager.py` (~300 LOC), per S002 spec.
**Alternatives**: Module Bus module (compaction needs deep message stream integration, harder from a module).
**Rationale**: Sessions are fundamental. Compaction and resume need tight integration.

#### B1b-006: Settings Manager Pattern
**Decision**: Core component with overlay pattern. Config stays frozen (immutable). SettingsManager wraps it with a mutable overlay dict. Get checks overlay first, falls back to config. Set writes to overlay and persists a `[settings]` section in arcagent.toml.
**Alternatives**: Mutable config section (breaks Pydantic frozen models); separate settings.json (multiple config locations).
**Rationale**: Never mutates validated config. Single TOML file. Overlay pattern is simple and testable.

#### B1b-007: Core LOC Budget
**Decision**: Keep 3K budget. Tools (grep/find/ls) are in `arcagent/tools/` not `arcagent/core/`. Actual core addition ~850 LOC. Projected total ~2,359.
**Alternatives**: Raise to 4K (unnecessary with correct counting).
**Rationale**: Discipline. Tools aren't core. Comfortable headroom.

### Data Model

#### B1b-008: Session JSONL Entry Types
**Decision**: Pi-coding-agent parity types PLUS security metadata. Types: session_header, message, tool_call (DID-signed), tool_result, compaction_summary, model_change, settings_change, custom (extension-injected), audit (security events).
**Alternatives**: S002 types only (fewer types); minimal raw messages (loses metadata).
**Rationale**: Federal audit requirements + future branching support + extension extensibility. DID signing enables tamper-evident transcripts.

#### B1b-009: Extension File Format
**Decision**: Factory function pattern. Extension file exports a function receiving `ExtensionAPI` object.
```python
def extension(api):
    api.register_tool(Tool(...))
    api.on("pre_tool", my_handler)
```
**Alternatives**: Protocol class (more boilerplate); factory returns Protocol (both, more complex).
**Rationale**: Simplest possible. LLM can write this easily. Same pattern as pi-coding-agent.

#### B1b-010: Skill Frontmatter Schema
**Decision**: Full metadata — name (required), description (required), version, author, requires (tools, extensions, mcps), tags, category. Only name + description go in system prompt.
**Alternatives**: Minimal name + description (no dependency validation); v3 spec only (name + description + requires).
**Rationale**: Design for marketplace from day one. Optional fields cost nothing if unused.

### Integration

#### B1b-011: Session Persistence Integration
**Decision**: Event-driven via Module Bus. SessionManager subscribes to bus events (post_tool, post_respond) and persists automatically. Agent is unaware of sessions.
**Alternatives**: Explicit calls in ArcAgent.run() (couples agent to sessions); transform_context hook (conflates concerns).
**Rationale**: Same pattern as pi-coding-agent. Event-driven architecture. Clean separation.

#### B1b-012: Compaction Trigger
**Decision**: ContextManager triggers (emits `agent:compact_needed`), SessionManager executes (Letta-style sliding window + pre-compaction flush to context.md).
**Alternatives**: SessionManager monitors independently (duplicates token tracking); ArcAgent orchestrates (adds logic to orchestrator).
**Rationale**: Each component does what it knows best. Bus event connects them.

#### B1b-013: Hot Reload Mechanism
**Decision**: Full re-discovery on `/reload`. Clear extension tools → re-scan directories → `importlib.invalidate_caches()` → re-import all files → re-run factories. Clean slate.
**Alternatives**: Incremental diff-based (file tracking complexity); file watcher (dependency, threading issues with async).
**Rationale**: Simple, stateless, no edge cases. Scan is fast (few files).

#### B1b-014: arccli Integration
**Decision**: ArcAgent owns full lifecycle. arccli creates `ArcAgent(config)`, calls `startup()`, `run(task)`, `shutdown()`. ArcAgent internally handles model loading, tool assembly, prompt building, sessions, extensions, skills.
**Alternatives**: CLI owns model (breaks encapsulation); hybrid with overrides (more complex API).
**Rationale**: Clean encapsulation. CLI is thin. Same ArcAgent works from CLI, API, or programmatically.

### Security

#### B1b-015: Extension Sandboxing
**Decision**: Configurable per-extension via arcagent.toml. Three modes: `workspace` (default, workspace paths only), `paths` (workspace + configured additional paths), `strict` (no filesystem/network/subprocess, API only). Audit is mandatory in all modes.
**Alternatives**: Full trust (not federal-appropriate); strict only (too restrictive for development).
**Rationale**: Federal deployments need strict. Developer mode needs flexibility. Config-driven.

### Performance

#### B1b-016: Skill Discovery Caching
**Decision**: Cache until `/reload`. Scan once at startup. Agent-created skills trigger targeted re-scan of `_agent-created/` only.
**Alternatives**: Scan every turn (unnecessary overhead); file watcher (dependency, threading).
**Rationale**: Skills rarely change. `/reload` is explicit refresh.

#### B1b-017: grep/find/ls Scope
**Decision**: Workspace-scoped by default, expandable via tool policy config. `[tools.policy]` in TOML controls allowed paths.
**Alternatives**: Always workspace-scoped (can't explore project dirs); unrestricted (insecure).
**Rationale**: Secure by default, configurable for flexibility. Same policy pattern as allow/deny.

### Testing

#### B1b-018: Testing Strategy
**Decision**: Same rigor as Phase 1a. TDD, 90%+ coverage on new code, unit + integration tests. Mock arcrun/arcllm at integration boundary.
**Alternatives**: Unit only (less confidence); integration-first (harder to debug).
**Rationale**: Phase 1a quality bar is the standard.

### Deployment

#### B1b-019: Feature Build Order
**Decision**: Tools → Skills → Extensions → Sessions/Compaction → Settings → CLI wiring. Each step is independently testable.
**Alternatives**: Extensions first (self-extension ASAP but sessions delayed); sessions first (persistence ASAP but extensibility delayed).
**Rationale**: Simplest first. Each step builds on the last. CLI wires everything at the end.

#### B1b-020: Spec Strategy
**Decision**: Single spec S003 covering all Phase 1b features (except sessions/compaction in S002). One document for the full picture.
**Alternatives**: Feature-per-spec (6 specs, harder to see integration points); skip specs for simple features (less documentation).
**Rationale**: Phase 1b features are interconnected. One spec shows all integration points.

---

## Memory Module — Deepening Summary (2026-02-15)

**Deepened on:** 2026-02-15
**Sections enhanced:** 9 (Architecture, Data Model, Integration, Security, Performance, Testing, Open Questions)
**Solutions referenced:** 0 (archive empty)
**Skills matched:** architecture-pattern-enforcer, testing-unit-test-writer, testing-coverage-gap-finder

### Key Findings

1. **Fire-and-forget asyncio tasks silently swallow exceptions** — D-017 needs explicit error handling via `add_done_callback()` and a task reference set to prevent GC. Without this, entity extraction failures will be invisible in production.
2. **NIST 800-53 AU-2 may require immutable audit files** — D-008's telemetry-only approach for identity audit may not satisfy federal compliance. Consider adding an append-only JSONL audit file as a defense-in-depth measure alongside telemetry.
3. **Letta's compaction model resolves our open question** — Sliding window with configurable percentage (default 30% summarized, 70% recent preserved) using a cheaper model. This directly informs D-014's compaction strategy.

### New Risks Discovered

- **Hook bypass via bash tool** — Agent could use `bash` to write files directly, bypassing Module Bus hooks entirely. D-016 veto logic must also intercept bash commands that target `workspace/notes/`.
- **Index corruption on crash** — D-007's `index.json` is vulnerable to partial writes during crash. Needs atomic write pattern (write-to-temp + rename).
- **Embedding model cold start** — all-MiniLM-L6-v2 takes ~2-3 seconds to load and uses ~43MB RAM. Lazy loading is correct but first-search latency will be noticeable.

---

## Memory Module — Build Decisions (2026-02-14)

**Phase**: build | **Status**: complete | **Total decisions**: 19

### Summary

The memory module is a single Module Bus subscriber (`memory.py`) with internal helper classes, providing 3-tier persistent memory (context.md, daily notes, entities), identity self-editing with audit, policy self-learning, and hybrid search. It leverages existing read/write/edit tools via Module Bus hooks rather than creating new tools (except `memory_search`). Conversation state managed by a new SessionManager in core with append-only JSONL transcripts.

### Architecture

#### D-001: Module Internal Structure
**Decision**: Single Module Bus module (`memory.py`) delegating to internal helper classes (EntityStore, HybridSearch, NoteManager, etc.). Protocol-based for plugin override.
**Alternatives**: Single monolithic class (too large); separate Module Bus modules per concern (too much wiring).
**Rationale**: Simple but extensible. One registration point, clean internal separation. MemoryProvider protocol enables custom plugins later.

##### Research Insights

**Best Practices:**
- The existing `Module` protocol in `module_bus.py:70-78` defines `name`, `startup(bus)`, and `shutdown()`. The memory module should implement this protocol directly, registering all its event handlers in `startup()`.
- Use `@runtime_checkable` on the `MemoryProvider` protocol to enable `isinstance()` checks when loading plugins. This matches the existing pattern at `module_bus.py:69`.
- Helper classes (EntityStore, HybridSearch, NoteManager) should be injected into the main module via constructor, not created internally — enables testing and plugin override.

**Edge Cases:**
- If a helper class raises during `startup()`, the module should fail fast. The existing bus catches exceptions per-module (`module_bus.py:184-185`) but continues starting others. Memory module should be marked as critical.
- Protocol-based plugin override requires careful versioning. If `MemoryProvider` protocol changes, old plugins break silently. Add a `version` property to the protocol.

#### D-002: Hook Routing Strategy
**Decision**: Convention-based, workspace-relative path matching. Writes to `workspace/{identity,policy,context}.md` or `workspace/notes/` trigger hooks.
**Alternatives**: Regex pattern matching (unnecessary complexity); config-driven file registry (more config surface).
**Rationale**: Convention over configuration. Matches existing workspace structure. Zero config needed.

##### Research Insights

**Best Practices:**
- The existing `agent:pre_tool` event in `tool_registry.py:244-248` passes `{"tool": tool.name, "args": args}`. The memory hook needs the `path` argument from read/write/edit tool args to do path matching.
- Use `Path.resolve()` to canonicalize paths before matching. Prevents bypasses via `../`, symlinks, or relative paths.
- Priority should be low (e.g., 10-20) so memory hooks run before default handlers. The bus supports priority ordering (`module_bus.py:98`).

**Edge Cases:**
- **Bash bypass**: Agent could use `bash` tool to run `echo "..." > workspace/notes/2026-02-15.md`, completely bypassing write tool hooks. Must also intercept `agent:pre_tool` for `bash` and check if the command targets memory paths.
- **Symlink attacks**: If `workspace/notes/` contains a symlink pointing outside workspace, writes through the symlink bypass workspace scoping. Use `Path.resolve()` and verify the resolved path is under workspace.
- **Nested hooks**: A hook that modifies a file could trigger another hook (write to context.md during a notes hook). Guard against re-entrancy with a `_hook_active` flag.

**Performance:**
- Path matching on every tool call adds overhead. Keep it simple: `str.startswith()` on resolved paths, no regex. The bus already runs same-priority handlers concurrently (`module_bus.py:144-148`).

#### D-003: Context.md Token Overflow
**Decision**: Auto-truncate oldest entries and summarize to fit within ~2K token budget.
**Alternatives**: Veto the write (frustrating for agent); warn but allow (soft limit defeats purpose).
**Rationale**: The agent shouldn't have to manually curate. Module handles it transparently.

##### Research Insights

**Best Practices (from Letta/MemGPT):**
- Letta's `ChatMemory` class uses a 2K **character** limit per section (not tokens). Character limits are simpler and more predictable than token estimates. Consider using characters instead of tokens for context.md budget.
- Letta agents self-manage memory via tool calls (`memory_insert`, `memory_replace`, `memory_rethink`, `memory_finish_edits`). Our approach (transparent truncation) is simpler but removes agent control. Consider a hybrid: agent manages content, module enforces budget.
- MemGPT's core memory has `persona` and `human` sections (each 2K chars). Our `context.md` combines both. Consider splitting into labeled sections within context.md for finer-grained truncation.

**Edge Cases:**
- Summarization requires an LLM call. If the eval model is unavailable, truncation without summarization should be the fallback — never lose new data.
- The existing `ContextManager.estimate_tokens()` at `context_manager.py:64-73` uses ~4 chars/token heuristic. Reuse this for budget enforcement consistency.
- Race condition: if two concurrent tool calls both write to context.md, the second write may see stale content. Use file locking or serialize writes.

#### D-004: Recent Notes Placement in System Prompt
**Decision**: Today + yesterday notes prepended to the context.md section.
**Alternatives**: Separate section after context (more headers); injected via transform_context (dynamic but complex).
**Rationale**: Notes are part of the "what's going on" context tier. Prepending ensures they're seen before working memory.

##### Research Insights

**Best Practices:**
- The existing `assemble_system_prompt()` at `context_manager.py:47-62` reads `_PROMPT_FILES = ["identity.md", "policy.md", "context.md"]` in order. The memory module should subscribe to a new `agent:assemble_prompt` event and inject notes *between* the file assembly steps, or prepend to the context section.
- Keep notes injection lightweight — just read and concatenate. No LLM calls during prompt assembly.

**Edge Cases:**
- Large notes (many entries in a day) could blow up the system prompt. Apply a token budget to injected notes (e.g., last 1K tokens from today + 500 from yesterday).
- If notes files don't exist yet today, gracefully return empty string — don't error.
- Timezone handling: "today" and "yesterday" depend on timezone. Use UTC or configurable timezone.

#### D-005: Policy Evaluation Model
**Decision**: Configurable cheaper model for policy self-evaluation. Defaults to fast/cheap (e.g., haiku). TOML config override.
**Alternatives**: Same model as agent (expensive); no LLM call / structured rules (less nuanced).
**Rationale**: Policy eval doesn't need the primary model's full capability. Cost savings significant over time.

##### Research Insights

**Best Practices (from Letta):**
- Letta uses the same model as the agent by default for summarization but explicitly supports a cheaper alternative (e.g., gpt-4o-mini). Same pattern applies here.
- Rate limit background LLM calls. If the agent is having a fast-paced conversation, policy evaluation every N turns could stack up. Use a semaphore to limit concurrent eval calls.

**Edge Cases:**
- Eval model unavailable (network down, API key expired, model deprecated). Must have a graceful fallback — skip evaluation rather than crash the agent.
- Temperature matters: evaluation should use low temperature (0.1-0.3) for consistency. Conversation uses higher temperature. The separate `[eval]` config (D-015) handles this.

**References:**
- [Letta context engineering docs](https://docs.letta.com/guides/agents/context-engineering/) — model configuration for summarization

#### D-006: Entity Extraction Model
**Decision**: Shares the same eval model config as policy evaluation. One `eval_model` knob.
**Alternatives**: Separate config per concern (more flexibility but more config); use primary model (expensive).
**Rationale**: Both are background evaluation tasks. One config knob keeps it simple.

##### Research Insights

**Best Practices:**
- Use structured output (JSON mode) for entity extraction to get reliable parsing. Prompt should define the exact schema: `{entities: [{name, type, aliases, facts: [{predicate, value, confidence}]}]}`.
- Chain-of-thought prompting improves entity extraction quality. Have the eval model reason about entities before outputting structured JSON.
- Keep extraction prompts focused: extract entities from the *most recent* assistant+user exchange, not the entire history. Reduces token cost and improves accuracy.

**Edge Cases:**
- Extraction from short exchanges ("ok", "thanks") produces no entities — handle gracefully, don't create empty entity records.
- Entity name normalization: "Josh", "Joshua Schultz", "Mr. Schultz" should resolve to the same entity. The alias system (D-007) handles this, but the extraction prompt should suggest canonical names.

### Data Model

#### D-007: Entity Index Contents
**Decision**: Rich index — name, type (person/org/project/concept), aliases, last_updated.
**Alternatives**: Minimal (name + path only, requires scanning dirs); skip index (filesystem scan, slow at scale).
**Rationale**: Enables efficient search without opening every entity directory. Type and aliases improve search quality.

##### Research Insights

**Best Practices:**
- Atomic writes for `index.json`: write to `index.json.tmp`, then `os.rename()` (atomic on POSIX). Prevents corruption from crashes during write.
- Index should be treated as a cache — rebuildable from `entities/*/facts.jsonl` files. Add a `rebuild_index()` method.
- For alias resolution, use case-insensitive matching and consider Levenshtein distance for fuzzy matching (threshold of 2-3 edits).

**Edge Cases:**
- **Scale**: JSON index works well under ~10,000 entities. Beyond that, consider SQLite. For agent memory, 10K entities is likely years of operation — JSON is fine.
- **Concurrent access**: Multiple async tasks writing to index simultaneously. Use `asyncio.Lock()` to serialize index writes.
- **Entity merging**: When extraction discovers "Josh" and "Joshua Schultz" are the same entity, need a merge operation that consolidates fact logs and updates aliases.
- **Contradiction detection**: When a new fact contradicts an existing one (e.g., "Josh works at Company A" vs "Josh works at Company B"), mark the old fact as superseded, not deleted. Keep the full history in JSONL.

#### D-008: Identity Audit Storage
**Decision**: Telemetry events only via existing `AgentTelemetry.audit_event()`. No separate audit file.
**Alternatives**: Append-only JSONL audit file (redundant with telemetry); markdown audit trail (human-readable but another file).
**Rationale**: Existing audit infrastructure handles this. Consistent with all other audit events in the system.

##### Research Insights

**Best Practices:**
- `audit_event()` at `telemetry.py:104-128` writes to structured log + OTel span event. The structured log goes to Python's logging system, which needs a configured handler to persist to disk.
- For NIST 800-53 AU-2 compliance, audit records must capture: event type, when, where, source, outcome, and identity. The existing `audit_event()` captures event_type and agent_did but should also include timestamp, session_id, and before/after values for identity changes.

**Edge Cases:**
- **Federal compliance risk**: NIST 800-53 AU-9 requires protection of audit information from unauthorized modification. If audit logs are only in Python logging (stdout/file), they can be modified. Consider defense-in-depth: telemetry events PLUS an append-only JSONL file at `workspace/audit/identity-changes.jsonl`.
- **Identity injection**: An adversary could craft input that convinces the agent to change its identity.md to something malicious. The audit trail must capture the *triggering conversation context* (which message caused the identity change), not just the before/after.
- **Rollback mechanism**: If an admin reviews the audit trail and wants to revert an identity change, there's no built-in rollback. The JSONL audit file approach naturally supports this (replay the log up to the desired point).

**Performance:**
- `audit_event()` is synchronous (no await). Good — audit events shouldn't be async fire-and-forget; they must be written before the operation proceeds (AU-12 compliance: audit before action).

#### D-009: Search Database Location
**Decision**: `workspace/search.db` (SQLite with sqlite-vec). Inside workspace, portable, rebuildable.
**Alternatives**: System-level `~/.arcagent/search/` (outside workspace); in-memory (slower cold start).
**Rationale**: Portable with the agent. Can be rebuilt from source markdown/JSONL files if corrupted or moved.

##### Research Insights

**Best Practices:**
- sqlite-vec stores vectors as BLOB columns in virtual tables (`vec0`). Supports float32, int8, and binary vector types. Use float32 for all-MiniLM-L6-v2's 384-dim output.
- Use `vec0` virtual table with partition keys to shard by content type (notes, entities, context). This enables filtered search by type.
- SQLite's WAL mode (`PRAGMA journal_mode=WAL`) enables concurrent reads during writes — important for lazy reindexing during search.

**Edge Cases:**
- **sqlite-vec availability**: The extension must be compiled and loadable. In air-gapped environments, it must be bundled. Add a graceful fallback to BM25-only search if sqlite-vec isn't available.
- **Database corruption**: SQLite is robust but not immune. Since the DB is rebuildable from source files, add a `rebuild_search_db()` command.
- **Git**: Don't commit `search.db` to git — it's binary and rebuildable. Add to `.gitignore`.

**References:**
- [sqlite-vec GitHub](https://github.com/asg017/sqlite-vec) — supports metadata filtering, partition keys, SIMD acceleration

#### D-010: Embedding Model
**Decision**: Local sentence-transformers (all-MiniLM-L6-v2). 384-dim, no API calls.
**Alternatives**: OpenAI text-embedding-3-small (API dependency, better quality); configurable provider (more flexibility, more complexity).
**Rationale**: No network dependency. Fits federal/air-gapped constraint. Good enough quality for file-based search.

##### Research Insights

**Best Practices:**
- all-MiniLM-L6-v2: 22MB model, ~43MB VRAM, 384-dim output. 5x faster than all-mpnet-base-v2 with ~3% lower quality (84-85% vs 87-88% on STS-B). Good tradeoff for file search.
- **Lazy loading recommended**: First-search cold start of ~2-3 seconds is acceptable. Loading at startup adds unnecessary delay when search may never be used.
- Consider ONNX Runtime for faster inference. sentence-transformers supports ONNX export. Reduces cold start and inference time.
- For quantized deployment (air-gapped/constrained), int8 quantization halves model size with minimal quality loss.

**Edge Cases:**
- **Dependency size**: sentence-transformers pulls in PyTorch (~2GB). For lightweight deployments, consider using the ONNX runtime with onnxruntime (~200MB) instead.
- **Model download**: First-ever use downloads the model from HuggingFace. In air-gapped environments, the model must be pre-bundled. Document the offline installation path.
- **Embedding versioning**: If the model is ever changed, all existing embeddings become incompatible. Index rebuild is required. Store the model name in search.db metadata.

**Resolves Open Question:**
- **Embedding model loading: lazy on first search.** Cold start is ~2-3 seconds, acceptable. Eager loading at startup wastes memory if search is never used.

### Integration

#### D-011: Context Manager Integration
**Decision**: Module Bus event hook. ContextManager emits `agent:assemble_prompt`. Memory module subscribes and injects recent notes.
**Alternatives**: Plugin registration on ContextManager (new interface); write to context.md directly (conflates notes with working memory).
**Rationale**: Uses existing Module Bus pattern. Clean separation. Any module can participate. Most consistent with architecture.

##### Research Insights

**Best Practices:**
- The current `assemble_system_prompt()` at `context_manager.py:47-62` is synchronous and doesn't emit any events. Will need modification to emit `agent:assemble_prompt` and accept injected content.
- Consider making `assemble_system_prompt()` return a mutable data structure (e.g., `PromptSections` dict) rather than a string, so the memory module can inject content at specific positions (before context, after policy, etc.).
- The Module Bus `emit()` is async (`module_bus.py:112-149`), but `assemble_system_prompt()` is sync. Either make prompt assembly async or use a different integration pattern (e.g., register a callback during module startup).

**Edge Cases:**
- If the memory module fails to inject notes (exception in handler), the agent still needs a working system prompt. The bus catches handler exceptions (`module_bus.py:166-172`), so this is safe — notes injection is best-effort.
- Multiple modules injecting into the prompt: need a clear ordering. Use priority levels (notes at priority 50, other modules at 100+).

#### D-012: ArcRun Statelessness
**Decision**: ArcRun stays stateless. All persistence, session management, and message history live in ArcAgent. `messages` parameter in arcrun.run() is just a pass-through.
**Alternatives**: ArcRun manages sessions (breaks its single-responsibility); hybrid ownership (messy boundaries).
**Rationale**: Clean boundary. ArcRun executes, ArcAgent remembers. Each project stays focused.

##### Research Insights

**Best Practices:**
- The current `_build_state()` at `loop.py:17-48` creates fresh messages each time: `messages=[system_message(system_prompt), user_message(task)]`. Adding a `messages` parameter should prepend these to the provided list, or replace the default.
- When passing messages through, `transform_context` (already in ArcRun) must handle the larger message list. The existing `ContextManager.transform_context()` at `context_manager.py:132-177` already handles arbitrary-length message lists with graduated pruning — this works.

**Edge Cases:**
- If `messages` parameter is provided but contains no system message, ArcRun should still prepend the system prompt. The system message should always be rebuilt fresh from current identity/policy/context (not carried from old messages).
- Message list could be very large after many turns. The existing `transform_context` handles this with observation masking and emergency truncation — no new code needed.

#### D-013: Conversation State Owner
**Decision**: New `SessionManager` class in core, alongside ContextManager. Clean separation of concerns.
**Alternatives**: Expand ContextManager (too many responsibilities); memory module owns sessions (keeps core too thin, sessions are fundamental).
**Rationale**: Matches Nanobot's proven pattern. SessionManager owns messages + session lifecycle. ContextManager stays focused on prompt assembly + token management.

##### Research Insights

**Best Practices:**
- SessionManager should own: session creation, message append, message list retrieval, session persistence (JSONL), and session compaction. It should NOT own: prompt assembly, token counting, or context pruning (those stay in ContextManager).
- Use `asyncio.Lock()` for thread safety on the message list. Multiple coroutines may try to append messages concurrently (entity extraction writing facts while main loop adds tool results).
- Session ID should be a UUID4, matching the existing `run_id` pattern at `loop.py:31`.

**Edge Cases:**
- **Session resume**: When loading a previous session from JSONL, validate message format. Corrupted entries should be skipped with a warning, not crash the agent.
- **Long-running sessions**: Without compaction, JSONL files grow unbounded. Compaction should be triggered proactively (see D-014 insights).

#### D-014: Session Persistence Format
**Decision**: Append-only JSONL transcripts at `workspace/sessions/{session-id}.jsonl`. Compaction adds summary entries.
**Alternatives**: Mutable JSON (simpler, no audit trail); in-memory only (no conversation resume).
**Rationale**: Immutable audit trail. Consistent with entity facts.jsonl pattern. Git-diffable. Federal-friendly.

##### Research Insights

**Best Practices (from Letta):**
- Letta's compaction uses **sliding window** mode by default: 30% oldest content summarized, 70% recent preserved. This is the right pattern for session compaction.
- Compaction uses a cheaper model (configurable). Summaries are capped at a character limit (Letta defaults to 2000 chars). Our eval model (D-005/D-015) serves this purpose.
- Compaction summary should be stored as a special JSONL entry with `type: "compaction_summary"` so replay can distinguish original messages from summaries.

**Edge Cases:**
- **Crash during append**: JSONL is resilient — partial last lines can be detected and skipped. Add a validation step on load: `try: json.loads(line)` per line, skip malformed.
- **File locking**: Use `fcntl.flock()` on Unix for advisory file locking if multiple processes could write to the same session file. For single-agent-per-workspace, this is unlikely but defensive.
- **Disk space**: Add a configurable retention policy (e.g., keep last N sessions, or sessions from last 30 days). Old session files can be archived or deleted.
- **Git-friendliness**: JSONL diffs cleanly in git (each line is independent). But large JSONL files produce large diffs. Consider rotating session files by date or message count.

**Resolves Open Question:**
- **Session compaction triggers: token-based.** Trigger compaction when the message list exceeds the context window's compact_threshold (85% of max_tokens, matching the existing `ContextManager` thresholds at `config.py`). The sliding window approach (Letta-style) preserves 70% recent, summarizes 30% oldest.

### Security

#### D-015: Eval Model Configuration
**Decision**: Separate `[eval]` config section in TOML with its own provider, model, max_tokens, temperature.
**Alternatives**: Reuse agent's LLM config (less control); eval_model string only (limited tuning).
**Rationale**: Full control over eval model behavior. Different temperature for evaluation vs conversation. Independent provider possible.

##### Research Insights

**Best Practices:**
- Add a `fallback_behavior` config option: `skip` (skip evaluation if model unavailable) vs `error` (raise). Default to `skip` — background tasks shouldn't crash the agent.
- Rate limit eval calls with a configurable `max_concurrent_evals` (default 2). Prevents background tasks from overwhelming the eval model API.
- Consider `timeout_seconds` for eval calls (default 30s). Background LLM calls shouldn't hang indefinitely.

**Edge Cases:**
- If using a different provider for eval (e.g., local ollama for eval, Anthropic for conversation), credential management gets more complex. The existing vault system handles this but needs to support multiple provider configs.
- Model deprecation: if the configured eval model is sunset, agent should log a warning and fall back to the primary model rather than silently failing.

#### D-016: Notes Append-Only Enforcement
**Decision**: Hard veto via Module Bus `agent:pre_tool` handler. Overwrites/deletes on notes/ are blocked.
**Alternatives**: Auto-transform to append (silent behavior change); soft warning (weak protection).
**Rationale**: Append-only means append-only. Agent gets a clear error. No ambiguity.

##### Research Insights

**Best Practices:**
- The veto mechanism at `module_bus.py:43-48` uses `ctx.veto(reason)` — first veto wins, all handlers still run. The veto reason is returned to the tool caller via `ToolVetoedError` at `tool_registry.py:249-252`. Good — the agent gets a clear error message explaining WHY the write was blocked.
- Use priority 10 (policy level) for the append-only handler so it runs before other handlers.

**Edge Cases:**
- **Bash bypass** (CRITICAL): The `bash` tool can `echo "..." > workspace/notes/file.md` or `rm workspace/notes/file.md`, completely bypassing the write tool's pre_tool hook. Must also intercept `agent:pre_tool` for `bash` and parse the command for file operations targeting notes paths.
- **File renames**: `mv workspace/notes/2026-02-14.md workspace/notes/old.md` is effectively a delete+create. Intercept rename operations too.
- **Legitimate corrections**: If the agent writes a typo in notes, there's no way to fix it without admin intervention. Consider allowing `edit` (append-at-position) but not `write` (overwrite) — or add an admin override mechanism.
- **Symlink bypass**: Agent could create a symlink elsewhere pointing to notes, then write through the symlink. Resolve all paths before matching.

### Performance

#### D-017: Entity Extraction Timing
**Decision**: Fully background via `asyncio.create_task()`. Fire-and-forget. Never blocks user interaction.
**Alternatives**: Background with completion gate (adds latency); batched every N turns (delayed awareness).
**Rationale**: Responsiveness is paramount. Entities may lag by one turn — acceptable tradeoff.

##### Research Insights

**Best Practices:**
- **CRITICAL**: `asyncio.create_task()` silently swallows exceptions if the task is never awaited. Must use the task reference pattern:
  ```python
  _background_tasks: set[asyncio.Task] = set()

  task = asyncio.create_task(self._extract_entities(messages))
  _background_tasks.add(task)
  task.add_done_callback(_background_tasks.discard)
  ```
  The existing `create_arcrun_bridge()` at `agent.py:106-123` already uses this exact pattern (`_pending` set + `add_done_callback`). Follow it.
- Add an error callback that logs extraction failures:
  ```python
  def _on_extraction_done(task: asyncio.Task) -> None:
      if task.exception():
          _logger.error("Entity extraction failed: %s", task.exception())
  task.add_done_callback(_on_extraction_done)
  ```

**Edge Cases:**
- **Rapid-fire messages**: If the agent responds 5 times in quick succession, 5 extraction tasks launch concurrently. Use a semaphore (max 2 concurrent extractions) to avoid overwhelming the eval model.
- **Shutdown during extraction**: If `agent.shutdown()` is called while extraction is running, the task may write to files after cleanup. Track active extraction tasks and cancel them during shutdown.
- **Memory leak**: Without the reference set pattern, the Python GC may collect the task before completion. The existing bridge pattern in `agent.py` shows the correct approach.

#### D-018: Search Index Sync Strategy
**Decision**: Lazy reindexing on search. Only rebuild when `memory_search` called and source files changed.
**Alternatives**: Incremental on write (overhead per write); periodic background (may miss recent writes).
**Rationale**: Saves work when search isn't used. Combined with async entity extraction, stale-until-searched is acceptable.

##### Research Insights

**Best Practices:**
- Use file modification timestamps (`os.path.getmtime()`) to detect changed files. Store last-indexed timestamps in the SQLite database metadata table.
- Use `PRAGMA journal_mode=WAL` for the search database to allow concurrent reads during reindex writes.
- Chunk documents at ~400 tokens with overlap (50-100 tokens). For markdown, chunk at heading boundaries when possible, falling back to token-based splitting.

**Edge Cases:**
- **First search on large corpus**: If the agent has 100+ entity files and 30+ days of notes, first reindex could take 10-30 seconds (embedding generation). Consider showing a progress indicator or chunking the reindex across multiple calls.
- **Deleted files**: Files that no longer exist should be removed from the index. Check for orphaned entries during reindex.
- **Encoding issues**: JSONL and markdown files with non-UTF-8 characters can crash the indexer. Always decode with `errors='replace'`.

**Performance:**
- BM25 scoring doesn't require a model — it's pure keyword matching. Only vector search needs embeddings. For the common case (keyword-heavy queries), BM25 alone may suffice. Consider a fast path: if BM25 returns high-confidence results (score > threshold), skip vector search entirely.

### Testing

#### D-019: LLM-Dependent Component Testing
**Decision**: Record/replay pattern. Record real LLM responses during development, replay in CI.
**Alternatives**: Mock responses (less realistic); mocks for unit + real for integration (more infrastructure).
**Rationale**: Realistic assertions without per-test API cost. One-time recording cost. Deterministic CI.

##### Research Insights

**Best Practices:**
- Use `pytest-recording` (VCR.py wrapper) with `--record-mode=once`. First run records HTTP interactions to "cassette" files, subsequent runs replay them.
- **CRITICAL**: Filter credentials from recordings: `filter_headers=['authorization', 'x-api-key']`. Without this, API keys end up in test fixtures (committed to git).
- Store cassettes in `tests/cassettes/` organized by test module. Each test gets its own cassette file.
- Use `pytest-recording`'s `none` mode by default (no network in CI). Pass `--record-mode=once` only during explicit re-recording.

**Edge Cases:**
- **Non-deterministic assertions**: LLM outputs vary even with same input. Don't assert exact output strings. Instead:
  - Assert structural properties (JSON has required keys)
  - Assert semantic properties (output contains expected entity name)
  - Use the cassette for deterministic replay, but design assertions that survive re-recording
- **Cassette staleness**: When the extraction prompt changes, cassettes must be re-recorded. Add a CI check that warns when prompt files are newer than their cassettes.
- **Recording the eval model**: Entity extraction and policy evaluation use the eval model. Record both the primary model and eval model interactions.

**References:**
- [pytest-recording on PyPI](https://pypi.org/project/pytest-recording/) — VCR.py integration for pytest
- [VCR.py docs](https://vcrpy.readthedocs.io/en/latest/usage.html) — recording modes, filtering

### Open Questions

- ~~Embedding model loading: lazy on first search or at startup?~~ **RESOLVED → Lazy loading.** Cold start ~2-3s, ~43MB RAM. Acceptable for first search. Eager loading wastes resources when search isn't used.
- ~~Session compaction triggers: token threshold vs message count vs both?~~ **RESOLVED → Token-based, sliding window.** Trigger at compact_threshold (85% of max_tokens). Letta-style: summarize oldest 30%, preserve recent 70%. Use eval model for summarization.
- Policy evaluation prompt design: exact prompt template (defer to /specify)

### Related Solutions

- (no existing solutions archive entries)

---

## Memory Wiring — Deepening Summary (2026-02-15)

**Deepened on:** 2026-02-15
**Sections enhanced:** 7 (MW-004, MW-006, MW-007, MW-008, MW-009, MW-011, MW-015)
**Research agents:** 5 parallel (ModuleContext DI, session-owns-context, module-injected guidance, FTS5 date filtering, compaction pre-flush)
**Open questions resolved:** 3/3

### Key Findings

1. **UNINDEXED columns in FTS5 solve date filtering without JOINs** — Store `created_date` as UNINDEXED in the FTS5 table. Filterable via WHERE but not tokenized for search. 10-30ms vs 50-200ms post-filter. Resolves date_from/date_to in MW-011.
2. **Hybrid fast/slow compaction pre-flush (MAGMA pattern)** — Fast path: heuristic pattern matching (<50ms, sync). Slow path: async LLM enrichment with 5s timeout. Fallback chain ensures zero information loss even if eval model is unavailable.
3. **OpenClaw race condition bug #5457** — Stale token counts during concurrent compaction. Session must lock its context manager during compaction to prevent interleaved reads/writes.
4. **Frozen ModuleContext dataclass with topological sort** — ModuleContext should be `frozen=True` to prevent modules from mutating shared state. Module load order via topological sort if inter-module dependencies exist (not needed yet, design for it).
5. **Module-injected guidance creates indirect prompt injection surface** — Adversary who can write to workspace files can inject behavioral guidance via memory module. Mitigate by validating injected content structure (markdown heading format only, no raw instructions).

### New Risks Discovered

- **Stale token counts during compaction**: OpenClaw bug #5457 — context manager token count can be stale if compaction and turn processing overlap. Use `asyncio.Lock()`.
- **Indirect prompt injection via memory guidance**: Auto-injected `## Memory` section could be weaponized if workspace files are compromised. Validate structure before injection.
- **Ephemeral session entity extraction**: Single-shot `run()` creates ephemeral sessions that never trigger post-session entity extraction. Extract on session close, not just per-turn.

### Open Questions Resolved

1. **Convention loader error handling** → Tiered: fail startup on missing `entry_point` (critical), warn and skip on malformed optional fields. See MW-007 insights.
2. **Date range filtering** → SQLite-level via FTS5 UNINDEXED columns. No JOINs, no post-filter. See MW-011 insights.
3. **Compaction pre-flush** → Hybrid fast/slow path (MAGMA pattern). Heuristics first, LLM fallback with timeout. See MW-006 insights.

---

## Memory Wiring — Build Decisions (2026-02-15)

**Phase**: build | **Status**: complete | **Total decisions**: 16
**Informed by**: Brainstorm (2026-02-14), OpenClaw/Letta/CrewAI/LangGraph/Semantic Kernel research, existing 423-test memory implementation

### Summary

Wire up ArcAgent's fully-built memory system by connecting 5 gaps: eval model instantiation, memory_search tool registration, agent awareness of memory capabilities, automatic compaction, and config enablement. Key architectural refinements: convention-based module loading with config allowlist, ModuleContext for proper dependency injection, session-owns-context hierarchy, and module-injected behavioral guidance (OpenClaw pattern). Memory is automatic (push to model), injected at session start only, and persists through compaction.

### Architecture

#### MW-001: Eval Model Instantiation
**Decision**: Lazy creation inside MarkdownMemoryModule from EvalConfig. Falls back to agent's LLM config when eval config is empty.
**Alternatives**: Agent passes its model (rate limit sharing), agent creates separate model (agent.py bloat).
**Rationale**: Memory module owns its dependency. Keeps agent.py thin. No coupling between primary and eval models.

#### MW-002: Module Loading Strategy
**Decision**: Convention-based auto-discovery with config allowlist. Modules discovered from `arcagent/modules/` by folder structure (MODULE.yaml + __init__.py). Only load if `[modules.{name}] enabled = true` in config.
**Alternatives**: Pure convention (no control), explicit registration in agent.py (bloat).
**Rationale**: Drop a folder, enable in config, done. Federal environments lock down which modules load via config.

#### MW-003: Memory Search Tool Ownership
**Decision**: Memory module registers `memory_search` tool during its own startup via ModuleContext providing ToolRegistry access.
**Rationale**: Module owns its tool. Follows from MW-002 (convention loader gives modules access to register tools).

#### MW-004: Module Startup Interface (ModuleContext)
**Decision**: Create `ModuleContext` dataclass with `bus`, `tool_registry`, `config`, `telemetry`, `workspace`, `llm_config`. Change `Module.startup(bus)` to `Module.startup(ctx: ModuleContext)`.
**Alternatives**: Event-based (stringly-typed), kwargs (brittle), service locator (hides dependencies).
**Rationale**: Proper dependency injection. Strongly typed, extensible. No service locator anti-pattern. Low blast radius (only memory module exists).

##### Research Insights

**Best Practices (DI Patterns):**
- Use `@dataclass(frozen=True)` for ModuleContext — prevents modules from mutating shared state. Modules receive a read-only view of their dependencies.
- Consider topological sort for module load order when inter-module dependencies exist. Not needed now (single module), but the loader should support a `depends_on` field in MODULE.yaml for future use.
- FastAPI and Django both use similar context injection patterns. FastAPI's `Depends()` is the closest analog — resolved at startup, injected per-request. ModuleContext is resolved once at startup, injected per-module.

**Edge Cases:**
- If ModuleContext grows beyond 7-8 fields, it becomes a code smell. Split into sub-contexts (e.g., `StorageContext`, `RuntimeContext`) if needed.
- Frozen dataclass prevents `tool_registry` mutation by the module — but the module needs to *call* `tool_registry.register()`. Frozen means the *reference* is immutable, not the object. This works correctly.

#### MW-005: Memory Injection Scope
**Decision**: System prompt only — identity.md, policy.md, context.md, today/yesterday notes. Already built. No extra search at session start.
**Alternatives**: Proactive entity injection (latency, complexity), full memory search (token cost).
**Rationale**: Simple. Agent uses `memory_search` tool when it needs more. Push intelligence to the model.

#### MW-006: Compaction Trigger
**Decision**: Session handles compaction internally. After each turn, checks context ratio. If above `compact_threshold` (0.85), compacts its own messages with pre-flush to context.md. Emergency truncation (0.95) remains the within-loop safety net.
**Alternatives**: Agent.py orchestrates (coordination headaches), context manager triggers (nests LLM calls inside callbacks).
**Rationale**: Session owns both messages and context (MW-008). Natural self-management. No LLM calls nested inside LLM callbacks.

##### Research Insights

**Compaction Pre-Flush (MAGMA Pattern — Resolves Open Question #3):**
- **Hybrid fast/slow path**: Fast path uses heuristic pattern matching (<50ms, synchronous) to extract key facts, decisions, and action items from messages about to be compacted. Slow path uses async LLM enrichment for richer extraction.
- **Fallback chain**: Heuristics → small LLM with 5s timeout → heuristics-only → emergency preserve-all. Zero information loss guaranteed even if eval model is unavailable.
- **Fast path heuristics**: Regex patterns for decisions (`decided`, `agreed`, `chose`), facts (`is`, `has`, `was`), action items (`TODO`, `need to`, `should`). Extract to context.md before compaction summarizes/discards the originals.
- **Slow path**: Fire async LLM extraction task. If it completes within 5s, merge results with heuristic extraction. If timeout, heuristic results are sufficient.

**Race Condition (OpenClaw Bug #5457):**
- Stale token counts during concurrent compaction. If a new message arrives while compaction is in progress, the token count used to decide compaction ratio is stale. Session must acquire `asyncio.Lock()` before checking token ratio AND hold it through compaction completion.
- OpenClaw fix: checkpoint-based recovery. Before compaction starts, write a checkpoint (pre-compaction message list + token count). If crash during compaction, restore from checkpoint on next startup.

**Ephemeral Sessions:**
- Single-shot `run()` creates ephemeral sessions that never trigger per-turn entity extraction. AWS Bedrock pattern: extract entities at session close, not just per-turn. Add a `close()` method to session that triggers final extraction.

#### MW-007: Convention-Based Module Loader
**Decision**: New `module_loader.py` in `core/`. Scans `arcagent/modules/*/MODULE.yaml`, reads entry_point, checks config allowlist, imports and instantiates with ModuleContext. Agent.py calls `loader.discover()` instead of `_register_modules()`.
**Rationale**: Zero agent.py changes for new modules. Convention with explicit control.

##### Research Insights

**Error Handling (Resolves Open Question #1):**
- **Tiered approach**: Critical fields (`entry_point`, `name`) missing → fail startup with clear error. Optional fields (`version`, `description`, `depends_on`) malformed → warn and use defaults.
- Rationale: A module without an entry_point can never load. Failing fast surfaces the problem immediately. But a missing `version` field shouldn't prevent a perfectly functional module from loading.
- Log warnings at `WARNING` level (not `ERROR`) for optional field issues — visible but not alarming.
- Validate MODULE.yaml against a Pydantic schema (`ModuleManifest` model) for consistent, typed validation.

#### MW-008: Session Owns Context
**Decision**: SessionManager holds a ContextManager instance. Session delegates context operations (prompt assembly, pruning, token mgmt) to its context manager. Single-shot `run()` creates an ephemeral session with context.
**Alternatives**: Full merge into SessionManager (too many responsibilities), fully independent siblings (coordination headaches).
**Rationale**: Context is a capability of a session, not a sibling. Enables MW-006 — session can self-manage compaction.

##### Research Insights

**Validated Patterns:**
- OpenClaw, OpenAI SDK, and Factory all use session-owns-context. OpenClaw's `ConversationSession` holds a `ContextWindow` directly. OpenAI's Agents SDK has `Runner` owning both message history and context assembly.
- The existing `ContextManager` at `context_manager.py` is stateless (reads workspace files, assembles prompt, returns string). This makes it safe to embed in SessionManager — no shared mutable state.

**OpenClaw Bug #5457 (Race Condition):**
- When SessionManager and ContextManager are separate, concurrent access to token counts causes stale reads. With session-owns-context, the session serializes access to its own context manager via `asyncio.Lock()`.
- Fix pattern: `async with self._context_lock: ratio = self._context.token_ratio(); if ratio > threshold: await self._compact()`

**Ephemeral Sessions for run():**
- Single-shot `run(task)` should create an ephemeral `Session` with its own `ContextManager`. On completion, extract entities from the full conversation, then discard the session.
- This matches AWS Bedrock's pattern: "batch mode" sessions are disposable but still capture learnings.

#### MW-009: Memory Capability Guidance
**Decision**: Module injects default behavioral guidance via `assemble_prompt` hook. Developer can override in identity.md with a `## Memory` section that takes precedence. If module is disabled, no stale instructions appear.
**Alternatives**: Static in identity.md only (stale if module changes), module-only injection (no developer control).
**Rationale**: OpenClaw pattern — module is self-describing. Developer retains override control.

##### Research Insights

**Override Detection:**
- Check for `## Memory` heading in identity.md using simple string check: `"## Memory" in identity_content`. If present, skip module injection. Regex not needed — markdown heading format is predictable.
- AWS Bedrock AgentCore uses similar pattern: system prompt has "slots" that modules fill, but operator-provided instructions take priority over module defaults.

**Security (Indirect Prompt Injection Risk):**
- Auto-injected memory guidance creates an attack surface: if an adversary can write to workspace files (e.g., via a compromised tool or shared workspace), they can inject behavioral instructions that appear as legitimate memory guidance.
- Mitigation: validate that injected content matches expected structure (markdown with specific headings, no raw instructions or code blocks). The guidance should describe *capabilities*, not give *commands*.
- Defense-in-depth: the existing policy.md veto system (D-016 from memory build) provides a second layer — even if guidance is injected, policy violations are still caught.

#### MW-010: Memory Enabled by Default
**Decision**: Memory module enabled by default. New agents get memory automatically. Federal environments disable via `[modules.memory] enabled = false`.
**Alternatives**: Disabled by default (every developer must know to enable), auto-detect from workspace files (chicken-and-egg).
**Rationale**: Memory is core to useful agents. Matches "push to model" philosophy.

### API Design

#### MW-011: memory_search Tool Interface
**Decision**: `memory_search(query: str, scope: str | None = None, date_from: str | None = None, date_to: str | None = None)`. All parameters beyond query are optional. Scope filters by memory tier ("notes", "entities", "context"). Date range filters by time. top_k stays internal (default 10).
**Alternatives**: Simple query-only (too limited), multiple tools per scope (model picks wrong one).
**Rationale**: Give the model control. It can be precise or broad. Date range needs implementation in HybridSearch.

##### Research Insights

**Date Filtering via FTS5 UNINDEXED (Resolves Open Question #2):**
- **SQLite-level filtering, not post-filter.** Store `created_date TEXT` as an `UNINDEXED` column in the FTS5 virtual table. UNINDEXED columns live in the FTS5 table but are NOT tokenized for full-text search — they're metadata that can be filtered via WHERE clauses.
- Schema: `CREATE VIRTUAL TABLE memory_fts USING fts5(content, scope, created_date UNINDEXED, source_path UNINDEXED)`
- Query: `SELECT * FROM memory_fts WHERE memory_fts MATCH ? AND created_date BETWEEN ? AND ?`
- **Performance**: 10-30ms with UNINDEXED columns vs 50-200ms for post-filtering (no JOINs, no separate metadata table).
- **NULL dates**: System files (identity.md, policy.md, context.md) have no creation date. Use NULL for `created_date` and include them with: `AND (created_date IS NULL OR created_date BETWEEN ? AND ?)`. System files always appear in results regardless of date filter.
- **Date format**: Store as ISO 8601 (`YYYY-MM-DD`). SQLite string comparison works correctly for ISO date ranges.

### Integration

#### MW-012: Eval Model Fallback
**Decision**: ModuleContext passes agent's LLMConfig. If EvalConfig.provider is empty, memory module falls back to agent's model string via `_load_model()`. Single fallback chain.
**Alternatives**: Pass agent's model instance (shares rate limits), require explicit config (broken by default).
**Rationale**: Memory works out of the box. Developers override with cheaper model via `[eval]` config.

### Security

#### MW-013: Memory Search Access Control
**Decision**: No per-result filtering. Workspace scoping is sufficient. Each agent has its own workspace, files, SQLite index. Cross-agent access prevented at workspace level.
**Alternatives**: Classification-aware filtering (metadata overhead), tool policy enforcement (too coarse).
**Rationale**: Simple. Workspace IS the security boundary.

#### MW-014: PII Handling
**Decision**: PII handled at ArcLLM layer. ArcLLM filters PII before model and on responses. Memory stores what agent learns — it's the agent's personal knowledge base. No additional PII filtering in memory module.
**Alternatives**: Regex PII filter in memory module (redundant), full PII detection library (dependency).
**Rationale**: Separation of concerns. ArcLLM owns the security boundary.

### Performance

#### MW-015: Background Task Concurrency
**Decision**: Use `asyncio.Semaphore(eval_config.max_concurrent)` to limit concurrent eval model calls. Config exists (`max_concurrent=2`), just enforce in `_spawn_background`.
**Alternatives**: No limit (overwhelm provider at scale), queue with worker (complexity).
**Rationale**: Prevents overwhelming eval model provider. Simple semaphore implementation.

##### Research Insights

**Task Reference Pattern:**
- Must use the task reference set pattern (already in `agent.py:106-123`) to prevent GC from collecting background tasks and to surface exceptions via `add_done_callback`.
- Semaphore should wrap the entire background task lifecycle, not just the LLM call. This prevents resource leaks if the task fails between acquiring the semaphore and making the LLM call.
- Graceful shutdown: track all background tasks in a set. On `shutdown()`, cancel pending tasks and await running ones with a timeout (5s). Log any tasks that don't complete.

### Testing

#### MW-016: Test Strategy
**Decision**: Unit tests for all new code (ModuleContext, module loader, eval model lazy init, compaction trigger, memory_search tool registration). One integration test: agent startup → chat → verify memory files created + search returns results. Follows 70/20/10 split.
**Alternatives**: Extend existing tests only (may miss integration issues), integration-heavy (slower, harder to debug).
**Rationale**: Existing 423 tests cover memory module internals. New tests cover the wiring.

### Open Questions

- ~~Convention loader error handling: if MODULE.yaml is malformed, skip with warning or fail startup?~~ **RESOLVED → Tiered.** Fail on critical fields (`entry_point`, `name`). Warn and skip on optional fields. See MW-007 insights.
- ~~Date range filtering implementation in HybridSearch: filter at SQLite level or post-filter?~~ **RESOLVED → SQLite-level via FTS5 UNINDEXED columns.** No JOINs, 10-30ms. See MW-011 insights.
- ~~Compaction pre-flush: should it use eval model or simple heuristic?~~ **RESOLVED → Hybrid fast/slow path (MAGMA pattern).** Heuristics first (<50ms), LLM fallback with 5s timeout. See MW-006 insights.
