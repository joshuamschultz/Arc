# Implementation Plan: Context-Aware and Time-Aware Proactive Recall

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- [x] **T-0992**: (red) (red) Recall cards carry a WHEN establishment timestamp
  - domain: test
  - Components: COMP-006
  - Requirements: REQ-356
  - Acceptance: Test: a gated recall over a seeded memory yields cards whose provenance/render includes the memory's establishment timestamp; a record with no timestamp degrades to unstamped, never errors. Fails before impl.
- [x] **T-0993**: (green) (green) Thread establishment timestamp onto RecallCard/Recall + render
  - domain: backend
  - Components: COMP-006
  - Requirements: REQ-356
  - Acceptance: RecallCard/Recall carry the stored timestamp; render_recalls surfaces it; absent timestamp = unstamped. T-0992 passes; mypy --strict + ruff clean; arcmemory no-arcagent-import test green.
- [x] **T-0994**: (red) (red) Config tests: working-set/temporal/decision-point toggles + tier defaults
  - domain: test
  - Components: COMP-012
  - Requirements: REQ-362
  - Acceptance: Tests assert new frozen fields exist with defaults and per-tier variants: arcmemory MemoryConfig gains temporal + working-set knobs; arcagent memory MemoryConfig gains working_set_enabled (default on) and a decision-point pre_tool opt-in flag (default off); a disabled toggle is representable. Fails before impl.
- [x] **T-0995**: (green) (green) Add working-set/temporal/decision-point config fields both sides
  - domain: backend
  - Components: COMP-012
  - Requirements: REQ-362
  - Acceptance: Frozen fields added on both configs with tier defaults; dynamics override still reaches arcmemory fields. T-0994 passes; existing SPEC-071 config tests stay green; mypy + ruff clean.

## Phase 2: Core

- [x] **T-0996**: (red) (red) Working-set tests: accumulate across turns, bounded, salient, detectors fire on it
  - domain: test
  - Components: COMP-001
  - Requirements: REQ-349, REQ-350
  - Acceptance: Tests: on_moment merges each turn's cues into a per-session working set; a later moment fires entity_seen on an entity from a PRIOR turn absent from the current cues/text; the set is bounded (decays by turn, no unbounded growth) and salience-filtered; NO LLM/embedder on the path. Fails before impl.
- [x] **T-0997**: (green) (green) Implement Session WorkingSet + wire into on_moment
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-349, REQ-350
  - Acceptance: A bounded per-session WorkingSet (generalizing prior_cues) is updated in on_moment and fed to the detectors; deterministic, bounded. T-0996 passes; mypy + ruff + arch tests green.
- [x] **T-0998**: (red) (red) Net-new dedup test for working-set-sourced cards
  - domain: test
  - Components: COMP-002
  - Requirements: REQ-351
  - Acceptance: Test: a working-set proactive recall merges into sections['recall'] only cards the same-turn query recall did not already surface (dedup preserved); an identical card is not injected twice. Fails before impl (or asserts a gap in current merge for working-set cards).
- [x] **T-0999**: (green) (green) Preserve net-new dedup for working-set cards in inject_recall
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-351
  - Acceptance: inject_recall/_merge_recall dedups working-set cards against query recall; only net-new cards added. T-0998 passes; SPEC-071 memory tests stay green; ruff clean.
- [x] **T-1000**: (red) (red) Supersession tests: newest current, older marked, never deleted
  - domain: test
  - Components: COMP-007
  - Requirements: REQ-357
  - Acceptance: Tests: two conflicting same-subject facts with different timestamps -> recall surfaces the most recent as current and MARKS the older superseded; the older value/confidence remains on disk (not deleted); deterministic, no LLM. Fails before impl.
- [x] **T-1001**: (green) (green) Implement supersession resolver (mark-not-delete)
  - domain: backend
  - Components: COMP-007
  - Requirements: REQ-357
  - Acceptance: Same-subject conflict resolves newest-current with the older marked superseded; old evidence retained (consistent with fact-currency). T-1000 passes; mypy + ruff + arch green.
