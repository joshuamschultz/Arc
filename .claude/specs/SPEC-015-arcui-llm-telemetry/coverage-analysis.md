# SPEC-015: ArcUI LLM Telemetry — Coverage Analysis

**Date:** 2026-03-01
**Packages:** ArcLLM, ArcUI, ArcAgent (bridge)

---

## 1. Coverage Summary

| Package | Lines | Missed | Coverage | Threshold | Status |
|---------|-------|--------|----------|-----------|--------|
| ArcLLM (SPEC-015 tests) | 1,978 | 851 | **57%** | >= 80% | **FAIL** |
| ArcUI | 502 | 75 | **85%** | >= 80% | **PASS** |
| ArcAgent (agent.py) | 349 | 28 | **89%** | >= 80% | **PASS** |

**Note:** ArcLLM 57% reflects the *full package* measured by the SPEC-015 test subset — many adapter files (0% coverage) are pre-existing code not related to SPEC-015. The SPEC-015-relevant files are analyzed separately below.

---

## 2. SPEC-015 Relevant File Coverage

These are the files directly created or modified by SPEC-015:

| File | Stmts | Miss | Cover | Threshold | Status |
|------|-------|------|-------|-----------|--------|
| **arcllm/trace_store.py** | 188 | 23 | **88%** | >= 90% | FLAG |
| **arcllm/modules/telemetry.py** | 221 | 40 | **82%** | >= 90% | FLAG |
| **arcllm/config_controller.py** | 52 | 2 | **96%** | >= 90% | PASS |
| **arcllm/modules/circuit_breaker.py** | 100 | 3 | **97%** | >= 90% | PASS |
| **arcllm/config.py** | 88 | 8 | **91%** | >= 90% | PASS |
| **arcllm/registry.py** | 152 | 26 | **83%** | >= 85% | FLAG |
| **arcllm/types.py** | 53 | 0 | **100%** | >= 90% | PASS |
| **arcui/aggregator.py** | 165 | 8 | **95%** | >= 90% | PASS |
| **arcui/auth.py** | 34 | 0 | **100%** | >= 90% | PASS |
| **arcui/event_buffer.py** | 32 | 0 | **100%** | >= 90% | PASS |
| **arcui/connection.py** | 31 | 4 | **87%** | >= 85% | PASS |
| **arcui/server.py** | 71 | 7 | **90%** | >= 85% | PASS |
| **arcui/routes/traces.py** | 21 | 1 | **95%** | >= 85% | PASS |
| **arcui/routes/export.py** | 29 | 2 | **93%** | >= 85% | PASS |
| **arcui/routes/cost_efficiency.py** | 11 | 1 | **91%** | >= 85% | PASS |
| **arcui/routes/config.py** | 28 | 5 | **82%** | >= 85% | FLAG |
| **arcui/routes/stats.py** | 23 | 4 | **83%** | >= 85% | FLAG |
| **arcui/routes/ws.py** | 54 | 43 | **20%** | >= 75% | **FAIL** |
| **arcagent/core/agent.py** | 349 | 28 | **89%** | >= 85% | PASS |

---

## 3. Critical Gaps Analysis

### P0 — SECURITY/INTEGRITY (Must Fix)

#### 3.1 WebSocket Auth Path — 0% Functional Coverage
**File:** `/Users/joshschultz/ai/arc/packages/arcui/src/arcui/routes/ws.py`
**Lines:** 20-85 (43 of 54 lines uncovered)
**Impact:** CRITICAL — WebSocket first-message auth is entirely untested. An attacker could bypass auth if the token validation logic has bugs.
**What's uncovered:**
- Token extraction from first message (lines 26-33)
- Auth timeout handling (line 30)
- Invalid token rejection with 4003 close code (lines 36-39)
- Auth success response (line 41)
- Event streaming loop (lines 46-53)
- Heartbeat mechanism (lines 55-62)
- Client receive loop (lines 64-70)
- Concurrent task management and cleanup (lines 72-85)

#### 3.2 Hash Chain Verification — Partial Gaps
**File:** `/Users/joshschultz/ai/arc/packages/arcllm/src/arcllm/trace_store.py`
**Lines:** 313, 316-317, 331, 334-335, 338-340, 346-350
**Impact:** HIGH — Hash chain is the audit trail integrity guarantee. Untested paths include:
- `verify_chain()` self-hash mismatch detection (lines 354-359)
- `verify_chain()` JSON decode failure path (line 335)
- `get()` single-record lookup error paths (lines 313, 316-317)
- Warm start with existing files and chain recovery (lines 188-189)

