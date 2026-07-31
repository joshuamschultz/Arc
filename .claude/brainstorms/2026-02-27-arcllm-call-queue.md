---
topic: ArcLLM Call Queue
date: 2026-02-27
status: complete
---

# ArcLLM Call Queue

## Inspiration

Bio memory consolidation on mosa_agent was silently failing — daily notes saved but no episodes or entities. Root cause: `spawn_background` wraps the entire consolidation coroutine in a 120s timeout, but the coroutine makes 4 sequential LLM calls to o4-mini (a reasoning model, 30-60s per call). The timeout kills the coroutine mid-pipeline.

Deeper problem: the timeout starts at **enqueue time**, not **send time**. If the main loop is already using the LLM, eval calls sit waiting invisibly, and their timeout burns while they're in an invisible queue. This pattern has been observed across multiple modules (bio_memory, policy, scheduler) and across multiple agents on the same Azure endpoint firing rapid calls simultaneously.

The problem isn't execution control flow (arcrun) — it's LLM call management. ArcLLM currently fires every `invoke()` immediately. It should queue, manage concurrency, and ensure calls actually fire before their timeouts start counting.

## Audience

- **ArcLLM itself** — the queue is an internal primitive, invisible to callers
- **All callers of `model.invoke()`** — bio_memory consolidator, policy evaluator, scheduler, deep consolidator, main agent loop, and any future module or extension that makes LLM calls
- **Multi-agent deployments** — multiple agents sharing the same Azure/OpenAI endpoint need coordinated access to avoid rate limit collisions

## Use Cases

- **Eval calls competing with main loop**: Bio memory fires a consolidation eval call while the main agent loop is mid-response. The eval call waits invisibly, its caller-set timeout burns, the entire pipeline dies.
- **Multiple modules firing simultaneously**: On post_respond, both policy and bio_memory trigger eval calls. Both hit the same Azure endpoint. One or both timeout because the provider can only handle N concurrent requests.
- **Rapid-fire calls from different agents**: Multiple agents on azureagent (mosa, josh, brad, brian) all use the same Azure OpenAI deployment. Rapid bursts cause 429s and cascading timeouts.
- **Shutdown race**: On agent shutdown, bio_memory, policy, and potentially scheduler all try final LLM work simultaneously.

## Desired Outcomes

- **Calls just work**: Module authors stop thinking about timeouts, concurrency, and rate limits. They call `model.invoke()` and get a response. ArcLLM handles the rest.
- **Visibility**: Queue depth, wait times, actual send times, and timeout reasons are observable via telemetry. When a call is slow, you can see whether it was slow at the provider or slow waiting in queue.

## Guiding Principles

- **Simplicity first**: Minimal API surface. Callers shouldn't need to understand queue internals. `model.invoke()` works exactly as before — the queue is transparent. No new parameters required (though optional priority hints are fine).
- **Observability**: Every enqueue, dequeue, send, timeout, and retry is an auditable event with OpenTelemetry spans. Queue depth and wait time are metrics. This is federal infrastructure — full audit trail.

## Constraints

- Must fit within arcllm's existing architecture (adapters, providers, retry wrapper)
- Zero new dependencies (use stdlib asyncio primitives)
- Per-process only — cross-machine coordination is a NATS concern
- Must not break the existing `model.invoke()` API contract
- Federal compliance: audit events on all queue operations

## Scope

**In**:
- Bounded concurrency per provider endpoint (configurable max concurrent calls)
- Per-call timeout that starts at **send time**, not enqueue time
- Optional priority levels (main loop > eval > background)
- Queue depth limits with backpressure (reject/skip when full)
- Telemetry: enqueue/dequeue/send/timeout/retry events
- Queue metrics: depth, wait time, active calls

**Out**:
- Not a general-purpose job scheduler (arcagent scheduler module handles that)
- Not cross-machine coordination (NATS handles multi-agent)
- Not smart routing or automatic model selection/fallback (caller picks the model)
- Not retry logic (existing retry wrapper in arcllm already handles this)
- This is only about ensuring that when a call gets sent to arcllm, it fires reliably

## Open Questions

*(none — all resolved during brainstorm)*

## Related Solutions

- `.claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md` — scheduler hardening identified unbounded queue as medium-severity issue (#7). Same pattern applies here.
