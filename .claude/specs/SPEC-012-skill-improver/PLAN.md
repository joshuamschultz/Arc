# PLAN: Skill Improver Module

**Spec ID**: SPEC-012
**Status**: COMPLETE
**Date**: 2026-02-25
**Completed**: 2026-02-26

## Implementation Phases

### Phase A: Data Models + Config + Module Skeleton

**Rationale**: Foundation for everything else. Establishes types, config, and module structure.

**Files created**: `modules/skill_improver/MODULE.yaml`, `__init__.py`, `config.py`, `models.py`, `skill_improver_module.py` (skeleton)

- [x] A1: Create `MODULE.yaml` manifest (name, entry_point, version, events)
- [x] A2: Create `config.py` with `SkillImproverConfig(ModuleConfig)` — all config fields with defaults
- [x] A3: Create `models.py` — `ToolCallRecord`, `SkillTrace`, `Candidate`, `OptimizeResult`, `MutationEvent`, `DimensionScore`, `EvalResult`
- [x] A4: Write failing tests for config validation (extra="forbid", defaults, type checking)
- [x] A5: Write failing tests for model serialization/deserialization (JSON round-trip for JSONL storage)
- [x] A6: Implement config and models to pass tests
- [x] A7: Create module skeleton (`skill_improver_module.py`) — name property, empty startup/shutdown
- [x] A8: Write failing test for module loading via `ModuleLoader` (discovers MODULE.yaml, instantiates)
- [x] A9: Implement module loading (constructor injection pattern)
- [x] A10: All tests green, ruff clean, mypy clean

**Phase A completion**: 10/10

---

### Phase B: Trace Collector

**Rationale**: Must collect data before we can optimize. Core passive infrastructure.

**Files created**: `trace_collector.py`
**Files modified**: `skill_improver_module.py` (wire up events)

- [x] B1: Write failing tests for skill path indexing (maps SkillRegistry paths to names)
- [x] B2: Write failing tests for skill read detection (read tool + matching path = span opens)
- [x] B3: Write failing tests for tool call recording within an active span
- [x] B4: Write failing tests for span close at turn end (post_plan)
- [x] B5: Write failing tests for usage counting (per-skill use count tracking)
- [x] B6: Write failing tests for JSONL storage (write trace, read traces, monthly rotation)
- [x] B7: Write failing tests for trace data sanitization (args hashed, task truncated to 200 chars)
- [x] B8: Implement `TraceCollector` to pass all tests
- [x] B9: Wire trace collector into module facade (subscribe to post_tool, post_plan, ready events)
- [x] B10: Integration test: mock agent turn with skill read → verify trace captured correctly
- [x] B11: All tests green, ruff clean, mypy clean

**Phase B completion**: 11/11

---

### Phase C: Guardrails

**Rationale**: Safety checks must exist before the engine runs. Prevents any optimization without guardrails.

**Files created**: `guardrails.py`

- [x] C1: Write failing tests for `check_eligible` — min traces, cooloff, exempt tags, generation limit
- [x] C2: Write failing tests for `validate_candidate` — intent preserved, token budget, anchor distance, oscillation
- [x] C3: Write failing tests for intent header parsing (`## SKILL INTENT [IMMUTABLE]` extraction and comparison)
- [x] C4: Write failing tests for anchor distance (SequenceMatcher ratio as proxy)
- [x] C5: Write failing tests for oscillation detection (fingerprint comparison against recent versions)
- [x] C6: Write failing tests for cooloff state management (set cooloff, check cooloff, expire cooloff)
- [x] C7: Implement `Guardrails` to pass all tests
- [x] C8: All tests green, ruff clean, mypy clean

**Phase C completion**: 8/8

---

### Phase D: Evaluator (LLM-as-Judge)

**Rationale**: Engine needs evaluation before it can select or reject candidates.

**Files created**: `evaluator.py`

- [x] D1: Write failing tests for judge prompt construction (one dimension, includes checklist + anchors + trace + anti-inflation)
- [x] D2: Write failing tests for score parsing (JSON response → DimensionScore with checklist results)
- [x] D3: Write failing tests for per-trace evaluation (calls judge once per dimension per trace)
- [x] D4: Write failing tests for aggregate score computation (mean across traces per dimension)
- [x] D5: Write failing tests for malformed LLM response handling (graceful fallback)
- [x] D6: Implement `SkillEvaluator` to pass all tests (mock arcllm calls)
- [x] D7: All tests green, ruff clean, mypy clean

**Phase D completion**: 7/7

---

### Phase E: Pareto Frontier

**Rationale**: Core selection mechanism. Engine depends on it.

**Files created**: `pareto.py`

