# SPEC-016: Multi-Agent UI Architecture — Coverage Analysis

**Date:** 2026-03-03
**Analysis:** Post-implementation coverage assessment

---

## 1. Coverage Summary

### arcui (SPEC-016 target package)

| Metric | Value | Threshold | Status |
|--------|-------|-----------|--------|
| Line coverage | 76% | >= 80% | FAIL |
| Branch coverage | ~58 partial branches | >= 75% | NEEDS REVIEW |
| Tests passing | 194/194 | 100% | PASS |

### arcagent/modules/ui_reporter

| Metric | Value | Threshold | Status |
|--------|-------|-----------|--------|
| Line coverage | 71% | >= 80% | FAIL |
| Branch coverage | 1 partial branch | >= 75% | MARGINAL |
| Tests passing | 29/29 | 100% | PASS |

### arccli (ui.py specifically)

| Metric | Value | Threshold | Status |
|--------|-------|-----------|--------|
| ui.py line coverage | 32% | >= 75% (UI) | FAIL |
| Tests passing | 93/93 | 100% | PASS |

---

## 2. Per-File Coverage — SPEC-016 Files Only

### New Files

| File | Line Cov | Branch | Missing Lines | Priority |
|------|----------|--------|---------------|----------|
| `arcui/registry.py` | 100% | 100% | None | -- |
| `arcui/subscription.py` | 78% | 87% | 62-64, 77-80, 83-84 | P1 |
| `arcui/transport_ws.py` | 45% | 0% | 91-157 (send/receive/close) | P0 |
| `arcui/routes/agent_ws.py` | 70% | 75% | 44-47, 57-59, 88-99, 126-142, 159-160 | P0 |
| `arcui/routes/agents.py` | 75% | 83% | 62-86 (control_agent body) | P1 |
| `arcagent/modules/ui_reporter/__init__.py` | 71% | 92% | 92-117, 121, 125-126, 170 | P1 |
| `arccli/ui.py` | 32% | 0% | 45-69 (ui_start command body) | P2 |

### Modified Files

| File | Line Cov | Branch | Missing Lines | Priority |
|------|----------|--------|---------------|----------|
| `arcui/server.py` | 82% | 78% | 56, 149-170, 199-221 (serve/attach_llm) | P2 |
| `arcui/auth.py` | 100% | 100% | None | -- |
| `arcui/event_buffer.py` | 92% | 91% | 79-80, 93 | P2 |
| `arcui/routes/ws.py` | 90% | 70% | 48-53, 60, 67-69, 88-89 | P2 |
| `arcui/routes/stats.py` | 48% | 19% | 17-78, 93-120 | P1 |
| `arcui/types.py` | 100% | 100% | None | -- |

---

## 3. Critical Gaps — Prioritized by Business Impact

### P0 — Critical (Must Fix)

#### GAP-1: `transport_ws.py` — send/receive/close paths (45% coverage)

**Business Impact:** This is the agent-side WebSocket transport. Agents use this to connect to the UI server. Untested send failures, receive parsing, and close cleanup mean agents could silently lose events or leak connections in production.

**Uncovered Code:**
- Lines 100-116: `send_event()` — connected path, exception handling, fallback to buffer
- Lines 118-129: `send_control()` — connected path, RuntimeError on disconnect
- Lines 131-147: `receive()` — message parsing, type dispatch (control, control_response, event)
- Lines 149-157: `close()` — WebSocket cleanup, buffer flush

**Recommended Tests:**
1. `test_send_event_when_connected_sends_json` — mock `_ws.send`, verify payload format
2. `test_send_event_exception_buffers_event` — mock `_ws.send` to raise, verify buffer
3. `test_send_control_when_disconnected_raises` — verify RuntimeError
4. `test_send_control_when_connected_sends_json` — mock `_ws.send`, verify payload
5. `test_receive_when_disconnected_raises` — verify RuntimeError
6. `test_receive_event_message` — mock `_ws.recv`, verify UIEvent returned
7. `test_receive_control_message` — mock `_ws.recv`, verify ControlMessage returned
8. `test_receive_control_response_message` — mock `_ws.recv`, verify ControlResponse
9. `test_close_cleans_up_websocket` — verify `_ws.close()` called, `_closed` set
10. `test_close_handles_ws_close_exception` — mock exception on close

**Effort:** 2-3 hours
**Expected Coverage Increase:** transport_ws.py 45% -> 95%+

#### GAP-2: `routes/agent_ws.py` — auth timeout, capacity rejection, heartbeat/receive loops (70% coverage)

**Business Impact:** This is the server-side agent connection endpoint. Untested auth timeout, capacity rejection, and the receive loop's control_response path mean the multi-agent system could break under load or misbehave on edge cases.

**Uncovered Code:**
- Lines 44-47: Auth timeout path (TimeoutError, JSONDecodeError)
- Lines 57-59: Server at capacity — `registry.is_full()` path
- Lines 88-99: `_heartbeat()` inner coroutine
- Lines 126-137: Control response correlation in `_receive()`
- Lines 141-142: JSON decode error in receive
- Lines 159-160: Pending control cleanup on disconnect

