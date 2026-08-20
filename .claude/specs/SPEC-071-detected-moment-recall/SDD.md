# Solution Design Document: Detected-Moment Proactive Recall

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

Proactive recall fires on detected loop moments instead of only on an explicit user query. The split seam (approved) is: arcagent's loop emits a general `agent:moment` bus event; arcmemory owns the fire/no-fire decision and the bounded analogical query, exposed as a new `Brain.on_moment` Protocol method. arcmemory never imports arcagent (enforced by tests/architecture/test_no_arcagent_import.py), so the decision cannot be a bus subscriber inside arcmemory — it must be a Protocol method the arcagent memory module calls. The signal crosses the seam as primitives only (kind: str, cues: list[str], text: str), matching the Brain Protocol's primitives-only boundary. See `.claude/steering/structure.md#dependency-direction` and `.claude/steering/structure.md#module-boundaries`.

## Architecture

Flow: agent loop reaches a moment (task run begins, tool/plan about to execute, or a new user turn) -> `bus.emit('agent:moment', {kind, cues, text, session_id})` (general surface; any module may subscribe) -> memory module's `@hook(event='agent:moment')` subscriber resolves clearance and calls `brain.on_moment(kind, cues=..., text=..., clearance=..., ...) -> str` (optional-method pattern, like `authorize`/`holdings` today) -> arcmemory routes kind to its deterministic detector; if the detector fires it runs the gated analogical recall (reusing Retriever so no-read-up gate + attribution audit apply), bounds to max_cards, applies in-window dedup, and returns rendered injectable text (empty string when nothing fires) -> the subscriber buffers that text on the session; the existing `@hook(event='agent:assemble_prompt')` recall handler merges the buffer into `sections['recall']` (append + dedupe against the query-driven recall), reusing the current render/tier path. No new prompt-injection machinery. Boundaries and error-handling per `.claude/steering/tech.md#error-handling-pattern`.

## Components

### COMP-001: agent:moment bus event + emission sites
**Responsibility:** A general loop-signal surface. New thin `bus.emit('agent:moment', {kind, cues, text, session_id})` calls at defined loop points: task_start (modules/tasks/capabilities.py when a task run begins), decision_point (agent:pre_plan, optionally agent:pre_tool), and the user-turn moments entity_seen/topic_shift (at agent:pre_respond, carrying the user text). Any module may subscribe, not only memory. Emission is net-new instrumentation; the ModuleBus mechanism already exists.
**Dependencies:** ModuleBus (core/module_bus.py)
**Inputs:** loop reaching a defined moment
**Outputs:** bus event `agent:moment` with primitive dict {kind: str in {task_start,entity_seen,topic_shift,decision_point}, cues: list[str], text: str, session_id: str|None}

### COMP-002: Brain.on_moment Protocol method
**Responsibility:** The decide seam. New method on arcagent's `Brain` Protocol (core/brain/protocol.py), primitives-only signature, with a no-op default on NullBrain. arcagent calls it; arcmemory implements it. This is how 'memory decides' without arcmemory importing arcagent.
**Dependencies:** Brain Protocol (arcagent core/brain/protocol.py)
**Inputs:** on_moment(kind: str, *, cues: list[str]|None=None, text: str='', clearance: str='unclassified', top_k: int=3, budget: int=512, session_id: str|None=None)
**Outputs:** str (injectable text; '' when no detector fires or dedup suppresses)

### COMP-003: Moment subscriber (arcagent memory module)
**Responsibility:** In modules/memory/capabilities.py: `@hook(event='agent:moment')` handler. Resolves caller clearance, calls `brain.on_moment(...)` via getattr optional-method pattern (like authorize at capabilities.py:66), and buffers the returned non-empty text on the session for injection. Respects the proactive_enabled config flag (no subscription/emit when off).
**Dependencies:** COMP-002, MemoryModuleConfig, _State.brain
**Inputs:** EventContext for agent:moment
**Outputs:** session-scoped proactive buffer updated; no return value

### COMP-004: Detector registry (arcmemory)
**Responsibility:** One deterministic detector per kind {task_start, entity_seen, topic_shift, decision_point}. Each decides fire/no-fire from primitives only (cues, text, session state) with NO LLM call. task_start/decision_point fire on presence; entity_seen fires when cues overlap known entity cards; topic_shift fires on low cue-overlap with the prior turn (MVP: cue-set overlap, not embeddings). Unknown kind -> no fire.
**Dependencies:** arcmemory graph/index (for entity-card lookup)
**Inputs:** (kind: str, cues: list[str], text: str, session_state)
**Outputs:** Detector decision {fire: bool, query_cues: list[str]}

### COMP-005: Proactive recall path (arcmemory)
**Responsibility:** On a fired detector, run the gated analogical recall by reusing Retriever/`recall` so `gate_no_read_up` (security.py:166) and `_emit_recall_attribution` (brain.py:249) apply unchanged. Bound the result to proactive_max_cards. Render to injectable text (reuse render_recalls/enforce_budget). Returns '' if the gated/bounded result is empty.
**Dependencies:** Retriever (retrieve.py), security.gate_no_read_up, brain._emit_recall_attribution, COMP-006, COMP-008
**Inputs:** (query_cues: list[str], clearance: str, max_cards: int, budget: int, session_id)
**Outputs:** rendered injectable text (str), classification-gated and bounded