- [x] **T-1002**: (red) (red) Time-window filter tests (recent / since / period)
  - domain: test
  - Components: COMP-008
  - Requirements: REQ-358
  - Acceptance: Tests: recall with an optional TimeWindow filters/boosts candidates to the window; None window is a no-op (today's behavior). Deterministic. Fails before impl.
- [x] **T-1003**: (green) (green) Implement TimeWindow filter in retrieve
  - domain: backend
  - Components: COMP-008
  - Requirements: REQ-358
  - Acceptance: An optional TimeWindow threads through retrieve/recall and filters/boosts by window; absent = unchanged. T-1002 passes; mypy + ruff clean.
- [x] **T-1004**: (red) (red) Recency tie-break tests (equal relevance -> newer wins)
  - domain: test
  - Components: COMP-010
  - Requirements: REQ-360
  - Acceptance: Test: two candidates equal on relevance -> the more recent ranks higher; a real relevance gap is NOT overridden by recency. Fails before impl.
- [x] **T-1005**: (green) (green) Implement recency tie-break in fuse/rank
  - domain: backend
  - Components: COMP-010
  - Requirements: REQ-360
  - Acceptance: Recency is the tie-break key only, applied before the bound. T-1004 passes; retrieve/fusion tests stay green; mypy + ruff clean.

## Phase 3: Integration

- [x] **T-1006**: (red) (red) Timeline reader tests: events+daily 'what changed' in order
  - domain: test
  - Components: COMP-009
  - Requirements: REQ-359
  - Acceptance: Tests: a what-changed query over a window reads the stored events stream + daily log and returns dated changes in chronological order; empty (not error) when nothing in the window. Fails before impl.
- [x] **T-1007**: (green) (green) Implement Timeline reader over events + daily log
  - domain: backend
  - Components: COMP-009
  - Requirements: REQ-359
  - Acceptance: Reads existing events/daily stores for a window, returns ordered changes; no new store. T-1006 passes; degrades gracefully when stores empty; mypy + ruff green.
- [x] **T-1008**: (red) (red) decision_point emission tests: pre_plan/pre_tool loop-state cues
  - domain: test
  - Components: COMP-004
  - Requirements: REQ-353, REQ-355
  - Acceptance: Tests (real bus): agent:moment kind=decision_point emits at agent:pre_plan carrying the plan-step cues, and at agent:pre_tool (opt-in) carrying tool name+args; payload is primitive {kind,cues,text,session_id}. Fails before impl.
- [x] **T-1009**: (green) (green) Emit decision_point moments at pre_plan (default) + pre_tool (opt-in)
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-353, REQ-355
  - Acceptance: Thin best-effort emits at the bridge/hook points carrying loop-state cues; pre_tool behind config; no behavior change when unsubscribed. T-1008 passes; ruff + mypy clean.
- [x] **T-1010**: (red) (red) Mid-loop channel tests: transform_context appends staged block, append-only
  - domain: test
  - Components: COMP-003
  - Requirements: REQ-352, REQ-354
  - Acceptance: Tests: a staged decision-point block is appended to the message tail by the agent transform_context callback before the next model call; the prefix is unchanged (append-only holds, ARCRUN_ASSERT_APPEND_ONLY); empty when nothing staged; arcrun still runs unaware (no arcmemory import in arcrun). Fails before impl.
- [x] **T-1011**: (green) (green) Implement mid-loop injection via arcrun transform_context
  - domain: backend
  - Components: COMP-003
  - Requirements: REQ-352, REQ-354
  - Acceptance: arcagent stages the block on a per-run buffer; its transform_context callback appends it (composing with compaction, prefix-stable). arcrun/arcmemory boundaries preserved. T-1010 passes; append-only assertion + arch tests green; mypy + ruff clean.
- [x] **T-1012**: (red) (red) Decision-point routing tests: subscriber routes result to mid-loop buffer, config-gated
  - domain: test
  - Components: COMP-005
  - Requirements: REQ-352, REQ-355
  - Acceptance: Tests: on a decision_point moment (with proactive_decision_point enabled) the memory subscriber calls on_moment and routes non-empty text to the mid-loop buffer (not the assemble buffer); disabled -> no call; pre_tool respected. Fails before impl.
- [x] **T-1013**: (green) (green) Implement decision-point recall routing in the memory subscriber
  - domain: backend
  - Components: COMP-005
  - Requirements: REQ-352, REQ-355
  - Acceptance: Subscriber handles decision_point behind config, routes to the mid-loop buffer, keeps clearance=unclassified + getattr optional-method. T-1012 passes; SPEC-071 subscriber tests stay green; mypy + ruff clean.

## Phase 4: Polish

- [x] **T-1014**: (red) (red) Determinism + boundary tests: no LLM/embedder on trigger/rank path; no arcmemory->arcagent import
  - domain: test
  - Components: COMP-011
  - Requirements: REQ-361, REQ-354
  - Acceptance: Tests: working-set update, detectors, decision-cue extraction, and temporal rank/supersession make no model/embedder call (poison-object seams); arcmemory architecture test (no arcagent, arcrun confined) stays green with the new code. Fails before impl if a path reaches a model.
- [x] **T-1015**: (green) (green) Enforce determinism + boundary on the new paths
  - domain: backend
  - Components: COMP-011
  - Requirements: REQ-361, REQ-354
  - Acceptance: New paths take no model/embedder seam; arcmemory imports neither arcagent nor arcrun-loop internals. T-1014 passes; architecture + dependency-boundary tests green.
- [x] **T-1016**: (green) (green) End-to-end real-path journey (all three capabilities)
  - domain: test
  - Components: COMP-001, COMP-003, COMP-005, COMP-006, COMP-007, COMP-008, COMP-009
  - Requirements: REQ-349, REQ-350, REQ-351, REQ-352, REQ-356, REQ-357, REQ-358, REQ-359, REQ-362
  - Acceptance: Through a real agent run (only the LLM wire faked): (A) a working-set entity from a prior turn surfaces a NET-NEW card the current message's query recall does not; (B) a decision_point recall reaches the model mid-loop before the tool call; (C) a superseded fact shows current+marked-old, a time-window/what-changed query returns ordered dated changes, and every surfaced card is classification-gated + audited (trigger recorded). Falsify each by disabling its toggle.
- [x] **T-1017**: (refactor) (refactor) Degrade + safety hardening + config off-switches
  - domain: backend
  - Components: COMP-012
  - Requirements: REQ-362
  - Acceptance: Every new path degrades not crashes (no embedder -> BM25+graph; no events/daily -> temporal skips; no timestamp -> unstamped); each capability's disable toggle returns exact prior behavior; mid-loop cards are gated+bounded+audited. All SPEC-071 + SPEC-072 tests green; ruff + mypy --strict + architecture tests clean.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-349 | T-0996, T-0997, T-1016 |
| REQ-350 | T-0996, T-0997, T-1016 |
| REQ-351 | T-0998, T-0999, T-1016 |
| REQ-352 | T-1010, T-1011, T-1012, T-1013, T-1016 |
| REQ-353 | T-1008, T-1009 |
| REQ-354 | T-1010, T-1011, T-1014, T-1015 |
| REQ-355 | T-1008, T-1009, T-1012, T-1013 |
| REQ-356 | T-0992, T-0993, T-1016 |
| REQ-357 | T-1000, T-1001, T-1016 |
| REQ-358 | T-1002, T-1003, T-1016 |
| REQ-359 | T-1006, T-1007, T-1016 |
| REQ-360 | T-1004, T-1005 |
| REQ-361 | T-1014, T-1015 |
| REQ-362 | T-0994, T-0995, T-1016, T-1017 |

## Open Questions

- COMP-003 shape: does the mid-loop staging need a thin additive arcrun-facade accessor or can arcagent's existing transform_context read a per-run buffer directly (confirm against context.py:492 + agent_dispatch transform wiring)?
- COMP-001 working-set membership/bound/decay + salience threshold: tune on the SPEC-060 harness.
- COMP-008 TimeWindow vocabulary: structured only, or add a no-LLM natural-language time parser?
- COMP-004 decision_point site: pre_plan-only vs pre_tool, decided by measured latency + recall lift.