**Recommended Tests:**
1. `test_auth_timeout_closes_4001` — client sends nothing, wait for timeout
2. `test_auth_invalid_json_closes_4001` — send malformed JSON
3. `test_capacity_full_closes_4029` — fill registry to max, try to connect
4. `test_control_response_resolves_pending_future` — send control_response message, verify future resolves
5. `test_invalid_json_in_receive_logged` — send non-JSON text after auth
6. `test_pending_controls_cleaned_on_disconnect` — register pending future, disconnect, verify exception

**Effort:** 2-3 hours
**Expected Coverage Increase:** agent_ws.py 70% -> 90%+

### P1 — High (Should Fix)

#### GAP-3: `routes/stats.py` — all stat endpoints (48% coverage)

**Business Impact:** Stats endpoints are the primary way operators monitor agent fleet performance. Untested per-agent drill-down, timeseries, performance, and queue stats mean broken monitoring in production.

**Uncovered Code:**
- Lines 17-44: `_validated_window()` and `_get_aggregator_for_request()` helpers
- Lines 47-61: `get_stats()` — window validation, aggregator lookup
- Lines 64-78: `get_timeseries()` — same pattern
- Lines 93-96: `get_budget()` — telemetry module iteration
- Lines 99-113: `get_performance()` — per-agent drill-down
- Lines 116-120: `get_queue_stats()` — queue module stats

**Recommended Tests:**
1. `test_stats_returns_valid_json` — basic happy path
2. `test_stats_invalid_window_returns_400` — `?window=invalid`
3. `test_stats_per_agent_drill_down` — `?agent_id=a1`
4. `test_stats_per_agent_not_found_returns_404` — unknown agent_id
5. `test_timeseries_returns_valid_json` — basic happy path
6. `test_performance_returns_valid_json` — basic happy path
7. `test_circuit_breakers_returns_empty_list` — no breakers registered
8. `test_budget_returns_empty_list` — no telemetry modules
9. `test_queue_stats_returns_empty_list` — no queue modules

**Effort:** 2 hours
**Expected Coverage Increase:** stats.py 48% -> 90%+

#### GAP-4: `routes/agents.py` — control_agent full flow (75% coverage)

**Business Impact:** The control proxy (POST `/api/agents/{id}/control`) is the operator's mechanism to steer running agents. The happy path (send control, wait for response) and timeout path are both untested.

**Uncovered Code:**
- Lines 62-86: `control_agent()` body — JSON parse, ControlMessage creation, future creation, send to WS, timeout handling

**Recommended Tests:**
1. `test_control_agent_happy_path` — operator sends control, mock WS responds, verify result
2. `test_control_agent_timeout_returns_504` — mock WS that never responds
3. `test_control_agent_cleans_pending_on_completion` — verify future removed from pending_controls

**Effort:** 1.5 hours
**Expected Coverage Increase:** agents.py 75% -> 95%+

#### GAP-5: `arcagent/modules/ui_reporter/__init__.py` — startup subscription loop (71% coverage)

**Business Impact:** The `startup()` method subscribes to all bus events. If it fails, the agent produces no UI events at all.

**Uncovered Code:**
- Lines 92-117: `startup()` — enabled check, agent name extraction, bus subscription loops
- Line 121: `shutdown()` log message
- Lines 125-126: `_on_event()` — actual bus event handler invocation
- Line 170: Fallback return in `_classify_layer` for unknown event prefixes

**Recommended Tests:**
1. `test_startup_subscribes_to_llm_events` — verify bus.subscribe called with LLM event names
2. `test_startup_subscribes_to_agent_events` — verify bus.subscribe called with agent event names
3. `test_startup_disabled_skips_subscription` — enabled=False, verify no bus.subscribe calls
4. `test_shutdown_logs_message` — verify log output
5. `test_on_event_wraps_and_logs` — invoke handler, verify _wrap_event called
6. `test_classify_layer_unknown_prefix_returns_agent` — event "team:something" returns "agent"

**Effort:** 1.5 hours
**Expected Coverage Increase:** ui_reporter 71% -> 95%+

#### GAP-6: `subscription.py` — broadcast edge cases (78% coverage)

**Business Impact:** Subscription filtering is the bandwidth optimization for large fleets. Untested full-queue drop behavior means events could silently vanish or queues could block.

**Uncovered Code:**
- Lines 62-64: Team filter matching in `matches()`
- Lines 77-80: Full queue drop-oldest in `broadcast_filtered()`
- Lines 83-84: QueueFull exception handling in `broadcast_filtered()`

**Recommended Tests:**
1. `test_team_filter_matches` — subscribe with teams=["alpha"], verify filtering
2. `test_team_filter_rejects_non_matching` — event with different team rejected
3. `test_broadcast_drops_oldest_on_full_queue` — use Queue(maxsize=1), push 2 events
4. `test_broadcast_handles_queue_full_exception` — verify no exception propagates

**Effort:** 1 hour
**Expected Coverage Increase:** subscription.py 78% -> 98%+

### P2 — Medium (Nice to Have)

