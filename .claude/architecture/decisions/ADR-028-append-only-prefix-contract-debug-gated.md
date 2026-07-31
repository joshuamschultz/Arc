# ADR-028: Append-Only Prefix Contract Enforced Only Under a Debug Flag

**Status**: Accepted
**Date**: 2026-07-02
**Builds on**: ADR-026 (`transform_context` append-only), ADR-027 (tool-set freeze)
**Relates to**: SPEC-029; the < 500ms cold-start and 1000s-of-agents scalability budget

## Context

`transform_context` is a caller-supplied hook (`state.transform_context`) that
arcrun applies to the full message list before each model call. The append-only
contract (ADR-026) is what keeps the provider cache prefix valid. A caller that
violates it — reordering, filtering, or rewriting earlier messages — silently
busts the cache on every turn. We want a way to catch that, without paying a
per-turn verification cost in the hot path.

A full runtime check compares the returned list's prefix against the input every
turn — an O(context) scan per turn, on the path that must stay under a 500ms cold
start and scale to thousands of concurrent agents.

## Decision

arcrun **documents** the append-only contract on the seam
(`loop`/`streams`/`react`/`state` docstrings) and provides a **debug-flag-gated**
assertion (`ARCRUN_ASSERT_APPEND_ONLY=1`) that flags a same-or-longer list whose
prefix was mutated (a per-turn rewrite), while allowing a shorter list (a
deliberate compaction). The assertion is **off by default** — zero cost in
production. Enforcement of the *policy* (compaction is once-per-boundary, not
per-turn) lives with the hook's owner, arcagent.

## Consequences

- The hot path pays nothing in production; the invariant is verifiable in dev/CI
  by exporting one env var.
- A prefix-rewriting `transform_context` in production degrades cache hit rate
  silently rather than failing — accepted, because this is a performance/cost
  guard, not a correctness or security control, and arcagent's `transform_context`
  is append-only by construction.
- arcrun stays caching-agnostic: it asserts *ordering stability*, not cache
  semantics.

## Alternatives considered

- **Always-on runtime check** — rejected: O(context) per turn violates the
  cold-start/scale budget.
- **No check at all** — rejected: a contract with no way to detect violations
  rots; the gated assertion makes it testable at will.
