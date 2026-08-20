# Implementation Plan: Detected-Moment Proactive Recall

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- [ ] **T-974**: (red) Contract test: Brain.on_moment signature + NullBrain no-op
  - domain: test
  - Components: COMP-002
  - Requirements: REQ-340
  - Acceptance: Test asserts Brain Protocol declares on_moment(kind, *, cues, text, clearance, top_k, budget, session_id) -> str with primitives only, and NullBrain.on_moment returns '' ; fails before impl.
- [ ] **T-975**: (green) Add on_moment to Brain Protocol + NullBrain default
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-340
  - Acceptance: protocol.py declares on_moment; NullBrain returns ''; T-974 passes; mypy --strict clean.
- [ ] **T-976**: (red) Config tests: proactive fields both sides, tier defaults
  - domain: test
  - Components: COMP-008
  - Requirements: REQ-344, REQ-346, REQ-348
  - Acceptance: Tests assert MemoryConfig.proactive_max_cards (default 3) + proactive_dedup_window are frozen and set per for_tier; MemoryModuleConfig.proactive_enabled exists; fails before impl.
- [ ] **T-977**: (green) Add proactive config fields + provider dynamics wiring
  - domain: backend
  - Components: COMP-008
  - Requirements: REQ-344, REQ-346, REQ-348
  - Acceptance: MemoryConfig + MemoryModuleConfig gain frozen fields; provider.py dynamics override reaches them; T-976 passes; mypy clean.

## Phase 2: Core

- [ ] **T-978**: (red) Detector registry tests: one deterministic detector per kind, no LLM
  - domain: test
  - Components: COMP-004
  - Requirements: REQ-341, REQ-342, REQ-343
  - Acceptance: Tests cover fire/no-fire per kind {task_start,entity_seen,topic_shift,decision_point}, unknown-kind -> no fire, and assert no model/embedder call on the decision path; fails before impl.
- [ ] **T-979**: (green) Implement detector registry (arcmemory)
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-341, REQ-342, REQ-343
  - Acceptance: Deterministic detector per kind returning {fire, query_cues}; entity_seen uses entity-card overlap, topic_shift uses prior-turn cue overlap; T-978 passes.
- [ ] **T-980**: (red) In-window dedup tests (session-scoped seen-set)
  - domain: test
  - Components: COMP-006
  - Requirements: REQ-346
  - Acceptance: Tests assert a card injected within proactive_dedup_window is suppressed on a later signal, and re-eligible after the window; fails before impl.
- [ ] **T-981**: (green) Implement in-window dedup (arcmemory)
  - domain: backend
  - Components: COMP-006
  - Requirements: REQ-346
  - Acceptance: Session-scoped seen-set over card ids across the window filters candidates to novel-in-window; T-980 passes.
- [ ] **T-982**: (red) Proactive recall path tests: gate + bound + attribution
  - domain: test
  - Components: COMP-005, COMP-009
  - Requirements: REQ-344, REQ-345, REQ-347
  - Acceptance: Tests assert a fired detector runs gated recall (a card above clearance is dropped), result is bounded to proactive_max_cards, and a memory.recall_attributed audit fires carrying the trigger kind; fails before impl.
- [ ] **T-983**: (green) Implement on_moment recall path in ArcMemoryBrain
  - domain: backend
  - Components: COMP-005, COMP-002
  - Requirements: REQ-340, REQ-344, REQ-345
  - Acceptance: ArcMemoryBrain.on_moment wires detector -> gated Retriever recall (reusing gate_no_read_up) -> dedup -> bound -> render; returns '' on no-fire/empty; T-982 recall+bound+gate assertions pass.
- [ ] **T-984**: (red) Audit trigger-kind test
  - domain: test
  - Components: COMP-009
  - Requirements: REQ-347
  - Acceptance: Test asserts _emit_recall_attribution includes extra.trigger = kind for a proactive fire; fails before impl.