#### GAP-7: `arccli/ui.py` — ui_start command body (32% coverage)

**Business Impact:** CLI is a developer convenience. The `--help` paths are tested. The actual `ui_start()` function body (lines 45-69) launches uvicorn, which is hard to unit test and better covered by e2e tests.

**Recommended:** Add a test that invokes `ui_start` with `--help` (already done) and a mock-based test that verifies `create_app()` and `uvicorn.run()` are called with correct args.

**Effort:** 0.5 hours
**Expected Coverage Increase:** ui.py 32% -> 80%+

#### GAP-8: `server.py` — serve() and attach_llm() (82% coverage)

**Business Impact:** `serve()` is a convenience function that wraps `create_app()` + `uvicorn.run()`. `attach_llm()` walks the module stack. Both are integration-level concerns better tested in e2e.

**Uncovered Code:**
- Lines 149-170: `attach_llm()` module stack walker (ImportError path)
- Lines 199-221: `serve()` function body

**Effort:** 1 hour

#### GAP-9: `event_buffer.py` — UIEvent fallback to ConnectionManager (92% coverage)

**Uncovered Code:**
- Lines 79-80: UIEvent broadcast via ConnectionManager (no subscription manager)

**Effort:** 0.5 hours

---

## 4. Integration Test Assessment

| Test File | Scenarios | Coverage Quality |
|-----------|-----------|-----------------|
| `test_multi_agent_flow.py` | Event pipeline, 2-agent flow, aggregator ingest, per-agent aggregator | GOOD |
| `test_multi_agent_subscription.py` | Agent filter, layer filter, combined filter, all-subscribe, two-browser | GOOD |
| `test_control_flow.py` | Control correlation, unmatched response, timeout, disconnect cleanup | GOOD |

**Assessment:** Integration tests cover the happy paths well. The primary gaps are in error paths and edge cases within individual components.

---

## 5. Error Path Coverage Assessment

| Error Path | Covered? | File | Gap ID |
|------------|----------|------|--------|
| WebSocket auth timeout | NO | agent_ws.py | GAP-2 |
| WebSocket invalid JSON auth | NO | agent_ws.py | GAP-2 |
| Server at capacity (4029) | NO | agent_ws.py | GAP-2 |
| Agent WebSocket send failure | NO | transport_ws.py | GAP-1 |
| Agent WebSocket receive parse error | NO | transport_ws.py | GAP-1 |
| Agent WebSocket close error | NO | transport_ws.py | GAP-1 |
| Control proxy timeout (504) | NO | agents.py | GAP-4 |
| Invalid window parameter (400) | NO | stats.py | GAP-3 |
| Agent not found for stats (404) | NO | stats.py | GAP-3 |
| Queue full during broadcast | NO | subscription.py | GAP-6 |
| Browser invalid token (4003) | YES | ws.py | -- |
| Agent invalid token (4003) | YES | agent_ws.py | -- |
| Agent disconnect cleanup | YES | agent_ws.py | -- |
| Auth middleware rejection | YES | auth.py | -- |

**Error path coverage: 4/14 (29%)** — This is the most critical gap.

---

## 6. Improvement Plan

### Phase 1: Critical Gaps (P0) — Expected +15% overall coverage

| Gap | File | Current | Target | Effort |
|-----|------|---------|--------|--------|
| GAP-1 | transport_ws.py | 45% | 95% | 2-3h |
| GAP-2 | agent_ws.py | 70% | 90% | 2-3h |

### Phase 2: High-Impact Gaps (P1) — Expected +10% overall coverage

| Gap | File | Current | Target | Effort |
|-----|------|---------|--------|--------|
| GAP-3 | stats.py | 48% | 90% | 2h |
| GAP-4 | agents.py | 75% | 95% | 1.5h |
| GAP-5 | ui_reporter | 71% | 95% | 1.5h |
| GAP-6 | subscription.py | 78% | 98% | 1h |

### Phase 3: Medium-Impact Gaps (P2) — Expected +3% overall coverage

| Gap | File | Current | Target | Effort |
|-----|------|---------|--------|--------|
| GAP-7 | arccli/ui.py | 32% | 80% | 0.5h |
| GAP-8 | server.py | 82% | 92% | 1h |
| GAP-9 | event_buffer.py | 92% | 98% | 0.5h |

### Total Effort Estimate: 12-14 hours

### Expected Final Coverage (arcui):
- Line coverage: 76% -> 90%+ (passes 80% gate)
- Branch coverage: significantly improved (passes 75% gate)
- Error path coverage: 29% -> 85%+

---

## 7. Success Criteria

- [ ] arcui line coverage >= 80%
- [ ] arcui branch coverage >= 75%
- [ ] arcagent/ui_reporter line coverage >= 80%
- [ ] All error paths tested (auth timeout, capacity, send failure, parse error, control timeout)
- [ ] Zero regression in existing 194 + 29 + 93 = 316 tests
- [ ] `transport_ws.py` coverage >= 90% (critical client-side transport)
- [ ] `agent_ws.py` coverage >= 85% (critical server-side endpoint)
- [ ] `stats.py` coverage >= 85% (operator monitoring)
