# Solution Design Document: SPEC-059 — Live, Steerable, Auditable Turns

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

The capability is one data flow: **the model streams events → the loop consumes them under a cancel token → the loop can steer/abort mid-stream and drain transient messages at safe boundaries → every intervention is audited → the UI renders the live tokens and status without owning any control.** Each arrow crosses exactly one module boundary. arcllm owns the wire (streaming + caching); arcrun owns the loop (consuming the stream, per-path concurrency, transient injection, mid-stream abort); arctrust owns the audit schema; arcui/arctui are UX-only surfaces that emit D-015 control messages and render.

The load-bearing design choice: **`invoke_stream` becomes a true async generator whose `aclose()` tears down the underlying HTTP stream.** Mid-stream abort is then not a special mechanism — it is `break` out of `async for`, which closes the generator, which aborts the provider connection. Simplicity buys the control feature for free.

## Architecture

```
 arcui / arctui  ──D-015 control msg──▶  arccli / arcrun.RunHandle  (executes stop/steer/cancel)
      ▲  (render only)                          │
      │ StreamToken / StatusLine / ToolCall*    │ cancel_event / steer → transient buffer
      │ events (event bus)                      ▼
 ┌────┴──────────────────────────────  arcrun loop (react)  ──────────────────────────┐
 │  async for ev in provider.invoke_stream(msgs+transient, ...):                        │
 │      if cancel/steer/runaway: break  ──▶ generator.aclose() ──▶ HTTP stream aborted  │
 │  per-path lock dispatcher for the tool batch                                         │
 │  drain transient buffer at safe boundaries only                                      │
 │  emit injection audit event on the bus ──────────────────────────┐                   │
 └──────────────────────────┬───────────────────────────────────────┼───────────────────┘
             invoke_stream() │ (StreamEvent async gen)               │ AuditEvent (schema from arctrust)
                             ▼                                        ▼
                        arcllm adapters                          arctrust.audit
             (Anthropic/OpenAI/Google/Ollama native;         (leaf: AuditEvent + sink;
              base single-event fallback; cache_control)       imports no sibling)
```

**Dependency direction (unchanged, per structure.md):** arcrun → arcllm; arcrun → arctrust (schema only, arctrust is a leaf); arcui/arctui → arcrun (subscribe to bus, send control). No new edges, no cycles. arcllm never imports arcrun; arctrust imports nobody.

## Components

### COMP-001: `StreamEvent` contract (arcllm)
A discriminated-union of frozen Pydantic models, `type` as the discriminant: `StreamStarted`, `TokenDelta{channel: Literal["text","reasoning"], text: str}`, `ToolCallDelta{index, id?, name?, arguments_delta?}`, `Completed{response: LLMResponse}`, `Failed{error: ArcLLMError}`. `invoke_stream` is retyped `AsyncIterator[StreamEvent]`. **Invariant enforced by a shared async wrapper** `terminate_once(gen)`: guarantees exactly one terminal (`Completed|Failed`) even if an adapter forgets — a bare generator that ends without a terminal is wrapped to emit `Failed(EventStreamError)`, a double-terminal is truncated. Adapters implement the *inner* generator; the wrapper is the unbreakable outer contract. Replaces the existing `Delta` type (deleted — no parallel model). *(Pillars: Simplicity, Modularity.)* — REQ-154, REQ-155

### COMP-002: Adapter stream overrides + fallback (arcllm)
Base `LLMProvider.invoke_stream` default yields `StreamStarted → one TokenDelta(whole content) → Completed(response)` by calling `invoke()` — the correct-by-default path for any non-streaming provider (REQ-156). Native overrides parse each provider's SSE into incremental events: **Anthropic** (`message_start`/`content_block_delta`/`message_delta`/`message_stop`, plus `thinking` deltas → `channel="reasoning"`) closes the current gap (REQ-157); **OpenAI-compatible family** (vLLM, Ollama, together, etc. via the shared `openai.py` wire) and **Google** override the fallback (REQ-158). Each native override is a `@asynccontextmanager`-scoped httpx stream so `aclose()` aborts the connection — the seam COMP-006 relies on. *(Pillar: Modularity — one override per wire family, not per model.)* — REQ-156, REQ-157, REQ-158

