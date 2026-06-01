---
spec_id: SPEC-028
name: arcrun-tool-observability
status: pending
created: 2026-05-31
type: integration
intake_confidence: 0.90
type_confidence: 0.85
prior_work:
  - .claude/specs/SPEC-026-arcstore-operational-storage/ (the durable spool + Observe plane this extends)
  - .claude/specs/SPEC-027-unified-execution-entry/ (pending; the run/chat unification this is independent of)
related_specs:
  - SPEC-026-arcstore-operational-storage (adds spool kinds + Observe queries here)
  - SPEC-022-arcui-agents-live (the live agent surface this enriches with tool/spawn depth)
trigger: arcui shows LLM calls + run lifecycle + audit chain, but NOT tool-level
  detail. Code executed via arcrun's sandbox (make_execute_tool) and child agents
  spawned via spawn_task surface only as transient in-memory EventBus events
  (tool.start/tool.end) — they are never written to the durable spool, so the
  dashboard cannot show "what code ran" or "what a spawned agent did". Worse,
  spawned-child LLM calls reuse the parent's model/TelemetryModule, so in the
  arcllm view a child's calls are indistinguishable from the parent's.
pillars_priority: [Simplicity, Modularity, Security, Scalability]
---

# SPEC-028 — ArcRun Tool / Code / Spawn Observability

## TL;DR

SPEC-026 gave Arc a durable operational store and made arcui read from it. But it
deliberately scoped the spool to **two** producer streams — `llm_call` (arcllm)
and `run_event` lifecycle (arcrun's `turn.start`/`turn.end`/`loop.completed`/
`strategy.selected`). Everything arcrun does *between* those lifecycle markers —
**every tool call, every sandboxed code execution, every spawned child agent** —
is emitted to the in-memory `EventBus` as `tool.start`/`tool.end`/`tool.error`
and then **dropped**. It never reaches the durable spool, so arcui can't show it.

This spec closes that gap so **arcui gives a full visual of everything arcrun can
do**:

1. **Tool calls** — durably record `tool.start`/`tool.end`/`tool.error` (name,
   args-metadata, result-metadata, latency, outcome) as a new spool kind.
2. **Code execution** — `make_execute_tool` is just a tool, so it rides #1; but
   it gets first-class treatment in the record (the code is a distinct field,
   metadata-only by default per the SPEC-026 `store_raw_bodies` posture) and a
   distinct UI affordance.
3. **Spawned agents** — give each child run its own `actor_did`/`agent_label`
   so (a) the child's `run_event`s spool under the child identity and (b) the
   child's `llm_call`s separate cleanly from the parent's in the arcllm view.
   Record the spawn edge (parent→child) so the UI can render the lineage tree.
4. **arcui** — new Observe queries + dashboard surfaces: a per-run tool/code
   timeline, a spawn lineage tree, and parent-vs-child LLM call separation.

**Module discipline is the whole point of this spec.** arcrun emits tool events
to its own `EventBus` (it already does). arcrun does **not** learn about spawn
(that stays arcagent). arcstore gains a kind. arcllm already carries
`agent_did`/`agent_label` — the fix is making spawn *pass a distinct one*. arcui
only reads. No new transport (SPEC-026 D-007 holds — pull, not push).

## Why this is SPEC-028 and not a SPEC-026 amendment

SPEC-026 is COMPLETE and its scope was explicit: *"`tool.*` events stay in the
in-memory hash chain only — the spool captures run lifecycle, not every tick"*
(`arcrun/events.py` comment). Recording tool/code/spawn detail is **new scope**
with its own security surface (code bodies, child-agent I/O, args that may carry
CUI), its own data model (a new spool kind + a spawn-edge record), and its own UI
work. It deserves its own spec, PRD, and acceptance criteria rather than silently
expanding a closed one.

## Current-state findings (verified in code, 2026-05-31)

| # | Finding | Evidence |
|---|---|---|
| F1 | Tool/code events are emitted but **not durable** | `arcrun/executor.py` emits `tool.start`/`tool.end`/`tool.error` to `EventBus`; `arcrun/events.py` `_RUN_EVENT_TYPES` spools only the 4 lifecycle types |
| F2 | Spool has no tool kind | `arcstore/records.py`: `SpoolKind = Literal["llm_call","run_event","agent_event"]` |
| F3 | Code exec is a tool | `arcrun.make_execute_tool` → rides the same `tool.*` events as any tool |
| F4 | Spawn child LLM calls are **not separated** | `arcagent/orchestration/spawn.py::spawn()` calls `arcrun.run(model=parent._model, …)` with **no `actor_did`**; child reuses the parent's `TelemetryModule`, so `llm_call` records carry the parent's `agent_did`/`agent_label` |
| F5 | Spawn child run events are **not spooled at all** | same call omits `actor_did`; `arcrun.run(..., actor_did=None)` → `EventBus(spool_actor_did=None)` → `_record_run_event` early-returns |
| F6 | Spawn lineage exists only in the audit chain | `spawn.py::_emit_spawn_audit` writes an arctrust `AuditEvent`; nothing operational records the parent→child edge for the UI |
| F7 | arcui can't show any of it | `arcui/observe.py` queries only `llm_calls`, `run_events`, `audit_chain` |

## Status Log

| Date | Status | Note |
|---|---|---|
| 2026-05-31 | PENDING | Spec scoped from a post-SPEC-026 review question ("does arcrun show code/spawn, and does arcllm separate parent vs child calls?"). Findings F1–F7 verified in code. Awaiting approval to implement. |