- [ ] **T-985**: (green) Extend recall attribution with trigger kind
  - domain: backend
  - Components: COMP-009
  - Requirements: REQ-347
  - Acceptance: _emit_recall_attribution adds trigger to the audit extra; explicit-recall path unchanged (trigger absent/none); T-984 passes.

## Phase 3: Integration

- [ ] **T-986**: (red) agent:moment emission tests
  - domain: test
  - Components: COMP-001
  - Requirements: REQ-341
  - Acceptance: Tests assert bus.emit('agent:moment', payload) fires at task run start, at pre_plan (decision_point), and at pre_respond (user-turn) with a primitive {kind,cues,text,session_id} payload; fails before impl.
- [ ] **T-987**: (green) Add agent:moment emission sites (arcagent loop + tasks)
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-341
  - Acceptance: Thin bus.emit('agent:moment', ...) added at the defined sites; any module can subscribe; T-986 passes; no behavior change when unsubscribed.
- [ ] **T-988**: (red) Moment subscriber + injection tests
  - domain: test
  - Components: COMP-003, COMP-007
  - Requirements: REQ-340, REQ-343, REQ-344, REQ-348
  - Acceptance: Tests assert the @hook(agent:moment) subscriber calls brain.on_moment via getattr, buffers non-empty text, assemble_prompt merges it into sections['recall'] deduped against query recall; empty return injects nothing; proactive_enabled=false -> no subscription/emit; fails before impl.
- [ ] **T-989**: (green) Implement moment subscriber + prompt injection merge
  - domain: backend
  - Components: COMP-003, COMP-007
  - Requirements: REQ-340, REQ-343, REQ-344, REQ-348
  - Acceptance: modules/memory/capabilities.py gains the agent:moment subscriber and the assemble_prompt merge; respects proactive_enabled; T-988 passes.
- [ ] **T-990**: (green) End-to-end real-path journey test
  - domain: test
  - Components: COMP-001, COMP-002, COMP-003, COMP-004, COMP-005, COMP-006, COMP-007, COMP-009
  - Requirements: REQ-340, REQ-341, REQ-342, REQ-343, REQ-344, REQ-345, REQ-346, REQ-347
  - Acceptance: Through a real agent turn (only the LLM wire faked): a moment emits -> on_moment fires -> a gated, bounded card appears in the prompt; a card above clearance is excluded; a no-cue turn injects nothing; a repeated signal is deduped; audit records the trigger.

## Phase 4: Polish

- [ ] **T-991**: (refactor) Boundary + latency hardening refactor
  - domain: backend
  - Components: COMP-001, COMP-004
  - Requirements: REQ-342
  - Acceptance: arcmemory no-arcagent-import arch test green; decision_point gated to pre_plan-only behind config (pre_tool opt-in); detector registry tidied with docstrings; all tests still green; ruff + mypy --strict clean.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-340 | T-974, T-975, T-983, T-988, T-989, T-990 |
| REQ-341 | T-978, T-979, T-986, T-987, T-990 |
| REQ-342 | T-978, T-979, T-990, T-991 |
| REQ-343 | T-978, T-979, T-988, T-989, T-990 |
| REQ-344 | T-976, T-977, T-982, T-983, T-988, T-989, T-990 |
| REQ-345 | T-982, T-983, T-990 |
| REQ-346 | T-976, T-977, T-980, T-981, T-990 |
| REQ-347 | T-982, T-984, T-985, T-990 |
| REQ-348 | T-976, T-977, T-988, T-989 |

## Open Questions

- Confirm each background task run reaches its own assemble_prompt so a task_start buffer injects within that run (verify during T-989/T-990).
- Tune proactive_max_cards and proactive_dedup_window against the SPEC-060 LongMemEval harness; defaults (3, TBD) are placeholders.
- topic_shift: keep cue-overlap MVP or add embedding-distance (no-LLM) if recall lift is weak.
- decision_point: pre_plan-only vs pre_tool, decided by measured latency in T-991.
