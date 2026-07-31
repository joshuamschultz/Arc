# Coverage Analysis: S001 Phase 1 Core Components

**Generated**: 2026-02-14
**Test Suite**: 141 tests (141 passed, 0 failed)
**Runtime**: 2.33s

---

## 1. Coverage Summary

| Metric | Current | Target | Status |
|--------|---------|--------|--------|
| Line Coverage | 95.97% | >= 80% | PASS |
| Branch Coverage | 85.58% | >= 75% | PASS |
| Overall (combined) | 94.53% | >= 80% | PASS |
| Missing Lines | 26 | - | - |
| Partial Branches | 15 | - | - |

---

## 2. Per-Component Coverage vs PRD Targets

| Component | Coverage | Target | Status | Missing Lines | Partial Branches |
|-----------|----------|--------|--------|---------------|-----------------|
| errors.py | 100.0% | N/A | PASS | 0 | 0 |
| identity.py | 97.9% | >= 90% | PASS (+7.9) | 1 | 1 |
| module_bus.py | 97.9% | >= 90% | PASS (+7.9) | 2 | 0 |
| config.py | 96.6% | >= 90% | PASS (+6.6) | 2 | 2 |
| tool_registry.py | 96.1% | >= 85% | PASS (+11.1) | 3 | 1 |
| telemetry.py | 94.6% | >= 80% | PASS (+14.6) | 2 | 1 |
| context_manager.py | 93.1% | >= 85% | PASS (+8.1) | 3 | 6 |
| agent.py | 86.5% | >= 80% | PASS (+6.5) | 13 | 4 |

All 7 components with PRD targets meet or exceed their thresholds.

---

## 3. Uncovered Code Analysis

### 3.1 agent.py (13 missing lines, 4 partial branches)

**Missing lines 38-40** (`_load_model` function body):
```python
def _load_model(model_id: str) -> LLMProvider:
    _logger.info("Loading model: %s", model_id)           # L38
    provider, _, model_name = model_id.partition("/")      # L39
    return arcllm_load_model(provider, model_name or None) # L40
```
- **Impact**: Medium. This function is exercised via integration tests through `run()`, but the unit test mocks it out. The function is a thin wrapper around `arcllm.load_model()`.
- **Requirement**: REQ-AGT-02 (Load LLM model via arcllm from config)

**Missing lines 53-54** (`_run_loop` function body):
```python
async def _run_loop(...) -> LoopResult:
    _logger.info("Running agent loop for task: %s", task[:80])  # L53
    return await arcrun_run(...)                                  # L54
```
- **Impact**: Low. Another thin delegation wrapper. Tested indirectly via integration tests.
- **Requirement**: REQ-AGT-01 (Prepare inputs for arcrun.run())

**Missing lines 94-95** (bridge RuntimeError fallback):
```python
except RuntimeError:
    _logger.warning(                                   # L94
        "No running event loop for bridge event: %s",  # L95
        event.type,
    )
```
- **Impact**: Low. Defensive fallback for when bridge is called outside an async context. Unlikely in production.
- **Requirement**: REQ-AGT-03 (Bridge on_event to Module Bus)

**Missing lines 210-211** (defensive None guard in `run()`):
```python
if telemetry is None or tool_registry is None or context is None or bus is None:
    msg = "Components not initialized. Call startup() first."  # L210
    raise RuntimeError(msg)                                      # L211
```
- **Impact**: Low. This is a defensive type-narrowing guard for mypy. The `self._started` check on L200 already prevents reaching this code in normal usage.

**Missing line 254** (shutdown defensive guard):
```python
if bus is None or tool_registry is None:
    return  # L254
```
- **Impact**: Low. Same defensive type-narrowing pattern.

**Missing lines 274, 281-282** (`_create_vault_resolver` internals):
```python
if not backend_ref:
    return None                     # L274

module = importlib.import_module(module_path)  # L281
backend_cls = getattr(module, class_name)      # L282
```
- **Impact**: Medium. Vault resolver creation via dynamic import. Line 274 is the guard after the vault backend is already confirmed non-empty (double guard). Lines 281-282 are the actual dynamic import path.
- **Requirement**: REQ-IDN-07 (VaultResolver for secret resolution)

**Partial branch 88->exit** (bridge: unmapped event type skipped):
- **Impact**: Low. Tests cover mapped events; the "else" path for unknown event types simply does nothing.

### 3.2 config.py (2 missing lines, 2 partial branches)

