# ADR-002: ArcRun Bridge Pattern vs Event Wrapper

**Status**: Accepted
**Date**: 2026-02-14
**Decision Makers**: Josh Schultz
**Relates to**: S001 Phase 1 Core Components, `agent.py`, `module_bus.py`

---

## Context

ArcAgent needs to intercept events from ArcRun's agent loop (tool calls, turn boundaries) to feed them into the Module Bus for policy enforcement, auditing, and extension hooks. Two primary patterns exist for connecting an external runtime loop to an internal event system.

### The Problem

ArcRun owns the execution loop. It calls `on_event(event)` synchronously during the loop. ArcAgent's Module Bus is async (`bus.emit()` returns `Awaitable[EventContext]`). The tool registry also wraps tool execution with pre/post events. This creates an event ownership question: **who is the source of truth for tool lifecycle events?**

## Options Considered

### Option A: Bridge Pattern (Chosen)

A thin synchronous adapter (`create_arcrun_bridge`) that maps ArcRun event types to Module Bus event names and schedules async emissions via `loop.create_task()`.

```python
def create_arcrun_bridge(bus: ModuleBus) -> Callable[[Event], None]:
    _event_map = {
        "tool.start": "agent:pre_tool",
        "tool.end": "agent:post_tool",
        "turn.start": "agent:pre_plan",
        "turn.end": "agent:post_plan",
    }

    def bridge(event: Event) -> None:
        bus_event = _event_map.get(event.type)
        if bus_event:
            loop = asyncio.get_running_loop()
            task = loop.create_task(bus.emit(bus_event, event.data))
    return bridge
```

**How it works**: ArcRun fires `on_event` synchronously. The bridge maps the event name and schedules the async bus emission as a fire-and-forget task. The bridge does not wait for bus handlers to complete — it cannot, because `on_event` is synchronous.

### Option B: Event Wrapper / Middleware Pattern

Wrap ArcRun's entire execution so that the wrapper controls the event lifecycle, emitting bus events before and after each tool call with full async control.

```python
async def wrapped_run(model, tools, system_prompt, task, bus):
    # Wrap each tool's execute to emit pre/post events
    wrapped_tools = [wrap_tool_with_events(t, bus) for t in tools]
    result = await arcrun.run(model, wrapped_tools, system_prompt, task)
    return result
```

**How it works**: Instead of reacting to ArcRun's events, the wrapper controls tool execution. Each tool's `execute` function is wrapped to emit `agent:pre_tool` *before* execution (with veto support) and `agent:post_tool` *after*. The wrapper owns the event lifecycle, not ArcRun.

### Option C: Dual Event Sources (Hybrid)

Use both: the bridge for turn-level events from ArcRun, and the tool wrapper for tool-level events from the registry.

## Decision

**Option C: Hybrid — Bridge for turns, Wrapper for tools.**

In practice, this is what the Phase 1 implementation does:

- **Tool events** (`agent:pre_tool`, `agent:post_tool`): Owned by `ToolRegistry._create_wrapped_execute()`. Every tool's execute function is wrapped with pre/post bus emissions, veto checking, timeout enforcement, and audit logging. This runs *inside* ArcRun's loop because the wrapped function *is* the tool that ArcRun calls.

- **Turn events** (`agent:pre_plan`, `agent:post_plan`): Owned by the bridge. ArcRun fires these during its loop; the bridge maps and schedules them.

- **Lifecycle events** (`agent:pre_respond`, `agent:post_respond`, `agent:error`): Owned by `ArcAgent.run()`. These fire before/after the entire ArcRun loop invocation.

## Tradeoffs

| Concern | Bridge Only | Wrapper Only | Hybrid (Chosen) |
|---------|------------|-------------|-----------------|
| **Veto support** | No (fire-and-forget) | Yes (sync control) | Yes (wrapper for tools) |
| **Async safety** | Weak (create_task) | Strong (awaited) | Strong for tools, weak for turns |
| **Coupling to ArcRun** | Medium (event format) | Low (wraps tools) | Low (tools independent) |
| **Turn-level events** | Yes (bridge maps) | No (no turn access) | Yes (bridge for turns) |
| **Complexity** | Low | Medium | Medium |
| **Audit completeness** | Gaps (fire-forget) | Complete | Complete for tools |

### Why Not Pure Bridge?

The bridge schedules bus emissions as fire-and-forget tasks (`loop.create_task`). This means:
- **No veto support**: By the time bus handlers run, ArcRun has already moved on
- **No guaranteed ordering**: Handler execution is decoupled from tool execution
- **No error propagation**: Bus handler failures don't stop tool execution

For tool-level events, this is unacceptable — policy modules need veto power over tool calls.

### Why Not Pure Wrapper?

ArcRun owns the turn lifecycle. The wrapper has no access to turn start/end events because those are internal to ArcRun's loop. We'd lose visibility into turn boundaries.

## Prior Art

### Nanobot (CrewAI predecessor)
Uses a **callback chain** pattern where tools register pre/post callbacks. Similar to our wrapper approach but without the bus abstraction. Callbacks are synchronous and tightly coupled to tool definitions. No veto mechanism.

### OpenClaw / AutoGPT
Uses an **event emitter** pattern where the runtime emits events that plugins subscribe to. Closer to our bridge pattern. Events are fire-and-forget with no veto semantics. Extensions react to events but cannot prevent actions.

### LangChain / LangGraph
Uses **callbacks** at the chain/agent level. `CallbackHandler` receives `on_tool_start`, `on_tool_end`, etc. These are purely observational — no veto or policy enforcement. The handler cannot stop a tool call; it can only observe it.

### Semantic Kernel (Microsoft)
Uses **filters** — middleware that wraps function calls with pre/post hooks. Filters can modify arguments, inspect results, and throw exceptions to prevent execution. Closest to our wrapper pattern. Filters are async and support cancellation.

### Key Insight from Prior Art

Every framework that needs *policy enforcement* (not just observation) wraps the tool execution rather than bridging runtime events. Observation-only use cases (logging, metrics) work fine with bridges/callbacks. Our hybrid acknowledges this: **bridge for observation (turns), wrapper for enforcement (tools)**.

## Consequences

### Positive
- Tool veto works: policy modules can prevent tool execution
- Audit trail is complete for tool calls (wrapper awaits bus handlers)
- Turn-level visibility preserved via bridge
- Clean separation of concerns: registry owns tools, bridge owns turns, agent owns lifecycle

### Negative
- Two event paths to understand (bridge + wrapper)
- Turn events are fire-and-forget (no veto on turns — acceptable since LLM calls are read-only)
- Bridge creates pending tasks that may outlive the event that spawned them

### Risks
- **Task leak**: Bridge `create_task` calls create tasks held in a `set()`. If the event loop shuts down before tasks complete, they are cancelled. Current mitigation: the set holds strong references so tasks aren't GC'd prematurely.
- **Double events**: If ArcRun adds more granular tool events in the future, we may get both bridge-emitted and wrapper-emitted pre_tool events. Current mitigation: bridge only maps turn events; tool events come exclusively from the wrapper.

## Future Considerations

If ArcRun adds an async `on_event` variant, the bridge could be simplified to `await bus.emit()` directly, making turn events awaitable with veto support. Until then, the hybrid pattern is the pragmatic choice.
