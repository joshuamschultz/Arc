# SPEC-027 — Unified Execution Entry: SDD

## 1. Import DAG (the boundaries this must preserve)

```
arctrust (leaf)
   ▲
arcllm ──► arcstore.spool          arcrun ──► arcstore.spool
   ▲                                  ▲  owns: loop + CapabilityProvider Protocol + StreamEvent
   └──────────── arcrun ─────────────┘
                   ▲
                arcagent  (implements CapabilityProvider; assembles Session + capabilities; calls arcrun.run)
                   ▲
                arcgateway (executor adapts StreamEvent → Delta)
                   ▲
              arcui / arccli / scheduler / MAS  (all call agent.run)
```

**Rule:** arcrun imports neither arcagent nor any concrete capability/skill type. It depends only on the `CapabilityProvider` Protocol it defines and on `arcstore.spool`. arcagent depends on arcrun. (FR-5.)

## 2. The one entry

```python
# arcagent/core/agent.py — the ONLY execution method
async def run(self, input: str | Messages, *, session: Session) -> AsyncIterator[StreamEvent]:
    """Drive one agent turn. Always session-bound, always streaming.

    Builds the CapabilityProvider for this turn, assembles messages (system
    prompt + session history + input), and yields arcrun StreamEvents. Appends
    the turn to session.history on completion (parity with the old chat())."""
```

- `Session` is mandatory (FR-1.2). It carries channel identity, the `SessionManager` log, and the DID context. arcgateway already produces channel-specific sessions; CLI/scheduler use `Session.local(key)` (open-or-resume a deterministic local session — D-122 "fresh session per fire" becomes "resume-or-open by deterministic key").
- Deletes `run_async`, `run_stream`, `chat`. `agent_dispatch` loses its three-way fork — one `_dispatch(messages, provider, session) -> AsyncIterator[StreamEvent]` remains.

### 2.1 One-shot helper (for CLI / callbacks)
```python
# arcrun (or arcagent.core) — collect a stream to a final result
async def collect(stream: AsyncIterator[StreamEvent]) -> RunResult: ...
```
CLI: `result = await collect(agent.run(task, session=Session.local(...)))`. Module callbacks (`agent_run_fn`) are bound to a `run`+`collect` closure (FR-2.3) — one shape, no `set_agent_chat_fn`.

## 3. CapabilityProvider (lives in arcrun; implemented in arcagent)

```python
# arcrun/types.py
@runtime_checkable
class CapabilityProvider(Protocol):
    def advertise(self) -> list[CapabilitySpec]: ...                 # lean: name·kind·"use when"·schema
    async def load(self, name: str, *, caller_did: str) -> str | None: ...   # lazy body for context
    async def invoke(self, name: str, args: dict, *, caller_did: str) -> CapabilityResult: ...
```

- `arcrun.run(messages, capabilities: CapabilityProvider, ...)` replaces `tools: list[Tool]`. The internal `ToolRegistry` (arcrun/registry.py) is built from `advertise()`; `list_schemas()` feeds `model.invoke()`; dispatch routes to `capabilities.invoke()`.
- arcagent implements `AgentCapabilityProvider` over the existing `CapabilityLoader` + `capability_registry` (agent_lifecycle.py). `advertise()` = registry manifest; `load()` reads the body from the resolved source path; `invoke()` runs the registered handler through `arctrust` policy.
- **Lazy skills:** a built-in `use_skill(name)` meta-tool is advertised; when the model calls it, arcrun calls `load(name)` and injects the body as that tool's result for the next turn (FR-4.2). Mirrors progressive disclosure.
- **Precedence/trust** are the provider's concern (ADR-023: last-wins, signed-to-load, audit-on-override, federal gate FR-6). arcrun stays oblivious.

## 4. Streaming through the gateway (finish M2)

`executor.py::_stream` today: `result = await agent.chat(...)` → one wrapped `Delta`. New:

```python
async for ev in agent.run(event.message, session=session_for(event)):
    if isinstance(ev, TokenEvent):
        yield Delta(kind="token", content=ev.text, is_final=False, turn_id=turn_id)
    elif isinstance(ev, TurnEndEvent):
        yield Delta(kind="token", content="", is_final=True, turn_id=turn_id)
```

- Delete the `hasattr(agent, "chat")` branch and the "ArcAgent does not expose a true streaming iterator" comment. Backpressure unchanged (bounded per-socket queue, web.py). Error mid-stream → iterator raises → fail-closed Delta + socket close (AC-3.2).

## 5. SPEC-026 recording stays intact

Recording lives in arcllm (llm_call → spool) and arcrun (run_events → spool) + arctrust `emit()` → WORM. Those fire inside the loop regardless of who drives it, so routing all surfaces through one `run` *strengthens* coverage (no surface can bypass recording). FR-5.2 asserts an end-to-end test: drive a turn via `run`, read it back through `arcstore` Observe.

## 6. Feature inventory (migration map — every deleted thing maps to a task)

| Deleted | Replaced by | Task |
|---|---|---|
| `agent.chat` | `agent.run` (+ session) | T-A2 |
| `agent.run_async` | `agent.run` (it's already async-iter) | T-A2 |
| `agent.run_stream` | `agent.run` (it's already streaming) | T-A2 |
| `agent_dispatch` 3-way fork | one `_dispatch` | T-A3 |
| `to_arcrun_tools()` flat list | `AgentCapabilityProvider` | T-C2 |
| arcrun `run(tools=list[Tool])` | `run(capabilities=CapabilityProvider)` | T-C1 |
| executor wrap-as-one-token + chat fork | real `StreamEvent`→`Delta` loop | T-B1 |
| `set_agent_chat_fn` + dual callback shapes | single `agent_run_fn` (run+collect) | T-A4 |

## 7. Module boundaries (explicit)

- **arcrun** — adds `CapabilityProvider` Protocol + `CapabilitySpec`/`CapabilityResult` types + `collect()`; changes `run()` signature. Owns nothing about where capabilities live.
- **arcagent** — implements `AgentCapabilityProvider`; collapses entries to `run`; `Session.local()`; binds module/scheduler callbacks to `run`.
- **arcgateway** — executor adapts the stream; session lookup unchanged.
- **arcllm/arcstore/arctrust** — untouched (recording + policy already in place).

## 8. Test Strategy

- **Unit:** `run` requires session; `collect()` final result; provider `advertise/load/invoke`; lazy `use_skill` loads body once; federal gate denies workspace caps.
- **Integration:** chat streams ≥2 deltas; CLI one-shot collects; scheduler/pulse fire runs a full turn; turn-through-run is readable via arcstore Observe (SPEC-026 intact).
- **Architecture:** arcrun imports no arcagent/concrete-capability type; no `chat`/`run_async`/`run_stream`/`to_arcrun_tools`/`set_agent_chat_fn` references remain; only `agent.run` reaches arcrun.