#### 3.3 File Rotation Tombstone Path
**File:** `/Users/joshschultz/ai/arc/packages/arcllm/src/arcllm/trace_store.py`
**Lines:** 201-209
**Impact:** HIGH — Date rotation writes a tombstone record linking chain across files. If this breaks, chain verification fails silently across file boundaries.

### P1 — BUSINESS LOGIC (Should Fix)

#### 3.4 Budget Enforcement — Warn Mode Paths
**File:** `/Users/joshschultz/ai/arc/packages/arcllm/src/arcllm/modules/telemetry.py`
**Lines:** 297-315, 351-359, 373, 388, 398
**Impact:** MEDIUM-HIGH — Budget warning paths (enforcement="warn"), daily limit checks, and alert threshold spans are not exercised. In production, "warn" mode is the default; "block" is tested but "warn" paths that add span events are not.

#### 3.5 Config Route Error Paths
**File:** `/Users/joshschultz/ai/arc/packages/arcui/src/arcui/routes/config.py`
**Lines:** 31, 36-37, 41-42
**Impact:** MEDIUM — PATCH config with no controller (404), invalid JSON body (400), and controller exception (400) are untested. These are operator-facing error paths.

#### 3.6 Stats Route Null Guard
**File:** `/Users/joshschultz/ai/arc/packages/arcui/src/arcui/routes/stats.py`
**Lines:** 14, 32-34
**Impact:** MEDIUM — Null aggregator guard (line 14) and budget endpoint with active telemetry modules (lines 32-34) are untested.

#### 3.7 Registry Edge Cases
**File:** `/Users/joshschultz/ai/arc/packages/arcllm/src/arcllm/registry.py`
**Lines:** 139, 145-147, 239-241, 249-261, 294-298, 302-304, 330, 337, 339, 341
**Impact:** MEDIUM — Provider discovery fallbacks, adapter instantiation error handling, and some registry query paths lack coverage.

### P2 — OPERATIONAL (Nice to Have)

#### 3.8 Connection Manager — Queue Full Drop
**File:** `/Users/joshschultz/ai/arc/packages/arcui/src/arcui/connection.py`
**Lines:** 44-45, 48-49
**Impact:** LOW — Queue-full oldest-message-drop path is defensive. Unlikely in normal operation but important for sustained high throughput.

#### 3.9 Aggregator Warm Start Edge
**File:** `/Users/joshschultz/ai/arc/packages/arcui/src/arcui/aggregator.py`
**Lines:** 289-292
**Impact:** LOW — `warm_start()` replay logic when `rec` is a dict (not a Pydantic model) is untested.

#### 3.10 Agent Reload & Vault Error Paths
**File:** `/Users/joshschultz/ai/arc/packages/arcagent/src/arcagent/core/agent.py`
**Lines:** 665-682, 793-794, 811-812
**Impact:** LOW — Hot-reload skill/extension re-discovery and vault resolver import failure paths.

---

## 4. Recommended Tests (Priority Order)

### Phase 1 — P0 Critical (Estimated: 3 hours)

1. **`tests/test_ws_auth.py`** (ArcUI) — WebSocket first-message auth
   - Test valid token auth succeeds with `auth_ok` response
   - Test invalid token closes with code 4003
   - Test auth timeout closes with code 4001
   - Test invalid JSON in first message
   - Test event streaming after auth
   - Test heartbeat sends ping
   - Test cleanup on disconnect

2. **`tests/test_trace_store.py`** additions (ArcLLM) — Chain integrity edge cases
   - Test `verify_chain()` detects tampered `record_hash` (self-hash mismatch)
   - Test `verify_chain()` handles corrupt JSON line
   - Test `get()` returns None for missing trace_id
   - Test `get()` skips corrupt lines
   - Test warm start resumes chain from existing file
   - Test date rotation writes tombstone and chain continues

### Phase 2 — P1 Business Logic (Estimated: 2 hours)

3. **`tests/test_telemetry.py`** additions (ArcLLM) — Budget warn mode
   - Test enforcement="warn" adds span event instead of raising
   - Test daily limit exceeded in warn mode
   - Test budget alert threshold fires at configured percentage
   - Test `_set_budget_otel` sets all expected span attributes

4. **`tests/test_routes.py`** additions (ArcUI) — Error paths
   - Test PATCH /api/config with no controller returns 404
   - Test PATCH /api/config with invalid JSON returns 400
   - Test PATCH /api/config with controller error returns 400
   - Test GET /api/stats with no aggregator returns 404
   - Test GET /api/budget with active telemetry modules