**Missing lines 175, 177** (env override edge case):
```python
if part not in target:
    target[part] = {}      # L175 - create intermediate dict
if not isinstance(target[part], dict):
    target[part] = {}      # L177 - overwrite non-dict intermediate
```
- **Impact**: Low. These handle edge cases in env var override nesting where intermediate TOML keys either don't exist or are non-dict values being overwritten by nested env vars.
- **Requirement**: REQ-CFG-03 (Environment variable overrides)

### 3.3 context_manager.py (3 missing lines, 6 partial branches)

**Missing line 98** (empty messages guard in `prune_observations`):
```python
if not messages:
    return messages  # L98
```
- **Impact**: Low. Defensive guard for empty input. `transform_context` already checks this before calling `prune_observations`.

**Missing line 143** (empty messages guard in `transform_context`):
```python
if not messages:
    return messages  # L143
```
- **Impact**: Low. Defensive guard, tested indirectly (the test passes empty messages that result in 0 tokens which is below threshold).

**Missing line 198** (non-string content token estimation in `_emergency_truncate`):
```python
else:
    msg_tokens = 0  # L198
```
- **Impact**: Medium. This handles messages where `content` is not a string (e.g., structured tool call content, multimodal content). Currently no test sends non-string content through emergency truncation.
- **Requirement**: REQ-CTX-04 (Observation masking), REQ-CTX-06 (Emergency threshold)

**Partial branches**: Several relate to loop exit conditions (`for` loops completing without `break`) and the `isinstance` check short-circuiting. Low impact.

### 3.4 identity.py (1 missing line, 1 partial branch)

**Missing line 74** (`save_keys` without signing key):
```python
if self._signing_key is None:
    raise IdentityError(...)  # L74
```
- **Impact**: Low. The `IdentityError` is raised when attempting to save a verify-only identity. This is a guard that's unlikely in normal flow since `from_config` always generates or loads a full keypair.
- **Requirement**: REQ-IDN-03 (Store keys file-based)

### 3.5 module_bus.py (2 missing lines, 0 partial branches)

**Missing lines 185-186** (module shutdown failure logging):
```python
except Exception:
    _logger.exception("Module %s failed to shut down", module.name)  # L185-186
```
- **Impact**: Medium. Module shutdown failure error isolation. Tests cover startup failure isolation but not shutdown failure.
- **Requirement**: REQ-BUS-08 (Module lifecycle with reverse-order shutdown)

### 3.6 telemetry.py (2 missing lines, 1 partial branch)

**Missing lines 77-78** (`tool_span` with enabled telemetry):
```python
with self._tracer.start_as_current_span(
    "arcagent.tool",  # L77-78
```
- **Impact**: Medium. The `tool_span` context manager's enabled path (creating a real OTel span) is not tested. `session_span` and `turn_span` enabled paths are tested, but `tool_span` only tests the disabled/noop path.
- **Requirement**: REQ-TEL-01 (Create OTel spans: arcagent.tool)

### 3.7 tool_registry.py (3 missing lines, 1 partial branch)

**Missing line 53** (`_echo_tool` function):
```python
def _echo_tool(text: str = "") -> str:
    return f"echo: {text}"  # L53
```
- **Impact**: None. Built-in test utility function. Not part of production logic.

**Missing line 112** (native tool async wrapper return):
```python
return _fn(**kwargs)  # L112
```
- **Impact**: Medium. The async wrapper inside `register_native_tools` is created but never actually called through `execute()` in tests. Tests register native tools and convert to arcrun tools, but don't invoke the wrapped function through the full execution pipeline in unit tests (covered in integration tests).
- **Requirement**: REQ-TRG-01 (Register tools from native transport)

**Missing line 168** (wrapped_execute `args is None` fallback):
```python
if args is None:
    args = kwargs  # L168
```
- **Impact**: Low. Defensive guard allowing keyword-argument invocation of the wrapped execute function.

---

## 4. Critical Gaps (Prioritized by Business Impact)

### P0 - None

No critical coverage gaps. All security paths (identity signing/verification, tool policy enforcement, audit event emission) are covered.

### P1 - High Impact (Should Fix)

| # | Gap | File | Lines | Requirement | Why It Matters |
|---|-----|------|-------|-------------|----------------|
| 1 | Module shutdown failure isolation | module_bus.py | 185-186 | REQ-BUS-08 | Shutdown errors must be isolated to prevent cascading failures. Startup failure isolation is tested; shutdown parity missing. |
| 2 | tool_span enabled path | telemetry.py | 77-78 | REQ-TEL-01 | Ensures OTel tool spans emit correctly with real tracer. Session and turn spans tested; tool span is the gap. |
| 3 | Non-string content in emergency truncation | context_manager.py | 198 | REQ-CTX-06 | Multimodal/structured content handling during emergency truncation. Edge case but affects robustness. |

