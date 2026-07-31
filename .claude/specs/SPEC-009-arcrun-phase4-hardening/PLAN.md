# PLAN: ArcRun Phase 4 — Hardening

**Spec ID**: SPEC-009
**Status**: COMPLETE
**Date**: 2026-02-21

## Implementation Phases

### Phase A: Event Integrity (hash chain + immutability)

**Rationale**: Foundation for everything else. Adversarial tests depend on hash chain. Must be done first.

**Files modified**: `src/arcrun/events.py`, `src/arcrun/types.py`, `src/arcrun/__init__.py`

- [x] A1: Write failing tests for Event immutability (`frozen=True`, `MappingProxyType`)
- [x] A2: Write failing tests for hash chain computation
- [x] A3: Write failing tests for `verify_chain()`
- [x] A4: Write failing tests for thread-safe EventBus
- [x] A5: Implement Event immutability (frozen=True, MappingProxyType, __post_init__ auto-convert)
- [x] A6: Implement hash chain in EventBus (GENESIS_PREV_HASH, _canonical_bytes, _compute_event_hash, threading.Lock)
- [x] A7: Implement `verify_chain()` and `ChainVerificationResult`
- [x] A8: Add `verify_integrity()` to LoopResult + update __init__.py exports
- [x] A9: Fix existing test suite — no breakage (auto-convert in __post_init__ handled compatibility)
- [x] A10: Full test suite green (183/183), ruff clean, format clean

**Phase A completion**: 10/10

---

### Phase B: Container Sandbox

**Rationale**: Second priority. New file, no changes to existing code (except `__init__.py` exports).

**Files created**: `src/arcrun/builtins/contained_execute.py`
**Files modified**: `src/arcrun/__init__.py`, `src/arcrun/builtins/__init__.py`, `pyproject.toml`

- [x] B1: Write failing tests for error hierarchy, lazy import, socket detection (16 tests)
- [x] B2: Write failing tests for container execution with mocked Docker SDK
- [x] B3: Implement `contained_execute.py` (error hierarchy, socket detection, tar injection, factory)
- [x] B4: Update `builtins/__init__.py` (comment noting optional import path)
- [x] B5: Update `pyproject.toml` with `[project.optional-dependencies] container = ["docker>=7.0"]`
- [x] B6: Full test suite green (199/199), ruff clean, format clean

**Phase B completion**: 6/6

---

### Phase C: Adversarial Test Suite

**Rationale**: Depends on Phase A (hash chain for event tampering tests) and Phase B (container for resource exhaustion tests).

**Files created**: `tests/security/__init__.py`, `tests/security/conftest.py`, 8 test files

- [x] C1: Create `tests/security/conftest.py` with shared fixtures
  - MockModel fixture (predetermined responses)
  - Event chain builder fixture
  - Standard tool set fixture
  - Sandbox config fixtures (permissive and restrictive)

- [x] C2: `test_prompt_injection.py` (3 tests)
  - System prompt extraction via task text
  - Tool call injection via task text
  - Instruction override in spawned child context

- [x] C3: `test_path_traversal.py` (4 tests)
  - `../../etc/passwd` in code execution params
  - Symlink escape from tmpdir
  - Null byte injection in file paths

- [x] C4: `test_resource_exhaustion.py` (5 tests)
  - Fork bomb: `while True: os.fork()`
  - Memory bomb: `"A" * 10**10`
  - Infinite loop with no output
  - Disk fill via `/tmp` write loop

- [x] C5: `test_event_tampering.py` (9 tests)
  - Modify event data after emit (blocked by frozen)
  - Insert fabricated event into chain
  - Delete event from chain
  - Reorder events in chain
  - Replay events from different run_id

- [x] C6: `test_tool_injection.py` (4 tests)
  - SQL injection via tool params
  - Command injection in execute args
  - Oversized parameter payload
  - Unicode homoglyph in tool name

- [x] C7: `test_spawn_depth_bomb.py` (3 tests)
  - Recursive spawn to max_depth
  - Parallel spawn flood (many concurrent spawns)
  - Spawn with manipulated depth field

