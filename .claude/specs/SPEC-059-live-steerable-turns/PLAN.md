# Implementation Plan: SPEC-059 — Live, Steerable, Auditable Turns

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

> Phases align to packages in dependency order: arcllm contract → arcllm adapters + arcrun mechanics → arcrun stream-consumption/abort/audit → UI surface + verification. Every task is TDD (failing test first). Module boundaries are hard — no task crosses a package boundary except the explicit emit/subscribe seams named in the SDD.

## Phase 1: Foundation

*arcllm streaming contract — unblocks everything.*

- **T-001** [domain: backend] (COMP-001, REQ-154, REQ-155): Define the `StreamEvent` frozen Pydantic union (`StreamStarted`, `TokenDelta{channel}`, `ToolCallDelta`, `Completed`, `Failed`) and the `terminate_once` async wrapper guaranteeing exactly one terminal. Test-first: wrapper emits `Failed` for a generator that ends with no terminal, truncates a double-terminal.
- **T-002** [domain: test] (COMP-001, REQ-155): Conformance test harness asserting the exactly-one-terminal invariant against a fake adapter (drives T-019's suite).
- **T-003** [domain: backend] (COMP-002, REQ-156): Retype `LLMProvider.invoke_stream` → `AsyncIterator[StreamEvent]`; base fallback yields `StreamStarted → one TokenDelta(whole content) → Completed`. Delete the `Delta` type (no parallel model). Test-first: a non-overriding adapter yields exactly those three events.
- **T-004** [domain: backend] (COMP-003, REQ-160): Extend `Usage` with `cache_read_tokens`/`cache_write_tokens` (default 0). Test-first: usage round-trips the new fields.

## Phase 2: Core

*Native adapters + loop concurrency + transient buffer.*

- **T-005** [domain: backend] (COMP-002, REQ-157): Anthropic native streaming override — SSE parse (`message_start`…`message_stop`), `thinking` deltas → `channel="reasoning"`, context-managed httpx stream. Test-first: recorded SSE fixture yields ordered `TokenDelta`s + one `Completed`.
- **T-006** [domain: backend] (COMP-003, REQ-159): Anthropic explicit `cache_control` breakpoints on the stable prefix (system + tools + leading turns). Test-first: request body carries breakpoints at the expected boundaries.
- **T-007** [domain: backend] (COMP-002, REQ-158): Native streaming overrides for the OpenAI-compatible wire family (covers vLLM/Ollama/together/etc.) and Google. Test-first: recorded stream fixtures per wire family yield incremental deltas.
- **T-008** [domain: backend] (COMP-003, REQ-161): Surface automatic cache-read usage for OpenAI/Google into `Usage`; assert no breakpoints injected for providers that reject them. Test-first: usage mapping from recorded responses.
- **T-009** [domain: backend] (COMP-004, REQ-162, REQ-163, REQ-164): Per-path lock dispatcher — `PathLockMap`, sorted lock acquisition (deadlock-free), read-only calls on unlocked paths run free, unknown scope → fail-closed (all path-args locked). Rebuild-or-extend the current `dispatch_batch` on pillar merit; exactly one dispatcher remains. Test-first: disjoint→parallel, shared→serialized, unknown→locked.
- **T-010** [domain: test] (COMP-004, REQ-165): Concurrency safety test — the SPEC-035 admission race-regression stress stays green under per-path dispatch; no lost trifecta update, no N-way overspend.
- **T-011** [domain: backend] (COMP-005, REQ-166, REQ-167): Transient injection — `Injection.persist` discriminator, `RunState.transient` deque, `drain_transient` appends to a request-message **copy** (never `state.messages`), callable only at the four safe boundaries. Test-first: drained item reaches the outbound request and is absent from `state.messages`.

## Phase 3: Integration

*Loop consumes the stream; mid-stream abort; audit.*

- **T-012** [domain: backend] (COMP-001, COMP-006, REQ-154): Loop consumes `async for ev in provider.invoke_stream(...)` in place of `invoke()`; delete the fake `run_stream` word-split (SPEC-043 cleanup) so there is one streaming path. Test-first: loop drives a fake stream to completion, accumulating text vs reasoning by channel.
- **T-013** [domain: backend] (COMP-006, REQ-172): Mid-stream abort — per-event check of `cancel_event`, superseding steer, and `_update_runaway`; on trigger `break` → `aclose()` → HTTP teardown; emit a synthetic terminal so accounting stays consistent. Test-first: a cancel set mid-stream stops consumption before the terminal.
- **T-014** [domain: test] (COMP-006, REQ-172): Abort conformance — a mock streaming server asserts zero bytes are read after an early `break`/`aclose()` (the connection actually aborts).
- **T-015** [domain: backend] (COMP-005, REQ-168, REQ-169): Late-steer reopen (turn-end drain returns a reopen signal → loop `continue`s) and blocking wait-tool clean cancel (`asyncio.wait(..., FIRST_COMPLETED)` → `status="cancelled"` tool_result, not an error). Test-first: both paths.
- **T-016** [domain: auth] (COMP-007, REQ-170): Injection audit — arctrust `AuditEvent` injection variant (kind/caller_did/message_id/ts/boundary); arcrun emits at enqueue + drain via the existing event-bus/spool seam (no direct arctrust import beyond schema). Test-first: every injection yields a matching audit event with `caller_did`; zero transient items in persisted messages.

## Phase 4: Polish

*UX surfaces (emit/render only) + cross-provider verification.*

- **T-017** [domain: ui] (COMP-008, REQ-171): arcrun emits `StreamToken`/`StatusLine`/`ToolCallStarted`/`ToolCallFinished` events on the bus; arctui renders them live, visually distinct from durable conversation. Test-first: arctui renders a token/status stream without mutating durable history.
- **T-018** [domain: ui] (COMP-008, REQ-171, REQ-173): arcui renders live stream + status; UI stop/steer/cancel emits a **D-015 control message** consumed by `arccli`/`arcrun.RunHandle` — the UI executes no control itself. Test-first: a UI control action produces a D-015 message and no local execution.
- **T-019** [domain: test] (COMP-001, COMP-002, REQ-155): Cross-provider streaming conformance suite — all adapters terminate every stream in exactly one `Completed|Failed`, using recorded fixtures (no live spend in CI).
- **T-020** [domain: test] (COMP-003, REQ-159, REQ-160, REQ-161): Cache verification — a repeated stable-prefix Anthropic request reports `cache_read_tokens > 0`; OpenAI/Google automatic cache-read observed and surfaced.

## Traceability

| Task | Requirement(s) | Component(s) |
|---|---|---|
| T-001 | REQ-154, REQ-155 | COMP-001 |
| T-002 | REQ-155 | COMP-001 |
| T-003 | REQ-156 | COMP-002 |
| T-004 | REQ-160 | COMP-003 |
| T-005 | REQ-157 | COMP-002 |
| T-006 | REQ-159 | COMP-003 |
| T-007 | REQ-158 | COMP-002 |
| T-008 | REQ-161 | COMP-003 |
| T-009 | REQ-162, REQ-163, REQ-164 | COMP-004 |
| T-010 | REQ-165 | COMP-004 |
| T-011 | REQ-166, REQ-167 | COMP-005 |
| T-012 | REQ-154 | COMP-001, COMP-006 |
| T-013 | REQ-172 | COMP-006 |
| T-014 | REQ-172 | COMP-006 |
| T-015 | REQ-168, REQ-169 | COMP-005 |
| T-016 | REQ-170 | COMP-007 |
| T-017 | REQ-171 | COMP-008 |
| T-018 | REQ-171, REQ-173 | COMP-008 |
| T-019 | REQ-155 | COMP-001, COMP-002 |
| T-020 | REQ-159, REQ-160, REQ-161 | COMP-003 |

## Open Questions

- **OQ-2:** Per-path locking is filesystem-paths-only for v1 (COMP-004); logical resource keys deferred until a tool declares one.
- **OQ-4:** Confirm at T-008 whether Google needs a request-side caching change or only usage surfacing (REQ-161) — default surface-only.
- **OQ-5:** `StreamEvent.Completed` carries the full `LLMResponse` (COMP-001) unless bus payload size proves a problem; revisit at T-017/T-018.
- **Sequencing note:** T-005/T-006 (Anthropic) are the critical path — they close the two headline gaps (missing native stream, unverified cache). Land Phase 1 + T-005/T-006 first for a demonstrable slice before the remaining adapters.
