---
topic: Phase 1 Technical Approach
date: 2026-02-14
status: deepened
chosen_approach: Interface bridge with existing foundations
deepened: 2026-02-14
research_agents: 4/4 completed
---

## Research Enhancement Summary

Document enriched with parallel research from 4 agents covering: async event bus patterns, token counting approaches, MCP Python SDK integration, and TOML+Pydantic config patterns. Key findings reinforce all 11 decisions with production patterns, edge cases, and implementation blueprints.

# Phase 1 Technical Approach

## Problem

Phase 1 defines 10 features for ArcAgent's core. The technical approach for how these components integrate with the existing ArcLLM and ArcRun foundations needed to be hashed out before design/implementation.

## Who

BlackArc engineering team building ArcAgent core.

## Success Criteria

- All 7 core components implemented with clear integration boundaries
- ArcRun is the single execution loop — ArcAgent orchestrates, not duplicates
- Module Bus bridges ArcRun events without coupling
- Config, telemetry, and patterns consistent with ArcLLM/ArcRun siblings
- Core stays under 3,000 LOC

## Existing Foundations

### ArcLLM (exists at ~/AI/arcllm/)

- `LLMProvider` ABC: `invoke(messages, tools) -> LLMResponse`
- 12 provider adapters (Anthropic, OpenAI, Ollama, etc.)
- Normalized types: Message, Tool, ToolCall, LLMResponse, Usage, ContentBlock
- Modules: fallback, retry, rate limiting, audit, security, OTel telemetry
- Config: TOML + Pydantic validation
- OTel: Full SDK setup with gRPC/HTTP exporters, mTLS support, GenAI semantic conventions

### ArcRun (exists at ~/AI/arcrun/)

