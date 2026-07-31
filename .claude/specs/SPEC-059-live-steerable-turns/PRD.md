# Product Requirements Document: SPEC-059 — Live, Steerable, Auditable Turns

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
An Arc agent turn is a live, observable, steerable event — not an opaque wait. An operator can watch tokens and one-line status as work happens, redirect the agent mid-turn, and cancel a runaway generation before it finishes — and every one of those interventions is permanently auditable even though it never pollutes the model's durable context.

### Problem Statement
Today the loop calls `model.invoke()` and blocks until the full response lands (`arcrun/strategies/react.py`), so nothing is visible mid-generation and steering can only land at turn boundaries. SPEC-043 **cut** true streaming as "a UX affordance, not a functional need." That framing is now wrong: streaming is the **functional enabler** for mid-stream cancel, mid-stream steering, and mid-stream runaway detection — control and correctness needs, not UX. Separately, our Anthropic adapter has no `invoke_stream` override (falls back to a single Delta), we do not emit explicit `cache_control` breakpoints so frontier prompt-cache hits are unverified, concurrent tool dispatch is all-or-nothing on any shared path, and there is no mechanism to inject a mid-turn message the model sees once without persisting it — nor an audit record when such an injection occurs.

### Value Proposition
One coherent capability — *live steerable auditable turns* — delivered across strict module boundaries (arcllm wire, arcrun loop, arctrust audit, arctui/arcui surface). It closes a real control gap (steer/cancel a running turn), a real cost gap (verified frontier cache hits), a real throughput gap (finer concurrency), and a real compliance gap (every intervention audited) without weakening the four security pillars.

## Personas

