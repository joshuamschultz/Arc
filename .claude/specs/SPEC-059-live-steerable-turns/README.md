# SPEC-059 — Live, Steerable, Auditable Turns

**Feature:** Make an Arc agent turn a live, observable, steerable, auditable event — across strict module boundaries. Streaming (arcllm) is the enabler; mid-stream steer/cancel/abort + per-path concurrency + transient injection (arcrun) are the payoff; audit (arctrust) makes every intervention permanent; arctui/arcui are UX-only surfaces.

**Status:** PENDING (planning complete — PRD + SDD + PLAN written and validated; not implemented)
**Branch:** `feat/arc-run-build` (intended — not created; `.claude/` is gitignored by repo convention, so this spec lives on disk only)
**Type:** Multi-concern (arcllm wire + arcrun loop + arctrust schema + arctui/arcui surface)
**Phase:** Phase 2
**Owner note:** Parked for later — implement when in-flight feature branches merge to main. Resume with `/implement SPEC-059`.

## Documents

| Doc | Purpose | Status |
|---|---|---|
| [PRD.md](./PRD.md) | 20 requirements (REQ-154..173), 5 stories, MoSCoW | ✅ validated |
| [SDD.md](./SDD.md) | 8 components (COMP-001..008), architecture, traceability | ✅ validated |
| [PLAN.md](./PLAN.md) | 20 TDD tasks (T-001..020), 4 phases, domain-tagged | ✅ validated |

## The one-line design

`invoke_stream` becomes a true async generator whose `aclose()` tears down the HTTP stream — so **mid-stream abort is just `break` out of `async for`**. Simplicity buys the control feature for free. Everything else hangs off that: the loop consumes events under a cancel token, drains a transient (non-persisted) buffer at safe boundaries, audits every injection, and emits token/status events the UI renders.

## Key decisions

- **D: Supersedes SPEC-043 OQ-3 (streaming cut).** SPEC-043 cut true streaming as "UX-only, not functional." Reversed here on functional grounds — streaming is the enabler for mid-stream abort/steer/runaway-detection (control needs). One streaming path only; the fake `run_stream` word-split is deleted.
- **D: `Delta` → `StreamEvent`.** One discriminated-union event model with a `terminate_once` wrapper enforcing exactly-one-terminal. `Delta` deleted (no parallel model, CLAUDE.md no-legacy).
- **D: Per-path locks, not all-or-nothing.** COMP-004 designed to the four pillars — rebuild-or-extend `dispatch_batch` on merit; exactly one dispatcher remains. Decides ordering only; never touches arcagent's admission critical section.
- **D: Transient-in-context, permanent-in-audit.** `Injection.persist=False` routes to a buffer drained into a request *copy*, never `state.messages`; every injection still emits an audit event with `caller_did`.
- **D: arcui/arctui are UX-only.** Control (stop/steer/cancel) executes in arcrun/arccli via a D-015 control message; the UI emits and renders, never executes.
- **D: Mid-stream abort is in scope (REQ-172).** Verified unbuilt (last two commits = `arc agent build` config-surface + dockerize; only a subprocess-backend `cancel()` exists today).

## Concern boundaries (hard)

| Package | Owns | Must NOT |
|---|---|---|
| **arcllm** | StreamEvent contract, adapter overrides + fallback, cache_control breakpoints, Usage cache fields | know about the loop, tools, or agent state |
| **arcrun** | consuming the stream, per-path concurrency, transient injection, mid-stream abort, emitting UI/audit events | do LLM-wire work or agent work; format prose status text |
| **arctrust** | `AuditEvent` injection schema | import any sibling (leaf) |
| **arctui / arcui** | render live tokens + status; emit D-015 control | execute control locally |

## Deferred / Won't (this spec)

- File-content checkpoint/undo substrate (checkpoint stays arcrun's `checkpoint.py` concern, but content-snapshot/undo is out of scope).
- Logical (non-path) lock keys for tools owning non-filesystem resources (OQ-2) — until a tool needs one.

## Open questions

Carried in PLAN.md §Open Questions: OQ-2 (path-only locks v1), OQ-4 (Google request-side caching?), OQ-5 (Completed payload size). Sequencing: land Phase 1 + T-005/T-006 (Anthropic native stream + explicit cache) first for a demonstrable slice.

## Learnings

_(captured during `/implement`)_
