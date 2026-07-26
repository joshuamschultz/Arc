# ADR-026: `transform_context` Is Append-Only; Compaction Is a Between-Run Boundary Event

**Status**: Accepted
**Date**: 2026-07-02
**Builds on**: ADR-025 (caching in the adapter), ADR-023 (arcrun ↔ arcllm path)
**Relates to**: SPEC-029; supersedes the per-turn graduated-prune model in `ContextManager`

## Context

The provider prompt cache reuses the longest stable prefix of consecutive
requests. The prior `ContextManager.transform_context` pruned a *sliding window*
of tool outputs on every turn above 70% context — rewriting the cached prefix
each turn, so a long-running agent never collected the cache read discount and
paid a write every turn. Meanwhile a second mechanism, `SessionManager.compact`,
already did discrete summarization. Two mechanisms, one of them cache-hostile.

A pure fix ("make `transform_context` identity") is unsafe on its own: `arcrun`'s
ReAct loop makes many model calls **within one dispatch**, but `maybe_compact`
runs only **between dispatches** (`agent_dispatch`), so a single long run has no
compaction boundary and could overflow the provider window mid-run.

## Decision

`transform_context` is **append-only**: it returns messages unchanged so the
cache prefix stays byte-stable. Its only in-turn action is a **last-resort
emergency truncation** if a single run reaches the hard ceiling before a
compaction boundary — it returns a *shorter* list (a discrete reduction), never
a per-turn rewrite of earlier messages, and always keeps at least the newest
message.

All real compaction — deep token-based split, structured summary, observation
masking — is a **discrete, persisted boundary event** owned by
`SessionManager.compact`, triggered by `maybe_compact` off the estimated fill of
the current messages. One cache miss per boundary; append-only in between.

## Consequences

- Long-running agents collect the cache read discount; cost/latency drop.
- `transform_context` is not *pure* identity (retains the valve) — a deliberate
  deviation from the first-cut SDD, documented here.
- Between-run compaction cadence means a single very long run leans on the valve;
  acceptable because the valve is rare and `max_turns` bounds a run.

## Alternatives considered

- **Pure identity `transform_context`** — rejected: no in-run overflow protection.
- **Per-turn masking at a stable boundary** — rejected: the boundary slides as
  messages append, so it still rewrites the prefix each turn.
- **An async per-turn compaction hook in arcrun** — rejected: pushes context
  concerns into the loop nucleus (violates Don't Mix Concerns).
