# arcrun Decision Log

All architectural decisions are logged here with context, options, reasoning, and status. This log is the source of truth for why things are the way they are.

---

## DECISION-001: Package Name

**Date:** 2026-02-11
**Context:** Original name "arcloop" described the shape (a loop) but not the purpose (agent execution). Needed a name that communicates what the layer does.
**Options:**
- `arcloop` — Describes mechanism, not purpose
- `arcexec` — Direct: "arc execute". Clear execution layer signal.
- `arcrun` — Action-oriented: "arc run". Short, verb-based, implies execution.
- `arcengine` — Engine metaphor from PRD. Heavier word but clear.
- `arcagent` — Most direct for domain but confusable with agent definitions (which live above).

**Decision:** `arcrun`
**Reasoning:** Short, action-oriented, verb-based. "Arc run" communicates exactly what it does — it runs agent tasks. Clean import: `from arcrun import run`. No confusion with the agent definition layer above.
**Status:** Accepted

---

## DECISION-002: Adopt Steering (Mid-Execution Interrupt)

**Date:** 2026-02-11
**Context:** pi-agent-core has `steer()` — ability to inject new instructions while tools are executing. Important for human-in-the-loop and course correction.
**Options:**
- Skip — caller can abort and restart
- Adopt — add `steer(message)` method to inject messages mid-loop

**Decision:** Adopt steering capability
**Reasoning:** Enables human-in-the-loop patterns. When a tool is running and new context arrives (user correction, priority change), the loop can incorporate it instead of completing a potentially wrong path. Critical for enterprise deployments where humans supervise agents.
**Status:** Accepted

---

## DECISION-003: Adopt Context Transform Hook

**Date:** 2026-02-11
**Context:** Long-running loops accumulate messages and hit context limits. pi-agent-core has `transformContext()` — a caller-provided hook to prune/compact messages before each LLM call.
**Options:**
- Skip — caller manages context externally
- Adopt — `transform_context` callback in run() options

**Decision:** Adopt context transform hook
**Reasoning:** arcrun manages the message list inside the loop. Without this hook, the caller has no way to prevent context overflow in long-running tasks. The hook keeps arcrun minimal (just calls the function) while giving callers full control over context management strategy.
**Status:** Accepted

---

## DECISION-004: Adopt Streaming Response Deltas

**Date:** 2026-02-11
**Context:** Currently arcrun waits for full `model.invoke()` response. Streaming would let callers show progress in real-time.
**Options:**
- Skip — full response only
- Adopt — support streaming when arcllm model supports it

**Decision:** Adopt streaming support
**Reasoning:** Better UX for interactive agents. Federal/enterprise dashboards need real-time visibility into what agents are doing. Streaming also enables early cancellation (ties into steering). Implementation: arcllm would need to support streaming invoke, arcrun passes through.
**Status:** Accepted — requires arcllm streaming support (not yet built)

---

## DECISION-005: Adopt Dynamic Tool Registry

**Date:** 2026-02-11
**Context:** pi-agent-core supports hot-reloading tools mid-session. Original arcrun design passes tools at run() and they're fixed.
**Options:**
- Fixed tools only — passed at run(), immutable
- Dynamic registry — tools can be added/removed/replaced during execution

**Decision:** Adopt dynamic tool registry
**Reasoning:** Agents that self-extend (writing their own tools, loading MCP servers mid-task) need to modify available tools during execution. The registry is simple: a mutable dict of Tool objects that the loop reads each turn. Add/remove is just dict operations. Keeps the core simple while enabling powerful patterns.
**Status:** Accepted

---

## DECISION-006: arcrun Owns Run-Level State

**Date:** 2026-02-11
**Context:** Need to decide where state lives during a single run() call. State includes: message history, turn count, token/cost accumulators, tool registry, event log, spawn budget.
**Options:**
- arcrun owns run state — internal RunState during execution, caller gets read access via handle. State dies when run() returns.
- Agent owns all state — arcrun is pure function, messages in, result out. No internal state.
- Shared — arcrun manages but accepts initial state and returns final state.

**Decision:** arcrun owns run-level state
**Reasoning:** arcrun already manages messages internally. Steering requires knowing current state to interrupt. Streaming requires state to emit deltas. Context transform needs access to the message list. RunState is internal to the execution — it dies when run() returns. Cross-session state (memory, user profiles) stays with the agent above. Clean separation: arcrun owns "what's happening right now", agent owns "what happened before and what to do next".
**Status:** Accepted