### P2 - Medium Impact (Nice to Have)

| # | Gap | File | Lines | Requirement | Why It Matters |
|---|-----|------|-------|-------------|----------------|
| 4 | Vault resolver dynamic import | agent.py | 274, 281-282 | REQ-IDN-07 | Vault path is exercised in identity tests with mock; the agent-level dynamic import is not. |
| 5 | Native tool execute invocation | tool_registry.py | 112 | REQ-TRG-01 | Native tool async wrapper created but not invoked through unit test path. Covered by integration tests. |
| 6 | Config env override intermediate dict creation | config.py | 175, 177 | REQ-CFG-03 | Edge case: deeply nested env vars creating intermediate dicts that don't exist in TOML. |

### P3 - Low Impact (Defensive Code)

| # | Gap | File | Lines | Why Low |
|---|-----|------|-------|---------|
| 7 | _load_model / _run_loop wrappers | agent.py | 38-40, 53-54 | Thin delegation wrappers, tested via integration |
| 8 | Bridge RuntimeError fallback | agent.py | 94-95 | Defensive code for edge case (no event loop) |
| 9 | Mypy type-narrowing guards | agent.py | 210-211, 254 | Unreachable after `_started` check |
| 10 | Empty message guards | context_manager.py | 98, 143 | Redundant defensive guards |
| 11 | Save verify-only identity guard | identity.py | 74 | Unlikely flow (verify-only never saved) |
| 12 | Wrapped execute None args guard | tool_registry.py | 168 | Defensive kwargs fallback |
| 13 | _echo_tool utility | tool_registry.py | 53 | Test utility, not production code |

---

## 5. Coverage Improvement Plan

### Current State
- Line Coverage: 95.97% (620/646 lines)
- Branch Coverage: 85.58% (89/104 branches)
- All PRD component targets: PASS

### Phase 1: P1 Gaps (Est: 1.5 hours, +1.0% line coverage)

#### Task 1.1: Test module shutdown failure isolation (30 min)
**File**: `/Users/joshschultz/AI/arcagent/tests/unit/core/test_module_bus.py`
**Lines covered**: module_bus.py L185-186
**Expected increase**: +2 lines, +0.3%

```python
class TestModuleShutdownFailure:
    async def test_shutdown_failure_isolates(self) -> None:
        """Module shutdown error must not crash other modules."""
        config = _make_config()
        tel = _make_telemetry()
        bus = ModuleBus(config=config, telemetry=tel)

        class FailingModule:
            @property
            def name(self) -> str:
                return "failing"
            async def startup(self, bus: ModuleBus) -> None:
                pass
            async def shutdown(self) -> None:
                raise RuntimeError("shutdown boom")

        class HealthyModule:
            def __init__(self) -> None:
                self.shut_down = False
            @property
            def name(self) -> str:
                return "healthy"
            async def startup(self, bus: ModuleBus) -> None:
                pass
            async def shutdown(self) -> None:
                self.shut_down = True

        healthy = HealthyModule()
        bus.register_module(FailingModule())
        bus.register_module(healthy)
        await bus.startup()

        # Shutdown: healthy shuts down first (reverse order),
        # then failing module raises, but healthy already succeeded
        await bus.shutdown()
        assert healthy.shut_down
```

#### Task 1.2: Test tool_span enabled path (30 min)
**File**: `/Users/joshschultz/AI/arcagent/tests/unit/core/test_telemetry.py`
**Lines covered**: telemetry.py L77-78
**Expected increase**: +2 lines, +0.3%

```python
class TestToolSpanEnabled:
    async def test_tool_span_creates_real_span(self) -> None:
        """tool_span with enabled telemetry creates an OTel span."""
        config = TelemetryConfig(enabled=True)
        tel = AgentTelemetry(config=config, agent_did="did:arc:test:agent/abc123")

        async with tel.tool_span("test_tool", {"arg": "val"}) as span:
            assert span.is_recording()
            assert span is not _NOOP_SPAN
```

#### Task 1.3: Test non-string content in emergency truncation (30 min)
**File**: `/Users/joshschultz/AI/arcagent/tests/unit/core/test_context_manager.py`
**Lines covered**: context_manager.py L198
**Expected increase**: +1 line, +0.15%

