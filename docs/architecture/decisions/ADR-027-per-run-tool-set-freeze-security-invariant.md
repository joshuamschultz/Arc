# ADR-027: Per-Run Tool-Set Freeze as a Structural Security Invariant

**Status**: Accepted
**Date**: 2026-07-02
**Builds on**: ADR-023 (arcrun receives the resolved tool set), ADR-019 (Four Pillars — universal Sign/Authorize)
**Relates to**: SPEC-029; OWASP ASI04 (Agentic Supply Chain), LLM06 (Excessive Agency)

## Context

`arcrun.ToolRegistry` is built once per run from the caller's resolved tool list
and read every turn via `list_schemas()`. It also exposed `add`/`remove`. Nothing
in the loop mutates it mid-run today, but the *capability* to do so is a live risk:
a mid-run `add` would (a) change the tool-definitions block that sits at the front
of the provider cache prefix — busting the cache — and (b) inject a tool past the
point the caller (and any human/policy review) fixed the set.

## Decision

The per-run `ToolRegistry` is **frozen** by the loop immediately after
construction (`loop.py`, before turn 0). After `freeze()`, `add`/`remove` raise
`RuntimeError` and emit a `tool.mutation_denied` anomaly audit event.
`list_schemas()` is memoized (byte-stable across turns). Dynamic capability
belongs to a **pre-run** rebuild or a **subagent** with its own registry — never
a mid-run mutation of the live set.

This is framed as an **immutability/ordering** invariant that arcrun legitimately
owns — the provider cache hit is an *emergent* benefit, not a caching concern
leaking into the loop.

## Consequences

- The byte-stability caching relies on is a **structural** guarantee, not a
  convention that could silently regress.
- The mid-run tool-injection surface (ASI04/LLM06) is closed; a prompt-injected
  instruction cannot reach a runtime `add`, and no tool enters after the reviewed
  set was fixed. A mutation attempt is an auditable anomaly.
- Construction-time population and the mutation events are preserved for the
  pre-freeze phase (tests, capability assembly).

## Alternatives considered

- **Leave `add`/`remove` open, rely on convention** — rejected: the invariant the
  cache and the security posture depend on would be unenforced.
- **Delete `add`/`remove` entirely** — rejected: they are valid during pre-freeze
  construction; freezing draws the line at run start without losing that.