- `run(model, tools, system_prompt, task) -> LoopResult`
- `RunHandle` for async with steer/follow_up/cancel
- `EventBus`: synchronous, single callback, emit-only
- `ToolRegistry`: internal lookup map (receives tools, doesn't own them)
- `Sandbox`: permission gating via check callback
- Strategies: ReAct (multi-step tool calling), Code
- `transform_context` callback for modifying messages pre-LLM call

## Key Decisions

### 1. Module Bus <-> ArcRun EventBus: Bridge Pattern

ArcAgent's Module Bus is a separate, richer async event system. It subscribes to ArcRun's EventBus via the `on_event` callback, translates ArcRun events (`tool.start`, `turn.start`, `llm.call`, etc.) into Module Bus events (`agent:pre_tool`, `agent:post_tool`, etc.), and adds its own lifecycle events.

```
ArcRun EventBus ---(on_event callback)---> ArcAgent Module Bus ---> modules
                                            |
                                            +-- also emits: agent:init, agent:pre_plan,
                                                agent:compact, agent:shutdown, etc.
```

**Rationale**: Clean separation. ArcRun stays generic. ArcAgent's Module Bus is fully async with priorities and interception.

#### Research Insights: Module Bus

**Roll your own (~350 LOC).** Reviewed 4 existing libraries (bubus, aiopubsub, blinker-async, pymitter) — none support priority ordering or veto/interception patterns. All lack the error isolation required for federal deployment. Building custom gives full control of the security audit surface.

**Dispatch pattern:** Use `asyncio.gather(return_exceptions=True)` for concurrent execution within the same priority level, sequential across priority levels. This ensures policy handlers (priority 10) complete before default handlers (priority 100). Use `return_exceptions=True` — never `TaskGroup` — because handlers are independent and one failure shouldn't cancel others.

**Error isolation:** Wrap each handler with `asyncio.wait_for(handler, timeout=30.0)` to prevent runaway handlers. Always re-raise `CancelledError`. Log at handler level AND at gather level. Consider `ExceptionGroup` (Python 3.11+) for strict mode reporting.

**Module lifecycle:** Use async context managers (`async with`) for paired startup/shutdown. Shutdown in reverse order of startup. Use `asyncio.Event()` for graceful shutdown signaling. Pattern validated by FastAPI lifespan and HTTPX event hooks.

**Context passing:** Use `contextvars.ContextVar` for request-scoped data (trace IDs, correlation IDs) that flows through handlers without explicit parameter passing. Create at module level, always reset with token in `finally` block.

**Implementation blueprint:**
- `EventContext` (dataclass with veto): ~30 LOC
- `Handler` (priority wrapper): ~20 LOC
- `EventBus` (core dispatch): ~200 LOC
- `Module` (base lifecycle): ~50 LOC
- `Application` (lifecycle coordinator): ~50 LOC

### 2. Module Bus Handler Model: Async-Only

All module handlers are `async def`. Simple contract. No sync/async detection magic.

### 3. Event Interception: EventContext.veto(reason)

Pre-* event handlers receive an `EventContext` with a `.veto(reason)` method. All handlers still run even after a veto (they can inspect veto state). Module Bus checks veto state after all handlers complete.

**Rationale**: Auditable (captures WHY). Explicit (no exception-as-control-flow). All handlers see state (no short-circuit).

#### Research Insights: Veto Pattern

Pattern validated against Chain of Responsibility (GoF) and FastAPI middleware. Key edge cases:

- **Multiple vetos:** Keep first veto reason (first policy module to reject wins). All handlers still see `is_vetoed()` state and can log or add context.
- **Post-event handlers:** Should NOT be able to veto — veto only makes sense for `pre_*` events where the action hasn't happened yet.
- **State mutation guard:** Ensure vetoed events don't partially mutate shared state. EventContext should carry immutable data; mutations happen only after veto check passes.

### 4. Handler Ordering: Integer Priority

Modules subscribe with integer priority. Lower runs first.

| Priority | Use Case |
|----------|----------|
| 10 | Policy checks (run first to gate) |
| 50 | Security modules |
| 100 | Default (most modules) |
| 200 | Logging/telemetry (run after) |

### 5. Token Counting: Hybrid

Client-side estimation (approximate) for proactive pruning decisions. Provider-reported (from `LLMResponse.usage`) for actual tracking and cost accounting. Conservative 1.1x multiplier on estimates.

#### Research Insights: Token Counting & Context Management

**Client-side estimation options:**
- **Anthropic `messages.count_tokens()` API** — free, 100% accurate, but requires API call. Best for ground truth before critical decisions.
- **Xenova/claude-tokenizer** (Hugging Face) — offline, no API calls, high accuracy. Load once at startup via `GPT2TokenizerFast.from_pretrained('Xenova/claude-tokenizer')`.
- **tokencost library** (AgentOps-AI) — supports 400+ models across providers. Good for multi-provider cost tracking.
- **tiktoken** — 100% for OpenAI, ~12% error rate for Claude. Don't use cross-provider.

**Context management strategy (JetBrains Research 2025):**
- **Observation masking > LLM summarization**: 52% cheaper, no quality loss, 15% faster runtime. Replaces older tool outputs with placeholders while preserving action/reasoning history.
- **Prune first (70-80% capacity):** Remove old tool results, keep metadata. Minimum 20K tokens available for pruning. Protect recent 40K tokens.
- **Compact second (85-95% capacity):** LLM summarization only when pruning exhausted.
- **Emergency stop (95%+):** Force compaction or end session.

**Anthropic Compaction API (Beta):** `compact-2026-01-12` beta flag. Supports custom trigger thresholds (min 50K tokens), `pause_after_compaction` for preserving recent messages, and custom summarization instructions. Cost tracked separately in `usage.iterations`.

**Context rot warning:** Larger context windows don't help — model recall accuracy decreases with token count. LLMs have an "attention budget" that depletes. This reinforces proactive pruning over relying on large windows.

### 6. Tool Registry: ArcAgent Owns, ArcRun Receives

ArcAgent's `ToolRegistry` is THE registry. It handles registration, discovery, permission gating, MCP integration, and audit. When running, it produces a `list[arcrun.Tool]` with wrapped execute functions that handle audit/events/policy. ArcRun receives these tools and executes them.

ArcRun's internal `ToolRegistry` is just an implementation detail (a lookup map). ArcRun doesn't own the concept of tool registration.

### 7. Agent Loop: One Loop (ArcRun)

ArcRun IS the loop. ArcAgent's `agent.py` (originally `loop.py`) is the orchestrator — it prepares inputs, calls `arcrun.run()`, and processes outputs.

```
ArcAgent prepares:
  1. model       <- ArcLLM adapter from config
  2. tools       <- Tool Registry builds list[arcrun.Tool]
  3. system      <- Context Manager assembles from identity.md + policy.md + context.md
  4. task        <- incoming message from channel
  5. hooks       <- on_event -> Module Bus bridge, transform_context -> Context Manager

ArcAgent calls:
  result = await arcrun.run(model, tools, system, task, on_event=bridge, ...)

ArcAgent processes:
  result -> emit post_respond events -> channel sends response -> update memory
```

No second loop. No duplication of ArcRun capabilities.

### 8. Tool Transports: All Four in Phase 1

- `native`: Python function in-process (core tools)
- `mcp`: MCP server via stdio/SSE/HTTP (extensions, third-party)
- `http`: REST API calls (remote services)
- `process`: Subprocess/shell execution (CLI tools, bash)

#### Research Insights: MCP Transport

**SDK:** Use official `mcp` package v1.26.0 (pin `mcp>=1.25,<2` — v2 is pre-alpha). Python 3.12+, asyncio-native throughout.

**Client pattern:** `async with stdio_client(params) as (read, write):` → `async with ClientSession(read, write) as session:` → `await session.initialize()` → `await session.list_tools()` / `await session.call_tool(name, args)`. Always use `AsyncExitStack` for proper cleanup.

**Transport gotchas:**
- **stdio:** Server stdout must be exclusively JSON-RPC. All logs to stderr. Must manage subprocess lifecycle.
- **SSE:** 60s default timeout. Configure `read_timeout_seconds=120`. Implement keep-alive (30s ping).
- **HTTP:** CORS config required. Session resumption via GET for SSE streams.

**Schema mapping:** MCP tools use JSON Schema (Draft 2020-12) for `inputSchema` — maps directly to Pydantic 2.x and arcrun.Tool `input_schema`. No transformation needed.

**Error handling (three-tier model):**
1. Transport: network timeouts, broken pipes → retry with exponential backoff
2. Protocol: JSON-RPC errors (-32700 parse, -32601 method not found, -32800 cancelled) → map to AgentError
3. Application: tool execution failures with `isError` flag → pass back to LLM

**Resilience:** Use tenacity for retry with exponential backoff (max 3 attempts). Add circuit breaker (purgatory or custom) with 5-failure threshold, 60s recovery.

**Security (NIST 800-53 / FedRAMP relevant):**
- Sandbox MCP servers (firejail on Linux, sandbox-exec on macOS, Firecracker for strongest isolation)
- Deny-by-default tool allowlists per agent
- SSRF prevention: block private IP ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16), enforce HTTPS, consider egress proxy
- Audit every tool invocation with correlation ID, agent DID, and timestamp