---

## DECISION-007: Build System

**Date:** 2026-02-11
**Context:** Need a build backend for `pyproject.toml`. arcrun is a library with one dependency (arcllm).
**Options:**
- Hatchling — Modern, minimal config. Official PyPA build backend. Zero extra files.
- Setuptools — Most familiar but more boilerplate. Feels legacy for 2026.
- Poetry — Rich dependency management but heavier. Brings its own lock file. Overkill for a single-dependency library.

**Decision:** Hatchling
**Reasoning:** Lightest config, modern standard, no extra files needed. ~25 lines in pyproject.toml covers everything. Matches the "simple and clear" design priority.
**Status:** Accepted

---

## DECISION-008: Tool Type Implementation

**Date:** 2026-02-11
**Context:** Need to decide how the Tool type supports both simple (pass a function) and complex (stateful, configurable) usage patterns. This is the first thing every caller touches.
**Options:**
- Dataclass + factories — Tool is a dataclass. Complex tools use factory functions returning Tool instances with closures. One pattern to learn.
- Dataclass + subclassing — Tool is a dataclass that can be subclassed (override execute). Two patterns, some devs prefer class-based for complex state.
- Protocol only — Structural typing, anything with the right shape works. Maximum flexibility but no validation at construction time.

**Decision:** Dataclass + factories
**Reasoning:** Follows pi-agent-core's proven pattern. One pattern to learn, not two. Factory functions (`make_search_tool(db)`) handle stateful/configurable tools via closures — achieves everything subclassing does without class inheritance overhead. Simpler DX, simpler internals.
**Status:** Accepted

---

## DECISION-009: Tool.execute Async Requirement

**Date:** 2026-02-11
**Context:** Should Tool.execute accept both sync and async functions, or require async only?
**Options:**
- Async only — execute must be async. Callers wrap sync themselves (trivial).
- Accept both — detect sync/async at construction, auto-wrap sync in asyncio.to_thread(). Friendlier but adds detection logic and threading footgun.

**Decision:** Async only
**Reasoning:** arcllm is async-native. run() is async. Everything in the execution path is async. Adding sync auto-detection adds complexity for a one-liner wrapper the caller can do themselves. Matches arcllm's design.
**Status:** Accepted

---

## DECISION-010: Tool.execute Receives Cancellation Signal

**Date:** 2026-02-11
**Context:** Long-running tools (HTTP requests, subprocess, file operations) need a way to know when to stop if the loop is cancelled or steered.
**Options:**
- Pass cancel signal via ToolContext — tools check ctx.cancelled or await ctx.cancel_event
- No signal — tools run to completion, loop ignores result if cancelled. Wastes compute, can't stop runaway subprocesses.

**Decision:** Pass cancel signal via ToolContext
**Reasoning:** Critical for steering (DECISION-002) and clean shutdown. Without it, a steered loop has no way to tell an in-flight HTTP request or subprocess to stop. Signal is opt-in for tool authors — they can ignore it for simple tools.
**Status:** Accepted

---

## DECISION-011: Typed ToolContext Object