- **Federal/Regulated Security Architect** ([product.md#user-personas](../../steering/product.md#user-personas)) — needs every operator intervention on a running agent to be captured in a tamper-evident audit trail (AU-family), regardless of whether it changed model context.
- **Agent Developer (Internal)** ([product.md#user-personas](../../steering/product.md#user-personas)) — needs to steer and cancel running agents, and needs concurrent tools to run fast without racing on shared files.
- **Auditor / Reviewer** ([product.md#user-personas](../../steering/product.md#user-personas)) — needs to reconstruct, after the fact, who steered or cancelled which turn and when.

## User Stories

- **US-1 — Steer a running turn.** As an Agent Developer, I want to redirect or cancel an agent mid-turn so that I can correct course without waiting for the whole generation, and the redirect lands at a safe boundary that never corrupts the message history.
- **US-2 — Watch work happen.** As an Operator, I want to see live tokens and one-line status ("calling `read_file`…") in arctui/arcui, visually distinct from the durable conversation, so that I can observe and judge a turn as it runs.
- **US-3 — Audit every intervention.** As an Auditor, I want every steer/status/cancel injection recorded with who/what/when/`caller_did` so that transient-in-context interventions are still permanently accountable.
- **US-4 — Fast, safe concurrency.** As an Agent Developer, I want independent tool calls to run in parallel and only serialize when they touch the same file, so that batches are fast without risking a write race.
- **US-5 — Verified frontier caching.** As an Operator, I want frontier prompt caching to actually hit (explicit breakpoints where the provider requires them) so that repeated stable prefixes are cheap and the saving is observable.

## Functional Requirements

### C1 — Provider-agnostic streaming abstraction (arcllm)

- **REQ-154** (story US-1, Must): The arcllm streaming abstraction SHALL expose one normalized event vocabulary — `StreamStarted`, `TokenDelta{channel: text|reasoning}`, `ToolCallDelta`, `Completed`, `Failed` — that every provider adapter emits, so consumers treat all providers identically. *(Pillar: Modularity — one contract across 16 providers.)*
- **REQ-155** (story US-1, Must): WHEN any adapter streams a response, THEN the stream SHALL terminate in exactly one `Completed` or one `Failed` event, and never both nor neither. *(Pillar: Simplicity — one unbreakable terminal invariant.)*
- **REQ-156** (story US-1, Must): WHERE an adapter has no native streaming wire format, the abstraction SHALL yield a single terminal event wrapping the whole `invoke()` response, so a non-streaming provider is correct by default. *(Pillar: Simplicity — the fallback is the default, not a special case.)*
- **REQ-157** (story US-1, Must): WHEN streaming from Anthropic, THEN the adapter SHALL emit incremental `TokenDelta` events from the native SSE stream (closing the current single-Delta fallback gap). *(Pillar: Modularity — parity with the frontier default provider.)*
- **REQ-158** (story US-2, Should): WHERE a provider exposes a native streaming wire format (OpenAI-compatible family, Google, Ollama), the adapter SHALL override the fallback to emit incremental deltas. *(Pillar: Scalability — first-token latency visible per provider.)*

### C2 — Correct frontier prompt caching (arcllm)

- **REQ-159** (story US-5, Must): WHEN building an Anthropic request with a stable prefix, THEN the adapter SHALL insert explicit `cache_control` breakpoints per the provider contract (https://platform.claude.com/docs/en/build-with-claude/prompt-caching), never assuming implicit caching. *(Pillar: Simplicity — call it out explicitly, no hidden assumption.)*
- **REQ-160** (story US-5, Must): The adapter SHALL surface provider-reported cache-read and cache-write token counts on the `Usage` object so cache effectiveness is observable. *(Pillar: Scalability — cost/observability of the cache.)*
- **REQ-161** (story US-5, Should): WHERE a provider caches automatically (OpenAI, Google), the adapter SHALL verify (via a repeated stable-prefix request) that cache-read tokens are reported greater than zero, and SHALL NOT inject breakpoints those providers reject. *(Pillar: Modularity — provider-correct behavior, no cross-contamination.)*

### C3 — Per-path concurrent tool execution (arcrun)

> The dispatcher SHALL be designed to the four pillars (Simplicity, Modularity, Security, Scalability) and clean abstraction — the SDD chooses **rebuild vs. extend** on those grounds alone, not by default deference to the current `dispatch_batch`. If rebuilt, the superseded implementation is deleted in the same change (CLAUDE.md: no parallel impls, no legacy).

- **REQ-162** (story US-4, Must): WHILE a non-read-only tool call holds a lock on a filesystem path, any other call in the same batch touching that path SHALL serialize behind it. *(Pillar: Security — no write race on shared state.)*
- **REQ-163** (story US-4, Must): WHERE tool calls in a batch touch disjoint paths (or are all read-only on an unlocked path), they SHALL execute concurrently. *(Pillar: Scalability — parallelism bounded only by genuine path contention.)*
- **REQ-164** (story US-4, Must): IF a tool's read/write scope cannot be determined, THEN the dispatcher SHALL treat it as state-modifying and lock its paths (fail-closed). *(Pillar: Security — unknown defaults to strict.)*
- **REQ-165** (story US-4, Must): WHILE dispatching a batch concurrently, the loop SHALL preserve the SPEC-034 first-DENY policy outcome and the SPEC-035 trifecta ledger with no lost update. *(Pillar: Security — concurrency must not weaken admission.)*

### C4 — Transient mid-turn injection (arcrun)

- **REQ-166** (story US-1, Must): WHEN assembling a turn's request, the loop SHALL drain the transient injection buffer into the outbound request WITHOUT writing those items back to `state.messages`. *(Pillar: Modularity — transient-in-context is a distinct channel from durable history.)*
- **REQ-167** (story US-1, Must): The loop SHALL drain the transient buffer only at safe boundaries (loop top, after a tool batch, turn-end pre/post) and SHALL NOT insert any injection between a `tool_use` and its matching `tool_result`. *(Pillar: Simplicity — one boundary rule that cannot produce an invalid request.)*
- **REQ-168** (story US-1, Must): IF an injection arrives during turn-end bookkeeping, THEN the loop SHALL reopen the turn rather than completing it. *(Pillar: Simplicity — late steer is not lost.)*
- **REQ-169** (story US-1, Must): WHEN an injection arrives while a blocking wait-tool is running, THEN the wait SHALL be interrupted and return a tool_result with a cancelled status (a clean result, not an error). *(Pillar: Simplicity — cancellation is a normal outcome, not a failure.)*

### C5 — Auditability of injections (arctrust)

- **REQ-170** (story US-3, Must): WHEN any injection (steer, status, cancel) is enqueued or drained, THEN an audit event SHALL be emitted carrying who/what/when and the `caller_did`, even though the injection never persists in model context. *(Pillar: Security — transient-in-context, permanent-in-audit; AU-family.)*

### C7 — Mid-stream abort (arcrun)

- **REQ-172** (story US-1, Must): WHILE the loop is consuming a stream, IF a cancel signal, a steer that supersedes the current generation, or a runaway condition is detected, THEN the loop SHALL abort the in-flight generation before its terminal event rather than waiting for it to complete. *(Pillar: Security — this is the functional payoff of streaming: control over a running generation, not just observation.)*

### C6 — Live status surface (arctui + arcui) — UX only

> arctui and arcui are **surfaces only**. Stopping, steering, or cancelling a run is **executed by arcrun (loop) or arccli** — the UI merely emits the D-015 control message and renders. The UI never executes control itself.

- **REQ-171** (story US-2, Must): WHILE an agent turn is running, arctui and arcui SHALL render streaming tokens and transient one-line status messages in the messages view, visually distinct from durable conversation entries. *(Pillar: Simplicity — the operator can tell "happening now" from "part of the record" at a glance.)*
- **REQ-173** (story US-1, Must): WHEN an operator triggers stop/steer/cancel from arctui or arcui, THEN the UI SHALL emit a D-015 control message that arcrun or arccli executes, and SHALL NOT perform the control action itself. *(Pillar: Modularity — control lives in the loop/CLI; the UI is UX-only.)*

## MoSCoW Priorities

| Priority | Requirements | Rationale |
|---|---|---|
| **Must** | REQ-154, 155, 156, 157, 159, 160, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173 | The functional spine: streaming contract + fallback + Anthropic override, explicit Anthropic caching + usage surfacing, per-path concurrency with fail-closed and no admission regression, the full transient-injection boundary rule set, injection audit, **mid-stream abort**, the live UI surface, and UI-emits-control-only. |
| **Should** | REQ-158, REQ-161 | Native streaming overrides for the remaining providers and automatic-cache verification — high value, but the fallback (REQ-156) and Anthropic path (REQ-157/159) make the capability correct without them. |
| **Could** | — | None this spec. |
| **Won't** | File-content checkpoint/undo substrate | Deferred. Checkpoint stays arcrun's concern (`checkpoint.py`), but content-snapshot/undo of agent edits is out of scope here. Mid-stream abort is **in scope** (REQ-172) — it is the required payoff of streaming, not a follow-on. |

## Success Metrics

Framework: [product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework) (security/operational posture, not SaaS).

- **Streaming correctness:** 100% of adapters (all 16 providers) terminate every stream in exactly one `Completed|Failed` under the conformance test; Anthropic emits ≥2 `TokenDelta`s on a multi-token response.
- **Cache effectiveness:** a repeated stable-prefix Anthropic request reports cache-read tokens > 0; the saving is visible on `Usage`.
- **Concurrency safety:** the SPEC-035 trifecta/admission concurrency stress (existing race-regression gate) stays green with per-path locking; a batch of N disjoint-path reads runs in ~1× not ~N× wall-clock.
- **Steering latency:** a steer issued during a blocking wait-tool lands (as a cancelled result) within one poll interval, not at end-of-turn.
- **Audit completeness:** 100% of injections produce a matching audit event with `caller_did`; zero transient injections appear in persisted `state.messages`.

## Risks and Constraints

- **Supersedes SPEC-043 OQ-3 (streaming cut).** SPEC-043 removed true streaming as UX-only. This spec reverses that decision on functional grounds: streaming is the enabler for **mid-stream abort** (REQ-172), steer, and runaway detection — control needs, not UX. The SDD MUST record the reversal and reconcile the out-of-loop `stream_llm_response` primitive into the one streaming path (no two parallel streaming implementations).
- **Mid-stream abort is unbuilt.** Verified: the two most recent commits (`arc agent build` config-surface, dockerize) contain no streaming/abort work; no mid-stream abort exists in `arcrun`/`arcllm` today (only a subprocess-backend `cancel()`). REQ-172 is new required work, not a regression to restore.
- **Overlaps SPEC-043 C4 (concurrency).** `parallel_dispatch.dispatch_batch` with read-only classification is already wired in `react.py`. C3 is designed to the four pillars — the SDD decides rebuild vs. extend on pillar merit; whichever it picks, there is exactly one dispatcher afterward (no fork, no dead parallel impl).
- **Admission TOCTOU (the hard problem, per SPEC-043).** The `snapshot→evaluate→record` critical section in arcagent's tool_registry is arcagent's to keep atomic; arcrun's finer concurrency must not move or weaken that lock. Cross-boundary constraint, called out so it isn't silently violated.
- **Module boundaries are hard.** arcllm never does loop/agent work; arcrun never does LLM-wire or agent work; arctrust imports no sibling. Control (stop/steer/cancel) executes in **arcrun or arccli**; arcui/arctui are UX-only — they emit the D-015 control message and render, and arcrun *emits* the status/token events the UI displays.
- **No legacy shims** (CLAUDE.md): the single-Delta fallback is kept because it is the correct default, not for back-compat; the fake word-split `run_stream` (SPEC-043 cleanup) must be deleted, not layered over.
- **Cost/rate limits:** streaming + caching interact with provider rate limits; conformance tests must use recorded/mocked streams, not live spend, in CI.

## Open Questions

- **OQ-1:** Does the transient buffer reuse the existing `Injection` type + `_inject` seam (adding a `persist: bool`/channel discriminator), or a sibling `TransientInjection` type? Leaning reuse-with-discriminator for simplicity — SDD to decide.
- **OQ-2:** Is per-path locking keyed on normalized absolute paths only, or also on logical resource IDs (e.g. a DB handle) some tools expose? Scope to filesystem paths first unless a tool declares otherwise.
- **OQ-3:** Where does the status-line *content* originate — does arcrun emit structured `ToolCallStarted`/`ToolCallFinished` events the UI renders, or does arcagent compose the one-liner? Concern boundary says arcrun emits structured events; arcagent/UI render text. Confirm in SDD.
- **OQ-4:** Do OpenAI and Google need any request-side change for caching, or is observing/surfacing cache-read usage sufficient (REQ-161)? Verify per current provider docs during SDD.