- [x] E1: Write failing tests for per-example dominance (A >= B on all traces, > on at least one)
- [x] E2: Write failing tests for token count as Pareto dimension (lower is better)
- [x] E3: Write failing tests for frontier add (add non-dominated, evict dominated)
- [x] E4: Write failing tests for add_if_improves (min-delta gate over parent)
- [x] E5: Write failing tests for frontier select (weighted random from frontier)
- [x] E6: Write failing tests for overall_best (highest average across dimensions)
- [x] E7: Write failing tests for frontier serialization (JSON round-trip for manifest)
- [x] E8: Implement `ParetoFrontier` to pass all tests
- [x] E9: All tests green, ruff clean, mypy clean

**Phase E completion**: 9/9

---

### Phase F: Reflector + Candidate Store

**Rationale**: Mutation generation and persistence. Engine depends on both.

**Files created**: `reflector.py`, `candidate_store.py`

- [x] F1: Write failing tests for reflection prompt construction (constrained, section-targeted, token budget, intent preservation instruction)
- [x] F2: Write failing tests for candidate text extraction from LLM response (markdown fences)
- [x] F3: Write failing tests for weak dimension identification (finds dimensions with lowest scores)
- [x] F4: Write failing tests for failure pattern extraction (groups failures by common patterns)
- [x] F5: Implement `SkillReflector` to pass all tests (mock arcllm calls)
- [x] F6: Write failing tests for candidate store — save, load, get frontier, get active, manifest
- [x] F7: Write failing tests for seed snapshot (saved on first optimization, never modified)
- [x] F8: Write failing tests for audit log (append-only JSONL, MutationEvent serialization)
- [x] F9: Write failing tests for rollback (activate previous candidate, set cooloff)
- [x] F10: Implement `CandidateStore` to pass all tests
- [x] F11: All tests green, ruff clean, mypy clean

**Phase F completion**: 11/11

---

### Phase G: Optimization Engine

**Rationale**: Depends on all prior phases. Orchestrates the full optimization loop.

**Files created**: `engine.py`
**Files modified**: `skill_improver_module.py` (wire up engine + trigger)

- [x] G1: Write failing tests for trace splitting (70/30 train/holdout, deterministic seed)
- [x] G2: Write failing tests for minibatch sampling (random subset of train traces)
- [x] G3: Write failing tests for single optimization iteration (select → evaluate → reflect → validate → add)
- [x] G4: Write failing tests for stagnation detection (stop after N gens with no frontier change)
- [x] G5: Write failing tests for full optimization loop (multiple iterations, returns OptimizeResult)
- [x] G6: Write failing tests for post-optimization application (atomic write, registry rescan, audit log)
- [x] G7: Implement `SkillOptimizer` to pass all tests (mock evaluator + reflector)
- [x] G8: Wire engine into module facade (spawn_background on usage threshold)
- [x] G9: Write failing tests for trigger mechanism (usage count → spawn_background)
- [x] G10: Implement trigger in module facade
- [x] G11: All tests green, ruff clean, mypy clean

**Phase G completion**: 11/11

---

### Phase H: Tools + Integration Testing

**Rationale**: Final tools and end-to-end validation.

**Files modified**: `skill_improver_module.py` (register tools)

- [x] H1: Write failing tests for `skill_versions` tool (returns version history for a skill)
- [x] H2: Write failing tests for `skill_rollback` tool (reverts to previous version, sets cooloff)
- [x] H3: Implement both tools with RegisteredTool + JSON schemas
- [x] H4: Integration test: full module lifecycle (startup → collect traces → trigger optimization → verify skill updated)
- [x] H5: Integration test: rollback scenario (optimize → rollback → verify cooloff)
- [x] H6: Integration test: guardrail enforcement (insufficient traces → no optimization)
- [x] H7: Integration test: exempt skill (tagged security-critical → never optimized)
- [x] H8: All tests green, ruff clean, mypy clean, full test suite passes
- [x] H9: Verify LOC budget (< 1,400 production LOC)

**Phase H completion**: 9/9

---

## Summary

| Phase | Tasks | Focus | Status |
|-------|-------|-------|--------|
| A | 10 | Foundation: models, config, skeleton | 10/10 |
| B | 11 | Trace collector (passive data capture) | 11/11 |
| C | 8 | Safety guardrails | 8/8 |
| D | 7 | LLM judge evaluator | 7/7 |
| E | 9 | Pareto frontier | 9/9 |
| F | 11 | Reflector + candidate store | 11/11 |
| G | 11 | Optimization engine + trigger | 11/11 |
| H | 9 | Tools + integration tests | 9/9 |
| **Total** | **76** | | **76/76** |

## Dependencies Between Phases

```
A (foundation)
├── B (traces) ← depends on A (models, config)
├── C (guardrails) ← depends on A (models, config)
├── D (evaluator) ← depends on A (models)
├── E (pareto) ← depends on A (models)
└── F (reflector + store) ← depends on A (models)
    └── G (engine) ← depends on B, C, D, E, F
        └── H (tools + integration) ← depends on G
```

Phases B through F can be developed in parallel after A completes.
Phase G requires all of B-F.
Phase H is the final integration layer.