### COMP-006: In-window dedup (arcmemory)
**Responsibility:** Session-scoped seen-set of injected card identities / cue signatures across a sliding window of proactive_dedup_window turns. Suppresses re-injecting a card already surfaced in-window so successive signals don't repeat the same memory.
**Dependencies:** MemoryConfig.proactive_dedup_window
**Inputs:** (session_id, candidate card ids, window)
**Outputs:** filtered card list (novel-in-window only)

### COMP-007: Injection channel (arcagent memory module)
**Responsibility:** The existing `@hook(event='agent:assemble_prompt')` recall handler (capabilities.py:79) also drains the session proactive buffer and appends it to `sections['recall']`, deduped against the query-driven recall text, reusing the current render/tier path. No separate prompt section, no new machinery. For a background task run, the buffered text lands in that run's own assemble_prompt.
**Dependencies:** COMP-003, SessionContext.assemble_system_prompt (context.py:332)
**Inputs:** proactive buffer + existing sections['recall']
**Outputs:** merged, deduped, bounded recall section in the system prompt

### COMP-008: Config (both sides)
**Responsibility:** arcagent MemoryModuleConfig gains `proactive_enabled: bool` (and optionally the emitted-kinds set) — governs whether arcagent emits/subscribes. arcmemory MemoryConfig gains frozen fields `proactive_max_cards` (default 3), `proactive_dedup_window`, and per-detector thresholds, defaulted per tier via for_tier (federal may default stricter/off). arcmemory tunables flow through provider.py dynamics override.
**Dependencies:** MemoryConfig (config.py), MemoryModuleConfig (arcagent modules/memory/config.py)
**Inputs:** toml config
**Outputs:** typed frozen config fields consumed by COMP-003/005/006

### COMP-009: Audit extension
**Responsibility:** Extend the `memory.recall_attributed` audit extra with the triggering `kind` so every proactive surfacing is attributable to its moment. Reuses brain._emit_recall_attribution; adds a trigger field to its extra dict.
**Dependencies:** brain._emit_recall_attribution (brain.py:249), arctrust audit.emit
**Inputs:** (recalls, trigger_kind)
**Outputs:** AuditEvent action='memory.recall_attributed' extra={cards, trigger}


## Data Model

No persistent schema change. Recall reuses the existing markdown-as-truth + SQLite index stores. In-window dedup state (COMP-006) and the proactive injection buffer (COMP-003/007) are in-memory, session-scoped, and rebuild-free. Config gains frozen fields only. The cross-seam signal is primitives (str/list/dict), so no shared type needs a neutral home; the valid `kind` set is a documented constant validated in on_moment.

## External Integrations

_(none new)_ — reuses arctrust classification (`dominates`/`parse_classification`) and arctrust audit already wired into arcmemory. No external systems, no new store, no FalkorDB/Redis (explicitly out of scope per the chosen substrate).

## Traceability

| Requirement | Components |
|---|---|
| REQ-340 | COMP-002, COMP-003, COMP-007 |
| REQ-341 | COMP-001, COMP-004 |
| REQ-342 | COMP-004 |
| REQ-343 | COMP-003, COMP-004 |
| REQ-344 | COMP-005, COMP-007, COMP-008 |
| REQ-345 | COMP-005 |
| REQ-346 | COMP-006, COMP-008 |
| REQ-347 | COMP-005, COMP-009 |
| REQ-348 | COMP-003, COMP-008 |

## Alternatives Considered

A1: arcmemory subscribes to the bus directly (REJECTED: arcmemory cannot import arcagent; forbidden by test_no_arcagent_import.py — the decision must be a Brain Protocol method). A2: a shared enum/dataclass for the signal in a neutral home (arctrust/primitives) (REJECTED for MVP: a primitive `kind: str` + documented constant keeps the boundary trivial per Simplicity; revisit only if kinds proliferate). A3: a dedicated mid-turn injection channel / synthesized message for same-step recall (REJECTED for MVP: buffer-and-merge into the next assemble_prompt reuses the existing render/tier path; a live mid-turn channel is deferred until a moment needs injection that assemble cannot provide). A4: detect moments inside arcmemory from a raw loop-event stream (REJECTED: couples memory to loop structure; emission stays in arcagent, the only layer that sees the loop). A5: reuse the existing query-driven recall alone and skip proactive (REJECTED: it never fires on task_start/decision_point where there is no user query — the whole point).

## Risks and Mitigations

New emission sites are net-new instrumentation (COMP-001) -> keep each to one thin emit, cover with a real-path test that asserts the event fires. Double-recall overlap between proactive and query-driven recall -> dedupe at injection (COMP-007) and in-window (COMP-006). decision_point on every pre_tool could add latency to tool-heavy turns -> gate decision_point to pre_plan first, pre_tool behind config. Primitives-only signal loses compile-time type safety -> validate kind against the documented constant in on_moment and drop unknown kinds silently (fail-open, no recall). Background-task moments (task_start) rely on each task run reaching its own assemble_prompt for the buffer to land -> verified as a PLAN task; carried as an open question.

## Open Questions

- Does each background task run reach its own agent:assemble_prompt so the task_start proactive buffer injects within that run? Confirm in PLAN before wiring task_start injection.
- Default proactive_max_cards and proactive_dedup_window values — tune against the SPEC-060 LongMemEval harness, not guessed.
- topic_shift detector: MVP cue-set overlap vs a lightweight embedding-distance check on consecutive queries (would touch the read path but add no LLM call) — start with cue overlap.
- decision_point scope: pre_plan only at first, or pre_tool too? Decide by measured latency.