**Dynamic tool discovery:** MCP supports `notifications/tools/list_changed` for hot-reloading tools. Most frameworks only discover tools once at init — consider subscribing to this notification for long-running agents.

### 9. Config Format: TOML

Consistent with ArcLLM and ArcRun. Uses `tomllib` (stdlib) for parsing, Pydantic for validation. Single config file: `arcagent.toml`.

#### Research Insights: TOML + Pydantic Config

**Pydantic Settings integration:** Use `pydantic-settings` with `TomlConfigSettingsSource`. Supports multiple files with `deep_merge=True` — later files override earlier ones recursively.

**Priority order (customizable via `settings_customise_sources`):**
1. Constructor arguments (highest)
2. Environment variables (with `ARCAGENT_` prefix, `__` nested delimiter)
3. TOML file (defaults and structure)
4. Secret files (lowest)

**Error handling:** `tomllib.TOMLDecodeError` provides line/column numbers for syntax errors. Pydantic `ValidationError` provides key paths (e.g., `llm.model`) but NOT line numbers. Two-phase validation: parse TOML first (catch syntax), then validate with Pydantic (catch semantics).

**Config inheritance pattern:**
```toml
extends = "base-agent.toml"
```
Implement custom loading that resolves `extends` recursively, then deep-merges. Pydantic Settings supports this with `TomlConfigSettingsSource(settings_cls, deep_merge=True)`.

**Hot reload:** Safe for non-critical settings (log levels, feature flags, rate limits). NOT safe for identity, cryptographic keys, or database credentials. Use hash-based change detection, validate before applying, graceful degradation (keep old config on parse/validation failure).

**Secrets:** Store vault references in TOML, resolve at runtime. Pattern: `vault_path = "secret/agents/001"` in config, `SecretManager.resolve_secret(path, key)` at startup. Use `SecretStr` for in-memory protection (prevents accidental logging). For federal: FIPS 140-2 compliant vault, HSM/TPM for private keys, never cache secrets to filesystem.

**Developer experience:** Generate JSON Schema from Pydantic models (`Settings.model_json_schema()`). Build CLI validation: `arcagent config validate my-agent.toml`, `arcagent config generate > example.toml`.

### 10. Telemetry: Build on ArcLLM's OTel

ArcLLM already has full OTel SDK setup (`OtelModule`). ArcAgent creates parent spans (`arcagent.session`, `arcagent.turn`, `arcagent.tool`) that ArcLLM's `arcllm.invoke` spans auto-nest under via OTel context propagation. Structured logging via Python's `logging` module.

### 11. Core File Rename

`arcagent/core/loop.py` -> `arcagent/core/agent.py`

It's the orchestrator that wires components and invokes ArcRun, not a loop.

## Scope

**In**:
- 7 core components: config, identity, telemetry, agent (orchestrator), tool_registry, context_manager, module_bus
- Module Bus with async handlers, priority ordering, EventContext.veto()
- Bridge from ArcRun EventBus to Module Bus
- Context Manager with hybrid token counting, system prompt assembly, compaction
- Tool Registry with all 4 transports, config-driven policy
- TOML config with Pydantic validation
- OTel telemetry building on ArcLLM's SDK setup