### Phase 3 — P2 Operational (Estimated: 1 hour)

5. **`tests/test_connection.py`** additions (ArcUI) — Queue overflow
   - Test broadcast drops oldest when queue is full
   - Test `QueueFull` defensive catch

6. **`tests/test_aggregator.py`** additions (ArcUI) — Warm start
   - Test `warm_start()` with dict records (no `model_dump`)

7. **`tests/test_registry.py`** additions (ArcLLM) — Discovery fallbacks
   - Test adapter instantiation failure handling
   - Test provider not found after discovery

---

## 5. Expected Coverage After Improvements

| File | Current | After Phase 1 | After Phase 2 | After All |
|------|---------|---------------|---------------|-----------|
| ws.py | 20% | ~85% | ~85% | ~90% |
| trace_store.py | 88% | ~95% | ~95% | ~95% |
| telemetry.py | 82% | ~82% | ~92% | ~92% |
| routes/config.py | 82% | ~82% | ~95% | ~95% |
| routes/stats.py | 83% | ~83% | ~95% | ~95% |
| registry.py | 83% | ~83% | ~83% | ~90% |
| **ArcUI overall** | **85%** | **~90%** | **~93%** | **~94%** |

---

## 6. JSON Summary

```json
{
  "arcllm_coverage": "57% (full package); 88% trace_store, 82% telemetry, 96% config_controller, 97% circuit_breaker",
  "arcui_coverage": "85%",
  "arcagent_coverage": "89%",
  "critical_gaps": [
    {
      "file": "arcui/routes/ws.py",
      "lines": "20-85",
      "description": "WebSocket first-message auth and event streaming entirely untested",
      "impact": "CRITICAL — auth bypass risk, no verification of token validation, timeout handling, or connection lifecycle"
    },
    {
      "file": "arcllm/trace_store.py",
      "lines": "188-189, 201-209, 313, 316-317, 331, 334-335, 338-340, 346-350, 354-359",
      "description": "Hash chain verification edge cases: self-hash mismatch detection, corrupt line handling, date rotation tombstones, warm start recovery",
      "impact": "HIGH — audit trail integrity cannot be verified if these paths have bugs"
    },
    {
      "file": "arcllm/modules/telemetry.py",
      "lines": "297-315, 351-359, 373, 388, 398",
      "description": "Budget warn-mode enforcement paths, daily limit checks, alert threshold span events",
      "impact": "MEDIUM-HIGH — warn mode is the production default; only block mode is tested"
    },
    {
      "file": "arcui/routes/config.py",
      "lines": "31, 36-37, 41-42",
      "description": "PATCH config error paths: missing controller, invalid JSON, controller exception",
      "impact": "MEDIUM — operator-facing error handling untested"
    },
    {
      "file": "arcui/routes/stats.py",
      "lines": "14, 32-34",
      "description": "Null aggregator guard and budget endpoint with telemetry modules",
      "impact": "MEDIUM — defensive guards untested"
    }
  ],
  "recommended_tests": [
    "P0: WebSocket auth test suite (valid/invalid/timeout/streaming/heartbeat/cleanup)",
    "P0: trace_store verify_chain tampered hash detection",
    "P0: trace_store verify_chain corrupt JSON handling",
    "P0: trace_store get() missing ID and corrupt line resilience",
    "P0: trace_store date rotation tombstone + cross-file chain continuity",
    "P0: trace_store warm start from existing file",
    "P1: telemetry budget enforcement='warn' adds span events",
    "P1: telemetry daily limit exceeded in warn mode",
    "P1: telemetry budget alert at threshold percentage",
    "P1: config route PATCH error paths (404, 400 invalid JSON, 400 controller error)",
    "P1: stats route null aggregator 404",
    "P1: budget route with active telemetry modules",
    "P2: connection manager queue-full drop behavior",
    "P2: aggregator warm_start with dict records"
  ],
  "passed": false
}
```

---

## 7. Verdict

**FAIL** — Two critical issues prevent a passing grade:

1. **WebSocket auth at 20% coverage** — This is a security-critical path (first-message token auth, role validation, connection lifecycle) with near-zero test coverage. For a federal/FedRAMP-targeted codebase, this is unacceptable.

2. **Hash chain verification gaps** — The `verify_chain()` tamper detection paths (self-hash mismatch, corrupt records) are untested. These are the paths that actually *catch* tampering — if they have bugs, the entire audit trail guarantee is hollow.

Both are SPEC-015 deliverables. Phase 1 tests (estimated 3 hours) would resolve all critical gaps and bring coverage to passing thresholds.