**Date:** 2026-02-11
**Context:** What context does Tool.execute receive about its environment?
**Options:**
- Typed ToolContext dataclass with: run_id, tool_call_id, cancel signal, event_bus, turn_number
- Plain dict with string keys (flexible but no type safety)
- Minimal — just params and cancel signal (simplest but tools can't emit events)

**Decision:** Typed ToolContext dataclass
**Reasoning:** IDE-friendly (autocomplete, type checking). Tools know exactly what's available. Includes run_id and tool_call_id for correlation, cancel signal for shutdown, event_bus for tools that need to emit custom events, turn_number for context awareness. Typed > dict for a public API surface.
**Status:** Accepted

---

## DECISION-012: Tool.execute Returns str

**Date:** 2026-02-11
**Context:** Should execute return a simple string or a richer ToolResult type?
**Options:**
- str — simple, arcrun wraps into message format. Exceptions for errors. Events for observability.
- ToolResult(content, metadata, is_error) — richer but adds ceremony for the 90% case.
- str | ToolResult union — flexible but two code paths internally.

**Decision:** str
**Reasoning:** Simplest thing that works. Errors handled via exceptions (arcrun catches, emits tool.error, returns error string to model). Observability handled via event bus (captures tool name, args, duration, result length). State/rendering metadata belongs to agent layer, not engine. pi needs {content, details} for TUI rendering and session state reconstruction — arcrun does neither. Can extend to str | ToolResult later without breaking existing tools.
**Status:** Accepted

---

## DECISION-013: Generic Event with Dict Data

**Date:** 2026-02-11
**Context:** ~12 event types need data. Choice between typed dataclass per event (~200 lines) vs generic Event with dict data (~10 lines).
**Options:**
- Generic Event + dict data — one Event dataclass, flexible, ~10 lines
- Typed dataclass per event — full autocomplete but ~200 lines (40% of Phase 1 budget)
- Generic Event + TypedDict helpers — middle ground (~80 lines) but callers still get dict

**Decision:** Generic Event with dict data
**Reasoning:** 1,000 line budget demands efficiency. One Event dataclass with type:str + data:dict saves ~190 lines. Expected keys documented in docstrings. Flexible enough for custom events from tool authors. Autocomplete loss is acceptable — event consumers typically switch on event.type anyway.
**Status:** Accepted

---

## DECISION-014: Sandbox Uses Caller-Provided Checker

**Date:** 2026-02-11
**Context:** Sandbox needs to determine what a tool is doing (path access, network, etc.) but tools are caller-defined — arcrun doesn't know tool internals.
**Options:**
- Caller-provided checker — SandboxConfig takes optional async check(tool_name, params) callback
- Tool name allow/deny list only — binary, no param inspection
- Tool-declared capabilities — Tools declare what resources they access
- Convention-based param inspection — scan params for 'path', 'url', etc.

**Decision:** Caller-provided checker
**Reasoning:** Caller knows their tools best. arcrun makes zero assumptions about tool parameter shapes. Default is no-op (allow all when no sandbox). arcrun ships utility functions (path_checker, etc.) that callers compose into their checker. Maximum flexibility, zero coupling to tool internals.
**Status:** Accepted

---

## DECISION-015: Phase 1 Sandbox = Tool-Level + Caller Checker

**Date:** 2026-02-11
**Context:** How much sandbox logic should Phase 1 include?
**Options:**
- Tool-level + caller checker — tool name allowlist + optional check function. Caller implements path checking via their checker using arcrun's path_checker utility.
- Full path checking in Phase 1 — tools declare path_params, sandbox validates against allowed/denied paths
- Just tool-level — only allow/deny by name, no path checking until Phase 4

**Decision:** Tool-level + caller checker
**Reasoning:** Tool name allowlist provides the security gate. Caller-provided check function enables granular control (path checking, network checking, etc.) without arcrun needing to understand tool internals. arcrun ships path_checker utility for callers who want it. Phase 4 adds container isolation and deeper analysis.
**Status:** Accepted

---

## DECISION-016: Allowlist Security Model

**Date:** 2026-02-11
**Context:** What does "deny-by-default" mean when a sandbox is configured?
**Options:**
- Allowlist — only tools in allowed_tools can run, everything else denied
- Denylist — all tools allowed unless in denied_tools list
- Configurable — caller picks default policy

**Decision:** Allowlist model
**Reasoning:** Federal/enterprise safe. Callers explicitly opt in to what runs. Critical property: when dynamic tool registry adds a new tool mid-execution, it's automatically denied (not in allowlist). Prevents privilege escalation via self-extending agents. If no sandbox configured, all tools are allowed (opt-in security).
**Status:** Accepted

---

## DECISION-017: Strategy Enforces Max Turns

**Date:** 2026-02-11
**Context:** Who counts turns and enforces the max_turns limit?
**Options:**
- run() enforces — counts model.invoke() calls, stops strategy at limit. Consistent but rigid.
- Strategy enforces — each strategy manages its own turn count and defines what "turn" means.

**Decision:** Strategy enforces
**Reasoning:** Different strategies have different semantics. CodeExec might count "code executions" not LLM calls. Recursive might count "spawn completions." Strategy owns the loop and knows when to stop. run() is pure orchestration — picks strategy, hands off, gets result. Clean separation of concerns.
**Status:** Accepted

---

## DECISION-018: Text + Tool Calls Preserved in Message

**Date:** 2026-02-11
**Context:** Models sometimes return both text content and tool_calls in one response. What happens to the text?
**Options:**
- Include in assistant message alongside ToolUseBlocks (preserves reasoning chain)
- Emit as event only, don't store (saves context but loses reasoning)

**Decision:** Include in assistant message
**Reasoning:** This is what arcllm's message format expects (TextBlock + ToolUseBlock in content array). The model's reasoning chain is valuable for the next turn. Events also capture it for observability. Discarding would break context that the model might reference in subsequent turns.
**Status:** Accepted

---

## DECISION-019: Strategy Prepends to System Prompt

**Date:** 2026-02-11
**Context:** CodeExec and Recursive strategies need to steer model behavior. How do they inject instructions?
**Options:**
- Prepend to system prompt — strategy instructions + caller's prompt concatenated
- Inject as first user message — keeps prompt clean but adds context token
- Separate system message — clean but not all providers support multiple system messages

**Decision:** Prepend to system prompt
**Reasoning:** Simple concatenation: strategy instructions first, then caller's system prompt. One string, no extra messages, works with every provider. Model sees unified instructions. ReAct (default) adds nothing — caller's prompt used as-is.
**Status:** Accepted

---

## DECISION-020: Two Entry Points — run() + run_async()

**Date:** 2026-02-11
**Context:** How callers start the execution loop. Need to support both simple (fire and forget) and interactive (steering) usage.
**Options:**
- Two functions: run() blocking, run_async() returns RunHandle
- One function always returning RunHandle (simple case needs .result())
- run() with optional on_handle callback

**Decision:** Two functions — run() + run_async()
**Reasoning:** 80% of callers use run() and never think about handles. Clean one-liner: `result = await run(model, tools, prompt, task)`. Steering callers use `handle = await run_async(...)` and get steer/followUp/cancel/result/state. Two patterns but each is clean for its use case.
**Status:** Accepted

---

## DECISION-021: Steer + FollowUp Delivery Modes

**Date:** 2026-02-11
**Context:** For mid-execution steering, should there be one or two message delivery modes?
**Options:**
- Steer only — inject message, skip remaining tools, continue loop
- Steer + followUp — steer interrupts, followUp waits for end_turn then injects
- Defer steering to Phase 4

**Decision:** Steer + followUp
**Reasoning:** Follows pi-agent-core's proven model. Steer handles "stop what you're doing, do this instead." FollowUp handles "when you're done, also do this." Both are needed for human-in-the-loop patterns. Steer checks between tool executions within a turn. FollowUp checks at end_turn before returning LoopResult. Cancel is a third mode — hard stop with partial result.
**Status:** Accepted

---

## DECISION-022: jsonschema for Param Validation

**Date:** 2026-02-11
**Context:** Tool params need validation against input_schema before execute(). Runs on every tool call.
**Options:**
- jsonschema library — standard, well-tested, clear errors. Adds one dependency.
- Manual validation — zero deps, covers required fields + basic types (~30 lines). No $ref/oneOf/pattern support.
- Pydantic (via arcllm) — available but doesn't validate arbitrary JSON Schema, only generates it.

**Decision:** jsonschema library
**Reasoning:** Standard, well-tested, handles edge cases. One-liner validation. Worth the dependency for correctness on every tool call.
**Note:** This conflicts with PRD constraint "zero dependencies beyond arcllm." Accepted as necessary tradeoff — incorrect param validation is a security/reliability risk. jsonschema is small and widely used.
**Status:** Accepted

---

## DECISION-023: Dynamic Tools Denied by Default

**Date:** 2026-02-11
**Context:** When a tool is added to the dynamic registry mid-execution, should it be automatically allowed or denied?
**Options:**
- Denied by default — not in allowlist, automatically denied. Caller must also update sandbox.
- Auto-allow — tools added via registry.add() are automatically allowed.
- Registry notifies sandbox — event-based coupling.

**Decision:** Denied by default
**Reasoning:** Prevents privilege escalation. A self-extending agent can't grant itself dangerous capabilities just by adding tools. Caller must explicitly update both registry and sandbox for new tools. Two-step process is the security tax — worth it for enterprise/federal deployments.
**Status:** Accepted

---

## DECISION-024: Strategies Return LoopResult

**Date:** 2026-02-11
**Context:** Should strategies return the final result or mutate shared state?
**Options:**
- Return LoopResult — strategy is self-contained, builds and returns result
- Mutate RunState — strategy modifies state, run() reads final state

**Decision:** Return LoopResult
**Reasoning:** Strategies own the loop, they own the result. Self-contained, independently testable. run() is pure orchestration — picks strategy, passes config, returns whatever the strategy returns. No shared mutable state between run() and strategy.
**Status:** Accepted

---

## DECISION-025: Exceptions Bubble Up for Model Errors

**Date:** 2026-02-11
**Context:** How does run() handle errors? Tool errors vs model API errors vs budget exceeded.
**Options:**
- Exceptions bubble up — tool errors caught and returned to model; model/network errors raise to caller
- All errors in LoopResult — never raise, caller checks result.error
- Custom exception types — LoopError, ToolError, BudgetExceededError

**Decision:** Exceptions bubble up
**Reasoning:** Tool errors are caught by arcrun and returned to the model as error tool results (model can retry or adjust). Model API errors (network, auth, rate limit) bubble up as exceptions — arcllm handles retries at the invoke level, so anything that reaches arcrun is a real failure. Caller's standard try/except pattern works. Clean separation: recoverable errors stay in the loop, unrecoverable errors exit to caller.
**Status:** Accepted

---

## DECISION-026: Extract Shared Tool Executor

**Date:** 2026-02-14
**Context:** Tool execution pipeline (sandbox check, registry lookup, schema validation, execute, events, error handling) is inline in react.py. Future strategies (CodeExec, Recursive) need the same pipeline. Extracting prevents copy-paste across strategies.
**Options:**
- Keep inline — each strategy implements its own tool execution (duplication)
- Extract to shared module — single function all strategies call
**Decision:** Extract to `src/arcrun/executor.py`
**Reasoning:** DRY. Every strategy needs the same 10-step pipeline. Extraction means one place to add timeout, truncation, rate limiting later. Strategies only own their loop/control flow — tool execution is convention.
**Status:** Accepted

---

## DECISION-027: Single Tool Call Granularity

**Date:** 2026-02-14
**Context:** Should executor expose a single-call function or a batch function?
**Options:**
- Single: `execute_tool_call(tc, state, sandbox)` — strategy loops over calls
- Batch: `execute_tool_calls(tool_calls, state, sandbox)` — executor loops
- Both: single as primitive, batch as convenience
**Decision:** Single tool call only
**Reasoning:** Strategy owns the loop. A strategy may call tools in parallel (asyncio.gather over single calls) or sequentially — that's strategy logic. Executor is just "run this one tool call through the pipeline." Most composable.
**Status:** Accepted

---

## DECISION-028: Executor Returns tuple[Message, bool]

**Date:** 2026-02-14
**Context:** What does execute_tool_call return?
**Options:**
- `tuple[Message, bool]` — message for conversation, bool for success
- `ToolCallResult` dataclass — richer but adds a new type
- Just `Message` — infer success from content (fragile)
**Decision:** `tuple[Message, bool]`
**Reasoning:** Minimal. Message goes into conversation history. Bool lets strategies track errors (for future circuit breaker) without parsing strings. Duration/name already emitted via events. No new types needed.
**Status:** Accepted

---

## DECISION-029: Strategy Owns Cancel/Steer Checks

**Date:** 2026-02-14
**Context:** Should executor check cancel_event and steer_queue before executing, or should strategies handle that?
**Options:**
- Strategy checks — executor is pure execution
- Executor checks — couples executor to control flow
**Decision:** Strategy checks before calling executor
**Reasoning:** Clean separation. Executor = "run this tool." Strategy = "should I run this tool?" Cancel and steering are control flow concerns that vary by strategy. Executor doesn't know about steering, cancel events, or loop state.
**Status:** Accepted

---

## DECISION-030: Executor Increments tool_calls_made

**Date:** 2026-02-14
**Context:** Should executor increment state.tool_calls_made on success, or leave it to strategies?
**Options:**
- Executor increments — consistent counting across all strategies
- Strategy increments — executor stays pure
**Decision:** Executor increments
**Reasoning:** Executor owns the full pipeline: validate → execute → track. Strategies don't need to remember to increment. Prevents counting bugs in future strategies. The counter is part of "executing a tool" not "running a loop."
**Status:** Accepted

---

## DECISION-031: Executor Is Internal (Not Public API)

**Date:** 2026-02-14
**Context:** Should execute_tool_call be exported from __init__.py?
**Options:**
- Internal — strategies import directly, not in public surface
- Public — exported for custom strategy builders
**Decision:** Internal only
**Reasoning:** Same pattern as _messages.py. Strategies import from arcrun.executor directly. Not part of the stable public surface — no stability commitment. Can promote to public later if custom strategies become a first-class use case.
**Status:** Accepted