```python
class TestEmergencyTruncateNonStringContent:
    def test_non_string_content_gets_zero_tokens(self) -> None:
        """Messages with non-string content (e.g., list) count as 0 tokens."""
        config = ContextConfig(max_tokens=100, emergency_threshold=0.5)
        cm = ContextManager(config=config, telemetry=mock_telemetry())

        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1"}]},
            {"role": "user", "content": "recent message"},
        ]
        result = cm._emergency_truncate(messages)
        # Non-string content message should be included (0 tokens)
        assert len(result) >= 1
```

### Phase 2: P2 Gaps (Est: 1.5 hours, +0.8% line coverage)

#### Task 2.1: Test vault resolver dynamic import (45 min)
**File**: `/Users/joshschultz/AI/arcagent/tests/unit/core/test_agent.py`
**Lines covered**: agent.py L274, 281-282
**Expected increase**: +3 lines, +0.5%

```python
class TestVaultResolverCreation:
    def test_vault_resolver_import_and_instantiate(self) -> None:
        """_create_vault_resolver dynamically imports backend class."""
        # Create mock module and class, test import path
        ...

    def test_vault_resolver_empty_backend_returns_none(self) -> None:
        """Double-guard: _create_vault_resolver with empty backend."""
        ...
```

#### Task 2.2: Test native tool execute invocation (30 min)
**File**: `/Users/joshschultz/AI/arcagent/tests/unit/core/test_tool_registry.py`
**Lines covered**: tool_registry.py L112
**Expected increase**: +1 line, +0.15%

#### Task 2.3: Test config env override intermediate dict creation (15 min)
**File**: `/Users/joshschultz/AI/arcagent/tests/unit/core/test_config.py`
**Lines covered**: config.py L175, 177
**Expected increase**: +2 lines, +0.3%

```python
class TestEnvOverrideEdgeCases:
    def test_env_creates_intermediate_dict(self, tmp_path, monkeypatch):
        """ARCAGENT_NEW_SECTION__KEY=val creates new section dict."""
        ...

    def test_env_overwrites_non_dict_intermediate(self, tmp_path, monkeypatch):
        """ARCAGENT_AGENT__ORG__NESTED=val overwrites string with dict."""
        ...
```

### Phase 3: P3 Gaps (Optional, +1.5% line coverage)

Not recommended for immediate action. These are defensive guards, thin wrappers, and type-narrowing code. The test ROI is low. They would add test maintenance burden without meaningfully reducing risk.

---

## 6. Success Criteria

| Criteria | Current | Target | Status |
|----------|---------|--------|--------|
| Overall line coverage >= 80% | 95.97% | 80% | PASS |
| Overall branch coverage >= 75% | 85.58% | 75% | PASS |
| config.py >= 90% | 96.6% | 90% | PASS |
| identity.py >= 90% | 97.9% | 90% | PASS |
| telemetry.py >= 80% | 94.6% | 80% | PASS |
| module_bus.py >= 90% | 97.9% | 90% | PASS |
| tool_registry.py >= 85% | 96.1% | 85% | PASS |
| context_manager.py >= 85% | 93.1% | 85% | PASS |
| agent.py >= 80% | 86.5% | 80% | PASS |
| errors.py | 100.0% | N/A | PASS |
| No critical (P0) gaps | 0 | 0 | PASS |
| All security paths covered | Yes | Yes | PASS |
| All 141 tests pass | Yes | Yes | PASS |

---

## 7. Summary

The S001 Phase 1 Core Components have **strong test coverage** that exceeds all PRD targets.

**Overall**: 95.97% line coverage, 85.58% branch coverage (targets: 80% / 75%).

**Key strengths**:
- errors.py at 100% coverage
- All security-critical paths (identity signing/verification, tool policy enforcement, audit event emission, veto semantics) are thoroughly tested
- Both unit (126 tests) and integration (15 tests) coverage
- All 7 components exceed their individual PRD targets by 6-15 percentage points

**Three P1 gaps worth closing** (est. 1.5 hours total):
1. Module shutdown failure isolation (module_bus.py L185-186) -- parity with startup failure tests
2. tool_span enabled path (telemetry.py L77-78) -- parity with session/turn span tests
3. Non-string content in emergency truncation (context_manager.py L198) -- multimodal robustness

**Verdict**: PASS. Coverage is healthy. The remaining gaps are low-risk defensive code and thin delegation wrappers. The P1 improvements are recommended for completeness but are not blocking.