**Out**:
- Vault integration (Phase 2)
- mTLS (Phase 2)
- PKI identity with challenge-response (Phase 2)
- Policy engine (Phase 2)
- Module signing (Phase 2)
- NATS inter-agent messaging (Phase 3)

## Open Questions

- (none remaining - all resolved in brainstorm)

## Related Solutions

- (no existing solutions archive entries)

## Architecture Implications

### Updated Project Structure

```
arcagent/
    core/
        agent.py          # Orchestrator (was loop.py) - wires and invokes ArcRun
        config.py          # TOML parser + Pydantic validation
        identity.py        # DID, keypair, file-based keys (Phase 1)
        telemetry.py       # OTel spans building on ArcLLM's SDK
        tool_registry.py   # Registration, discovery, policy, wraps to arcrun.Tool
        context_manager.py # System prompt assembly, token monitoring, compaction
        module_bus.py      # Async event system with bridge to ArcRun EventBus
```

### Integration Flow

```
arcagent.toml
    |
    v
Config (TOML + Pydantic)
    |
    +---> Identity (DID + Ed25519, file-based keys)
    +---> Telemetry (OTel parent spans, structured logging)
    +---> Module Bus (async, priority-ordered, EventContext.veto)
    |         |
    |         +---> bridge subscribes to ArcRun EventBus
    |
    +---> Tool Registry (native/MCP/HTTP/process, policy)
    |         |
    |         +---> produces list[arcrun.Tool] with wrapped execute fns
    |
    +---> Context Manager (system prompt, token budget, compaction)
    |         |
    |         +---> provides transform_context callback to ArcRun
    |
    v
Agent (orchestrator)
    |
    +---> arcllm.load_model() -> LLMProvider
    +---> arcrun.run(model, tools, system, task, on_event=bridge)
    +---> processes LoopResult
```

## Research Sources

### Module Bus & Event Patterns
- [bubus - Production Python Event Bus](https://github.com/browser-use/bubus)
- [aiopubsub Design Blog (Quantlane)](https://quantlane.com/blog/aiopubsub/)
- [FastAPI Middleware Pattern](https://fastapi.tiangolo.com/tutorial/middleware/)
- [HTTPX Event Hooks](https://www.python-httpx.org/advanced/event-hooks/)
- [Chain of Responsibility Pattern](https://refactoring.guru/design-patterns/chain-of-responsibility)
- [Graceful Shutdowns with asyncio](https://roguelynn.com/words/asyncio-graceful-shutdowns/)
- [Exception Handling in asyncio (Piccolo)](https://piccolo-orm.com/blog/exception-handling-in-asyncio/)

### Token Counting & Context Management
- [JetBrains Research: Efficient Context Management (2025)](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)
- [Anthropic Compaction API Docs](https://platform.claude.com/docs/en/build-with-claude/compaction)
- [Anthropic Count Tokens API](https://platform.claude.com/docs/en/api/messages-count-tokens)
- [AgentOps-AI/tokencost](https://github.com/AgentOps-AI/tokencost)
- [Xenova/claude-tokenizer (Hugging Face)](https://huggingface.co/Xenova/claude-tokenizer)
- [OpenCode Context Management (DeepWiki)](https://deepwiki.com/anomalyco/opencode/3.8-context-management-and-compaction)
- [Effective Context Engineering for AI Agents (Anthropic)](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

### MCP Python SDK
- [MCP Python SDK (GitHub)](https://github.com/modelcontextprotocol/python-sdk)
- [MCP Security Best Practices (Official Spec)](https://modelcontextprotocol.io/specification/draft/basic/security_best_practices)
- [mcp-agent (LastMile AI)](https://github.com/lastmile-ai/mcp-agent)
- [Real Python: Build a Python MCP Client](https://realpython.com/python-mcp-client/)
- [Octopus: MCP Timeout and Retry Strategies](https://octopus.com/blog/mcp-timeout-retry)
- [MCP Security Checklist (SlowMist)](https://github.com/slowmist/MCP-Security-Checklist)

### TOML + Pydantic Config
- [Pydantic Settings Management](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [Python tomllib Documentation](https://docs.python.org/3/library/tomllib.html)
- [Python and TOML (Real Python)](https://realpython.com/python-toml/)
- [Config Hot Reload in Python (OneUptime)](https://oneuptime.com/blog/post/2026-01-22-config-hot-reload-python/view)
- [Python Secrets Management (GitGuardian)](https://blog.gitguardian.com/how-to-handle-secrets-in-python/)