### COMP-003: Frontier prompt caching (arcllm)
Anthropic adapter inserts explicit `cache_control: {"type": "ephemeral"}` breakpoints on the largest stable prefix boundaries (system block + tool definitions + leading conversation turns) per the provider contract (https://platform.claude.com/docs/en/build-with-claude/prompt-caching). `cache_control` wire types stay in `anthropic.py` (existing boundary). `Usage` gains `cache_read_tokens` and `cache_write_tokens` fields, populated from each provider's usage block; OpenAI/Google report their automatic cache-read counts into the same fields (no breakpoints injected for those — they reject them). *(Pillars: Simplicity — explicit not implicit; Scalability — cost observable.)* — REQ-159, REQ-160, REQ-161

### COMP-004: Per-path lock dispatcher (arcrun)
`PathLockMap`: per-batch `dict[str, asyncio.Lock]` created only for paths touched by non-read-only calls. Each call resolves its path set (normalized absolute paths from tool args), acquires the relevant locks **in sorted order** (deadlock-free) before executing, releases after. Read-only calls on unlocked paths acquire nothing and run fully concurrent. **Fail-closed:** a call whose read/write scope is unknown is treated as state-modifying and all its path-shaped args are locked (REQ-164). Concurrency does not touch the arcagent admission critical section — each call still flows through the existing `wrapped_execute` (snapshot→evaluate→record), which arcagent keeps atomic per SPEC-043; this dispatcher only decides *ordering*, never policy (REQ-165). **Rebuild-vs-extend is chosen at implementation time on pillar merit; exactly one dispatcher exists afterward** (the current `dispatch_batch` classifier is either extended in place or deleted). *(Pillars: Security, Scalability.)* — REQ-162, REQ-163, REQ-164, REQ-165

### COMP-005: Transient injection buffer + drain points (arcrun)
`RunState.transient: deque[Injection]`. Reuse the existing `Injection` type with a `persist: bool` discriminator (OQ-1 resolved: reuse) — `persist=False` routes here. `drain_transient(state)` appends buffered items to a **copy** of the message list handed to `invoke_stream`, never to `state.messages`. Called only at safe boundaries: loop top, after the tool batch, turn-end pre-bookkeeping, turn-end post-bookkeeping (REQ-166, REQ-167). A drain at turn-end that finds items returns a "reopen" signal so the loop `continue`s instead of completing (REQ-168). Blocking wait-tools run under `asyncio.wait([tool_task, buffer_signal], FIRST_COMPLETED)`; a buffer arrival cancels the wait and yields a `tool_result` with `status="cancelled"` and a clean message (REQ-169). The drain point is structurally forbidden between a `tool_use` and its `tool_result` because it only runs *after* the whole batch resolves. *(Pillars: Simplicity, Modularity.)* — REQ-166, REQ-167, REQ-168, REQ-169

### COMP-006: Mid-stream abort (arcrun)
The loop consumes `async for ev in provider.invoke_stream(...)` and checks, per event: `state.cancel_event.is_set()`, a superseding steer in `state.transient`, and the runaway detector (`_update_runaway`). On any trigger it `break`s; the `async for` exit calls `gen.aclose()`, which (via COMP-002's context-managed streams) aborts the in-flight HTTP request before its terminal. The partial generation is discarded and the loop emits a synthetic terminal so accounting stays consistent. This is the functional payoff of streaming — control over a running generation. Depends on COMP-001 (event loop to break out of) and COMP-002 (closeable streams). *(Pillars: Security, Simplicity.)* — REQ-172

### COMP-007: Injection audit (arctrust schema + arcrun emission)
arctrust (leaf) defines `AuditEvent` fields for injections (`kind: steer|status|cancel`, `caller_did`, `message_id`, `ts`, `drained_at_boundary`). arcrun emits one audit event through its **existing event-bus/spool seam** (the same path `tool.executed` uses) at enqueue and at drain — never importing arctrust beyond the schema, preserving the leaf property. `Injection.new` already requires a non-empty `caller_did`; this component makes the emission mandatory and boundary-tagged. Transient injections produce audit records but appear in zero persisted `state.messages` (REQ-170). *(Pillar: Security — transient-in-context, permanent-in-audit.)* — REQ-170

### COMP-008: Live status surface + control ingress (arcrun emits; arctui/arcui render)
arcrun emits structured UI events on its bus: `StreamToken` (from COMP-001 `TokenDelta`), `StatusLine` (from `ToolCallStarted`/`ToolCallFinished` — one-liners like "calling read_file"), distinct from durable `Message` events. arctui and arcui subscribe and render these visually distinct from durable conversation (REQ-171). Control ingress is the mirror: a stop/steer/cancel action in the UI emits a **D-015 control message** (`{action, target, data, request_id}`, operator identity attached) that `arccli`/`arcrun.RunHandle` executes via existing `steer`/`cancel`/`cancel_event`; the UI performs no control itself (REQ-173). Reuses the arcui WebSocket streaming already built (D-036/D-057). *(Pillars: Modularity, Simplicity.)* — REQ-171, REQ-173

## Data Model

- **`StreamEvent`** (arcllm, frozen Pydantic union) — fields per COMP-001. Replaces `Delta`.
- **`Usage`** (arcllm, extended) — `+ cache_read_tokens: int = 0`, `+ cache_write_tokens: int = 0`.
- **`Injection`** (arcrun, extended) — `+ persist: bool = True`; `persist=False` ⇒ transient buffer. Existing required `caller_did` unchanged.
- **`RunState`** (arcrun, extended) — `+ transient: deque[Injection]`. In-memory only; never serialized into `LoopCheckpoint` (checkpoint stays loop-accounting per PRD).
- **`AuditEvent`** (arctrust, extended) — injection variant fields per COMP-007.
- **`ControlMessage`** (existing, D-015) — reused unchanged for UI→loop control.

## External Integrations

- **Anthropic Messages API** — native SSE streaming (`message_start`…`message_stop`); explicit `cache_control` breakpoints; `thinking` deltas → reasoning channel. Ref: https://platform.claude.com/docs/en/build-with-claude/prompt-caching.
- **OpenAI-compatible wire** (OpenAI, vLLM, Ollama, together, fireworks, groq, deepseek, moonshot, mistral, xai, azure) — SSE `chat.completions` streaming via the shared `openai.py` path; automatic cache-read usage surfaced.
- **Google (Gemini)** — native streaming; automatic caching usage surfaced.
- **httpx** — all streams are context-managed so `aclose()` aborts the connection (the COMP-006 dependency). No new HTTP dependency.

## Traceability

| PRD Requirement | Component(s) |
|---|---|
| REQ-154 (event vocabulary) | COMP-001 |
| REQ-155 (exactly-one terminal) | COMP-001 |
| REQ-156 (single-event fallback = default) | COMP-002 |
| REQ-157 (Anthropic native stream) | COMP-002 |
| REQ-158 (other native overrides) | COMP-002 |
| REQ-159 (explicit Anthropic cache_control) | COMP-003 |
| REQ-160 (cache tokens on Usage) | COMP-003 |
| REQ-161 (verify automatic caching) | COMP-003 |
| REQ-162 (per-path serialize behind write) | COMP-004 |
| REQ-163 (disjoint paths parallel) | COMP-004 |
| REQ-164 (fail-closed unknown scope) | COMP-004 |
| REQ-165 (no admission regression) | COMP-004 |
| REQ-166 (transient drained in, not persisted) | COMP-005 |
| REQ-167 (drain at safe boundaries only) | COMP-005 |
| REQ-168 (late steer reopens turn) | COMP-005 |
| REQ-169 (wait-tool clean cancel) | COMP-005 |
| REQ-170 (injection audit w/ caller_did) | COMP-007 |
| REQ-171 (live status/token render) | COMP-008 |
| REQ-172 (mid-stream abort) | COMP-006 |
| REQ-173 (UI emits control, loop executes) | COMP-008 |

## Alternatives Considered

- **Keep `Delta`, add `StreamEvent` beside it.** Rejected — two parallel streaming models violates CLAUDE.md (no legacy) and the "one path" reconciliation the PRD requires. `Delta` is deleted.
- **Mid-stream abort via a side cancel channel/watchdog task.** Rejected — a second control path is more surface than `break` + `aclose()`. Making the generator closeable is simpler and unbreakable.
- **Global batch lock (current all-or-nothing) kept as-is.** Rejected on Scalability — serializes disjoint-path work needlessly. Per-path locking is the minimal change that removes false contention while staying fail-closed.
- **arcrun imports arctrust audit emitter directly.** Rejected — would risk the leaf property; emission goes through the existing event-bus/spool seam, arctrust supplies only the schema.
- **UI executes stop/cancel locally.** Rejected per user direction — arcui/arctui are UX-only; control executes in arcrun/arccli (REQ-173).

## Risks and Mitigations

- **`Delta` → `StreamEvent` blast radius.** Mitigation: `invoke_stream` is the only public streaming surface; migrate its single OpenAI override + the arcrun consumer + delete the fake `run_stream` word-split (SPEC-043 cleanup) in one change; conformance test per adapter.
- **`aclose()` not aborting a provider stream.** Mitigation: every native override is context-managed httpx; a conformance test asserts the connection closes on early `break` (mock server counts bytes after abort).
- **Concurrency weakening admission (the SPEC-043 hard problem).** Mitigation: COMP-004 decides ordering only; the arcagent `snapshot→evaluate→record` lock is untouched; the existing race-regression stress gate must stay green.
- **Reasoning-channel deltas leaking into durable content.** Mitigation: `channel` is explicit on `TokenDelta`; only `text` channel accumulates into `LLMResponse.content`, `reasoning` into `.thinking`.
- **Transient audit volume.** Mitigation: status-line injections are frequent; audit them at a coarser grain (enqueue + drain, not per-render) to bound spool growth.

## Open Questions

- **OQ-2 (from PRD):** Per-path locking keyed on filesystem paths only for v1; a tool that owns a non-path resource (DB handle) may later declare a logical lock key — deferred until a tool needs it.
- **OQ-3 (from PRD):** Confirmed — arcrun emits structured `ToolCallStarted/Finished` events; arcagent/UI compose the human one-liner text. arcrun does not format prose.
- **OQ-4 (from PRD):** Confirm during implementation whether Google needs any request-side caching change or only usage surfacing (REQ-161); default assumption is surface-only.
- **OQ-5 (new):** Does `StreamEvent.Completed` carry the full `LLMResponse` (simplest for the consumer) or a thin ref the loop resolves? Default: full `LLMResponse` for consumer simplicity; revisit only if payload size on the bus becomes a concern.