- [x] C8: `test_steering_injection.py` (3 tests)
  - Inject instructions via tool result content
  - Manipulate system prompt through child spawn
  - Context poisoning via crafted tool output

- [x] C9: `test_timing_attacks.py` (5 tests)
  - 10+ parallel spawns via `asyncio.gather`
  - Nested parallel spawns (3 parents, 2 children each)
  - Spawn + cancel race condition
  - Spawn + steer race condition
  - Verify: unique run_ids, no event interleaving, no deadlocks

- [x] C10: Run security test suite — 36/36 pass
  - `pytest tests/security/ -v`
  - All tests discoverable and passing

**Phase C completion**: 10/10

---

### Phase D: NIST Documentation

**Rationale**: Can be written in parallel with Phase C but logically follows all implementation.

**Files created**: `docs/security/nist-800-53-mapping.md`, `docs/security/threat-model.md`, `docs/security/adversarial-tests.md`

- [x] D1: Create `docs/security/nist-800-53-mapping.md`
  - 38 controls across 8 families (AC, AU, CM, IA, SC, SI, SA, CA)
  - Each entry: control ID, title, status, arcrun feature, code reference, test evidence
  - Phase 4 additions highlighted (AU-9, AU-10, SC-4, SC-7, SC-39, AC-25, SA-8, CA-8)
  - Priority gaps for ATO noted

- [x] D2: Create `docs/security/threat-model.md`
  - OWASP LLM Top 10 (2025) mapped to arcrun mitigations
  - OWASP Agentic Top 10 (2026) mapped to arcrun mitigations
  - Attack surface analysis for each component

- [x] D3: Create `docs/security/adversarial-tests.md`
  - 8 test categories with descriptions
  - OWASP coverage matrix
  - Example payloads (sanitized)
  - Expected behaviors and failure modes

**Phase D completion**: 3/3

---

### Phase E: Integration & Verification

**Rationale**: Final gate check.

- [x] E1: LOC count verification
  - Pre-Phase-4 baseline: 1,369 lines (ruff-formatted)
  - Phase 4 net addition: 91 lines (events.py, types.py, __init__.py changes + contained_execute.py)
  - Total: 1,460 lines (ruff-formatted) — exceeds spec estimate of 1,400 by 60 lines
  - Well under project-level 3,500 LOC budget (CLAUDE.md)
  - Variance due to ruff formatter expanding compact expressions

- [x] E2: Full test suite — 235/235 pass, 97% coverage
  - `pytest -v --tb=short` — 235 passed
  - `pytest --cov=arcrun` — 97% line coverage (threshold: >= 80%)
  - Unit: 199, Security: 36

- [x] E3: Quality checks
  - `ruff check src/arcrun` — 0 errors on Phase 4 files (2 pre-existing in _messages.py)
  - `ruff format` — Phase 4 files formatted
  - mypy: deferred to Phase 5 (existing codebase not --strict ready)

- [x] E4: Phase 4 gate criteria verification
  - [x] Container sandbox available as opt-in (`make_contained_execute_tool`)
  - [x] Event checksums prevent tampering (verify_chain passes, 9 tamper tests)
  - [x] Adversarial tests pass (8 categories, 36 tests)
  - [x] Concurrent spawns don't deadlock (10 parallel, thread-safe EventBus)
  - [x] NIST mapping documented (38 controls across 8 families)
  - [x] Total LOC 1,460 (formatted) — 60 over spec estimate, well under 3,500 project limit

**Phase E completion**: 4/4

---

## Summary

| Phase | Tasks | Dependencies | Focus |
|-------|-------|-------------|-------|
| A | 10 | None | Event integrity (hash chain, immutability, verification) |
| B | 6 | None | Container sandbox (new factory, error types) |
| C | 10 | A, B | Adversarial tests (8 categories, 40+ test cases) |
| D | 3 | A, B | NIST documentation (38 controls, threat model) |
| E | 4 | A, B, C, D | Integration verification (LOC, coverage, gate) |

**Total tasks**: 33
**Parallel opportunities**: Phase A and B can run in parallel. Phase C and D can run in parallel after A+B.
**Estimated net new production LOC**: ~130-165
