# Coverage Analysis: SPEC-003 Module Decoupling

**Analyzed:** 2026-02-16
**Spec:** SPEC-003 Module Decoupling
**Coverage Target:** ≥80% line coverage on changed code

---

## Executive Summary

**Overall Coverage:** 91% line coverage across all SPEC-003 components

**Status:** **PASS** - Exceeds 80% threshold

**Critical Gap:** PolicyModule has only 23% coverage (89/115 lines uncovered). This is the primary integration point for the policy engine and requires comprehensive testing.

---

## Coverage by Component

| Component | Statements | Missing | Coverage | Status |
|-----------|-----------|---------|----------|--------|
| **Core** | | | | |
| `core/config.py` | 137 | 1 | 99% | Excellent |
| `core/errors.py` | 33 | 0 | 100% | Complete |
| `core/context_manager.py` | 125 | 5 | 96% | Excellent |
| `core/module_loader.py` | 100 | 1 | 99% | Excellent |
| **Memory Module** | | | | |
| `modules/memory/config.py` | 10 | 0 | 100% | Complete |
| `modules/memory/errors.py` | 11 | 2 | 82% | Good |
| `modules/memory/entity_extractor.py` | 146 | 0 | 100% | Complete |
| `modules/memory/hybrid_search.py` | 145 | 0 | 100% | Complete |
| `modules/memory/markdown_memory.py` | 305 | 9 | 97% | Excellent |
| **Policy Module** | | | | |
| `modules/policy/config.py` | 6 | 0 | 100% | Complete |
| `modules/policy/errors.py` | 7 | 1 | 86% | Good |
| `modules/policy/policy_engine.py` | 117 | 0 | 100% | Complete |
| `modules/policy/policy_module.py` | 115 | 89 | **23%** | **Critical Gap** |
| **Total** | **1268** | **108** | **91%** | **Pass** |

---

## Critical Gaps

### 1. PolicyModule Integration (23% coverage)

**Impact:** HIGH - This is the Module Bus subscriber that wires policy evaluation into the agent lifecycle.

**Uncovered Lines:**
- Lines 46-62: Constructor initialization and config validation
- Line 66: `name` property
- Lines 70-83: `startup()` - event handler registration
- Lines 92-95: `shutdown()` - background task cleanup
- Lines 104-126: `_get_eval_model()` - lazy model initialization with fallback logic
- Lines 130-138: `_on_assemble_prompt()` - policy.md injection
- Lines 142-158: `_on_post_respond()` - periodic evaluation trigger
- Lines 164-172: `_on_shutdown()` - session-end evaluation
- Lines 184-189: `_safe_evaluate()` - error handling wrapper
- Lines 193-222: `_spawn_background()` - backpressure, semaphore, timeout logic

**Why It Matters:**
- PolicyModule is the integration layer between policy_engine (100% covered) and the Module Bus
- Uncovered code includes critical paths: event registration, prompt injection, periodic evaluation, graceful shutdown
- Fallback behavior logic (`"skip"` vs `"error"`) is untested
- Background task management (semaphore, timeout, backpressure) is untested
- Module Bus integration is untested

---

### 2. Memory Module Errors (82% coverage)

**Impact:** LOW - Only error constructor instantiation is uncovered

**Uncovered Lines:**
- Line 30: `EntityExtractionError.__init__` (default parameter case)
- Line 42: `SearchError.__init__` (default parameter case)

**Why It Matters:**
- These are edge cases where errors are raised without custom parameters
- Low priority since error raising is tested indirectly in other tests

---

### 3. Policy Module Errors (86% coverage)

**Impact:** LOW - Only one error constructor uncovered

**Uncovered Line:**
- Line 25: `PolicyError.__init__` (default parameter case)

**Why It Matters:**
- Similar to memory errors, this is an edge case
- Low priority

---

## Missing Test Coverage

### PolicyModule Tests Not Found

No test file exists for `PolicyModule` (`test_policy_module.py`).

**What's tested:**
- `PolicyEngine` has comprehensive coverage (100%) with 28 test classes
- Tests cover parsing, serialization, curator logic, score thresholds, atomic writes, reflection, etc.

**What's NOT tested:**
- PolicyModule event handlers (`_on_assemble_prompt`, `_on_post_respond`, `_on_shutdown`)
- Module Bus integration (subscription, event context handling)
- Lazy eval model initialization and fallback behavior
- Background task spawning with semaphore and backpressure
- Graceful shutdown with task cancellation
- Session message accumulation across turns
- Periodic evaluation triggering (every N turns)

---

## Scheduler Module Status

**New Files:**
- `modules/scheduler/config.py` - SchedulerConfig model (25 LOC)

**Test Files Found:**
- `tests/unit/modules/scheduler/test_scheduler.py`
- `tests/integration/test_scheduler_integration.py`

**Status:** Scheduler module not included in SPEC-003 coverage run. Will need separate analysis if part of the decoupling effort.

---

## Edge Cases Analysis

### 1. Dict-to-Pydantic Validation in Constructors

**PolicyModule.__init__()** (Line 46):
```python
self._config = PolicyConfig(**(config or {}))
```

**Coverage Status:** **Uncovered**

**Test Cases Needed:**
- Constructor with `None` config (uses defaults)
- Constructor with valid dict config
- Constructor with invalid config (should raise ValidationError)
- Constructor with partial config (defaults fill in)

**Similar Pattern in Memory Module:**
```python
# Not visible in diff, but likely exists
self._config = MemoryConfig(**(config or {}))
```

**Status:** Need to verify if this pattern is tested in memory module tests.

---

### 2. Dynamic Prompt Assembly Ordering

**PolicyModule._on_assemble_prompt()** (Lines 128-138):
- Injects `policy.md` into `sections["policy"]`
- Priority: 60 (documented in line 80)

**Coverage Status:** **Uncovered**

**Test Cases Needed:**
- Policy file exists → content injected
- Policy file empty → no injection
- Policy file missing → no injection
- Section ordering with other modules (integration test)
- Verify priority 60 relative to memory module (priority 50)

---

### 3. Fallback Behavior Configuration

**EvalConfig.fallback_behavior** (`"skip"` | `"error"`):

**Uncovered Scenarios:**
- `fallback_behavior="skip"` → model failure returns None, no exception (lines 113-117, 122-126)
- `fallback_behavior="error"` → model failure raises exception (lines 113-115, 122-123, 187-188)

**Test Cases Needed:**
- Model load fails with `fallback_behavior="skip"` → returns None
- Model load fails with `fallback_behavior="error"` → raises RuntimeError
- Eval fails with `fallback_behavior="skip"` → logs and continues
- Eval fails with `fallback_behavior="error"` → propagates exception

---

### 4. Background Task Management

**_spawn_background()** (Lines 191-222):
- Semaphore limiting concurrent tasks
- Backpressure (drops tasks when queue full)
- Timeout enforcement (120s default)
- Done callback with telemetry on error

**Coverage Status:** **Uncovered**

**Test Cases Needed:**
- Normal case: task spawned, executes, completes
- Backpressure: queue full → task dropped, logged
- Timeout: task exceeds 120s → cancelled
- Task error: exception captured, audit event emitted
- Semaphore: max_concurrent enforced
- Shutdown: all tasks cancelled gracefully

---

## Improvement Plan

### Phase 1: Critical Gaps (P0) - PolicyModule Integration

**Priority:** Must implement before SPEC-003 is production-ready

**Estimated Effort:** 4-6 hours

**Tests to Add:**

#### T1.1: PolicyModule Lifecycle
```python
@pytest.mark.asyncio
async def test_startup_registers_handlers(module_bus, tmp_path):
    """PolicyModule.startup() registers 3 event handlers with correct priorities."""
    module = PolicyModule(workspace=tmp_path)
    ctx = ModuleContext(bus=module_bus)
    await module.startup(ctx)

    assert module_bus.has_subscriber("agent:post_respond", priority=110)
    assert module_bus.has_subscriber("agent:assemble_prompt", priority=60)
    assert module_bus.has_subscriber("agent:shutdown", priority=60)

@pytest.mark.asyncio
async def test_shutdown_cancels_background_tasks(tmp_path):
    """PolicyModule.shutdown() cancels all background tasks."""
    module = PolicyModule(workspace=tmp_path)
    # Spawn a long-running task
    module._spawn_background(asyncio.sleep(100))
    assert len(module._background_tasks) == 1

    await module.shutdown()
    assert len(module._background_tasks) == 0
```

**Expected Coverage Gain:** +15-20 lines

---

#### T1.2: Event Handlers
```python
@pytest.mark.asyncio
async def test_on_assemble_prompt_injects_policy(tmp_path):
    """_on_assemble_prompt injects policy.md content into sections."""
    module = PolicyModule(workspace=tmp_path)
    policy_path = tmp_path / "policy.md"
    policy_path.write_text("# Policy\n- [P01] Test rule\n")

    sections = {}
    ctx = EventContext(data={"sections": sections})
    await module._on_assemble_prompt(ctx)

    assert "policy" in sections
    assert "Test rule" in sections["policy"]

@pytest.mark.asyncio
async def test_on_assemble_prompt_missing_file_no_error(tmp_path):
    """Missing policy.md does not raise error."""
    module = PolicyModule(workspace=tmp_path)
    sections = {}
    ctx = EventContext(data={"sections": sections})
    await module._on_assemble_prompt(ctx)

    assert "policy" not in sections

@pytest.mark.asyncio
async def test_on_post_respond_periodic_evaluation(tmp_path, mock_model):
    """_on_post_respond triggers eval every eval_interval_turns."""
    config = {"eval_interval_turns": 3}
    module = PolicyModule(config=config, workspace=tmp_path)
    module._eval_model = mock_model

    messages = [{"role": "user", "content": "test"}]
    ctx = EventContext(data={"messages": messages, "session_id": "s1"})

    # Turn 1, 2 → no eval
    await module._on_post_respond(ctx)
    await module._on_post_respond(ctx)
    assert len(module._background_tasks) == 0

    # Turn 3 → eval triggered
    await module._on_post_respond(ctx)
    assert len(module._background_tasks) == 1

@pytest.mark.asyncio
async def test_on_shutdown_runs_final_eval(tmp_path, mock_model):
    """_on_shutdown runs policy eval once at session end."""
    module = PolicyModule(workspace=tmp_path)
    module._eval_model = mock_model
    module._session_messages = [{"role": "user", "content": "test"}]

    ctx = EventContext(data={"session_id": "s1"})
    await module._on_shutdown(ctx)

    # Verify eval was called (via mock)
    mock_model.assert_called_once()
```

**Expected Coverage Gain:** +30-35 lines

---

#### T1.3: Fallback Behavior
```python
@pytest.mark.asyncio
async def test_eval_model_fallback_skip_returns_none(tmp_path):
    """fallback_behavior=skip returns None on model load failure."""
    eval_cfg = EvalConfig(fallback_behavior="skip")
    module = PolicyModule(eval_config=eval_cfg, workspace=tmp_path)

    # No provider/model configured, no llm_config
    model = module._get_eval_model()
    assert model is None

@pytest.mark.asyncio
async def test_eval_model_fallback_error_raises(tmp_path):
    """fallback_behavior=error raises on model load failure."""
    eval_cfg = EvalConfig(fallback_behavior="error")
    module = PolicyModule(eval_config=eval_cfg, workspace=tmp_path)

    with pytest.raises(RuntimeError, match="No eval model config"):
        module._get_eval_model()

@pytest.mark.asyncio
async def test_safe_evaluate_skip_suppresses_errors(tmp_path):
    """fallback_behavior=skip suppresses eval errors."""
    eval_cfg = EvalConfig(fallback_behavior="skip")
    module = PolicyModule(eval_config=eval_cfg, workspace=tmp_path)

    # Mock engine.evaluate to raise
    module._engine.evaluate = AsyncMock(side_effect=RuntimeError("boom"))

    # Should not raise
    await module._safe_evaluate([], None)

@pytest.mark.asyncio
async def test_safe_evaluate_error_propagates(tmp_path):
    """fallback_behavior=error propagates eval errors."""
    eval_cfg = EvalConfig(fallback_behavior="error")
    module = PolicyModule(eval_config=eval_cfg, workspace=tmp_path)

    module._engine.evaluate = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await module._safe_evaluate([], None)
```

**Expected Coverage Gain:** +15-20 lines

---

#### T1.4: Background Task Management
```python
@pytest.mark.asyncio
async def test_spawn_background_with_backpressure(tmp_path):
    """Background task queue drops tasks when full."""
    module = PolicyModule(workspace=tmp_path)

    # Fill queue to limit (_MAX_BACKGROUND_QUEUE = 5)
    for _ in range(5):
        module._spawn_background(asyncio.sleep(1))
    assert len(module._background_tasks) == 5

    # Next spawn should drop
    with patch("arcagent.modules.policy.policy_module._logger") as mock_log:
        module._spawn_background(asyncio.sleep(1))
        mock_log.warning.assert_called_once()
        assert "queue full" in mock_log.warning.call_args[0][0].lower()

    assert len(module._background_tasks) == 5  # Not added

@pytest.mark.asyncio
async def test_spawn_background_with_timeout(tmp_path):
    """Background tasks timeout after _BACKGROUND_TASK_TIMEOUT seconds."""
    module = PolicyModule(workspace=tmp_path)

    async def long_task():
        await asyncio.sleep(200)  # Exceeds 120s timeout

    module._spawn_background(long_task())
    await asyncio.sleep(0.1)  # Let task start

    # Task should be cancelled due to timeout
    # (In real test, mock wait_for to simulate timeout)

@pytest.mark.asyncio
async def test_spawn_background_error_audit(tmp_path, mock_telemetry):
    """Background task errors emit audit events."""
    module = PolicyModule(workspace=tmp_path, telemetry=mock_telemetry)

    async def failing_task():
        raise ValueError("task error")

    module._spawn_background(failing_task())
    await asyncio.sleep(0.1)  # Let task fail

    # Verify audit event emitted
    mock_telemetry.audit_event.assert_called_with(
        "policy.background_error",
        {"error": "task error", "type": "ValueError"}
    )
```

**Expected Coverage Gain:** +20-25 lines

---

#### T1.5: Constructor and Configuration
```python
def test_policy_module_constructor_defaults(tmp_path):
    """PolicyModule constructor with no config uses defaults."""
    module = PolicyModule(workspace=tmp_path)

    assert module._config.eval_interval_turns == 10  # default
    assert module.name == "policy"
    assert module._turn_count == 0
    assert len(module._session_messages) == 0

def test_policy_module_constructor_with_config(tmp_path):
    """PolicyModule constructor accepts dict config."""
    config = {"eval_interval_turns": 5}
    module = PolicyModule(config=config, workspace=tmp_path)

    assert module._config.eval_interval_turns == 5

def test_policy_module_constructor_invalid_config(tmp_path):
    """PolicyModule constructor raises on invalid config."""
    config = {"eval_interval_turns": "not-an-int"}

    with pytest.raises(ValidationError):
        PolicyModule(config=config, workspace=tmp_path)
```

**Expected Coverage Gain:** +5-8 lines

---

### Phase 2: Edge Cases (P1) - Config Validation

**Priority:** Should implement for production confidence

**Estimated Effort:** 2-3 hours

**Tests to Add:**

#### T2.1: Config Validation Across Modules
```python
def test_memory_config_defaults():
    """MemoryConfig uses defaults when empty dict passed."""
    config = MemoryConfig()
    assert config.context_budget_tokens == 2000
    assert config.entity_extraction_enabled is True

def test_policy_config_defaults():
    """PolicyConfig uses defaults when empty dict passed."""
    config = PolicyConfig()
    assert config.eval_interval_turns == 10

def test_scheduler_config_defaults():
    """SchedulerConfig uses defaults when empty dict passed."""
    config = SchedulerConfig()
    assert config.enabled is False
    assert config.min_interval_seconds == 60

def test_policy_config_validation_error():
    """PolicyConfig raises ValidationError on invalid data."""
    with pytest.raises(ValidationError):
        PolicyConfig(eval_interval_turns="not-an-int")
```

**Expected Coverage Gain:** +5-10 lines (across config.py files)

---

### Phase 3: Nice to Have (P2) - Error Constructor Coverage

**Priority:** Low - edge cases, low impact

**Estimated Effort:** 1 hour

**Tests to Add:**

#### T3.1: Error Constructor Defaults
```python
def test_entity_extraction_error_defaults():
    """EntityExtractionError uses default code/message."""
    error = EntityExtractionError()
    assert error.code == "MEMORY_EXTRACTION"
    assert "extraction failed" in error.message.lower()
    assert error.component == "memory"

def test_search_error_defaults():
    """SearchError uses default code/message."""
    error = SearchError()
    assert error.code == "MEMORY_SEARCH"
    assert "search failed" in error.message.lower()

def test_policy_error_defaults():
    """PolicyError uses default code/message."""
    error = PolicyError()
    assert error.code == "POLICY"
    assert error.component == "policy"
```

**Expected Coverage Gain:** +3-4 lines

---

## Summary of Recommendations

### Must-Do (Phase 1)
1. Create `tests/unit/modules/policy/test_policy_module.py`
2. Implement 20-25 test cases covering PolicyModule integration
3. Focus on:
   - Module Bus event handler registration
   - Prompt injection (`_on_assemble_prompt`)
   - Periodic evaluation triggering (`_on_post_respond`)
   - Session-end evaluation (`_on_shutdown`)
   - Fallback behavior (`"skip"` vs `"error"`)
   - Background task management (backpressure, timeout, semaphore)
   - Graceful shutdown

**Impact:** 23% → ~85-90% coverage on PolicyModule

---

### Should-Do (Phase 2)
1. Add config validation tests for all three module configs
2. Test dict-to-Pydantic edge cases (None, empty dict, invalid data)

**Impact:** Minor gains (5-10 lines), but ensures robust config handling

---

### Nice-to-Have (Phase 3)
1. Add error constructor default parameter tests
2. Low priority since errors are tested indirectly

**Impact:** Marginal (3-4 lines), mainly for completeness

---

## Test Stub: PolicyModule Integration

**File:** `tests/unit/modules/policy/test_policy_module.py`

```python
"""Tests for PolicyModule — Module Bus subscriber and ACE integration.

Coverage target: 85%+ on PolicyModule (currently 23%).
Focuses on Module Bus integration, event handlers, and background task management.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from arcagent.core.config import EvalConfig
from arcagent.core.module_bus import EventContext, ModuleContext
from arcagent.modules.policy.config import PolicyConfig
from arcagent.modules.policy.policy_module import PolicyModule


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """Temporary workspace directory."""
    return tmp_path


@pytest.fixture
def mock_module_bus() -> MagicMock:
    """Mock Module Bus with subscribe tracking."""
    bus = MagicMock()
    bus.subscribers = {}

    def subscribe(event: str, handler, priority: int, module_name: str):
        bus.subscribers[event] = (handler, priority, module_name)

    bus.subscribe = subscribe
    bus.has_subscriber = lambda event, priority: event in bus.subscribers
    return bus


@pytest.fixture
def mock_telemetry() -> MagicMock:
    """Mock telemetry."""
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


@pytest.fixture
def mock_eval_model() -> AsyncMock:
    """Mock eval model that returns empty policy delta."""
    return AsyncMock(return_value='{"additions":[],"updates":[],"rewrites":[]}')


# ============================================================================
# T1.1: Lifecycle Tests
# ============================================================================

class TestPolicyModuleLifecycle:
    """Test startup, shutdown, and event registration."""

    @pytest.mark.asyncio
    async def test_startup_registers_handlers(
        self, tmp_workspace: Path, mock_module_bus: MagicMock
    ) -> None:
        """PolicyModule.startup() registers 3 event handlers with correct priorities."""
        module = PolicyModule(workspace=tmp_workspace)
        ctx = ModuleContext(bus=mock_module_bus)
        await module.startup(ctx)

        # Verify all 3 handlers registered
        assert "agent:post_respond" in mock_module_bus.subscribers
        assert "agent:assemble_prompt" in mock_module_bus.subscribers
        assert "agent:shutdown" in mock_module_bus.subscribers

        # Verify priorities
        _, post_respond_priority, _ = mock_module_bus.subscribers["agent:post_respond"]
        assert post_respond_priority == 110

        _, assemble_prompt_priority, _ = mock_module_bus.subscribers["agent:assemble_prompt"]
        assert assemble_prompt_priority == 60

    @pytest.mark.asyncio
    async def test_shutdown_cancels_background_tasks(
        self, tmp_workspace: Path
    ) -> None:
        """PolicyModule.shutdown() cancels all background tasks."""
        module = PolicyModule(workspace=tmp_workspace)

        # Spawn a long-running task
        async def long_task() -> None:
            await asyncio.sleep(100)

        module._spawn_background(long_task())
        assert len(module._background_tasks) == 1

        await module.shutdown()
        assert len(module._background_tasks) == 0

    def test_name_property(self, tmp_workspace: Path) -> None:
        """PolicyModule.name returns 'policy'."""
        module = PolicyModule(workspace=tmp_workspace)
        assert module.name == "policy"


# ============================================================================
# T1.2: Event Handler Tests
# ============================================================================

class TestEventHandlers:
    """Test _on_assemble_prompt, _on_post_respond, _on_shutdown."""

    @pytest.mark.asyncio
    async def test_on_assemble_prompt_injects_policy(
        self, tmp_workspace: Path
    ) -> None:
        """_on_assemble_prompt injects policy.md content into sections."""
        module = PolicyModule(workspace=tmp_workspace)
        policy_path = tmp_workspace / "policy.md"
        policy_path.write_text("# Policy\n\n- [P01] Test rule\n")

        sections: dict[str, str] = {}
        ctx = EventContext(data={"sections": sections})
        await module._on_assemble_prompt(ctx)

        assert "policy" in sections
        assert "Test rule" in sections["policy"]

    @pytest.mark.asyncio
    async def test_on_assemble_prompt_missing_file_no_error(
        self, tmp_workspace: Path
    ) -> None:
        """Missing policy.md does not raise error."""
        module = PolicyModule(workspace=tmp_workspace)
        sections: dict[str, str] = {}
        ctx = EventContext(data={"sections": sections})

        # Should not raise
        await module._on_assemble_prompt(ctx)
        assert "policy" not in sections

    @pytest.mark.asyncio
    async def test_on_assemble_prompt_empty_file_no_inject(
        self, tmp_workspace: Path
    ) -> None:
        """Empty policy.md is not injected."""
        module = PolicyModule(workspace=tmp_workspace)
        policy_path = tmp_workspace / "policy.md"
        policy_path.write_text("")

        sections: dict[str, str] = {}
        ctx = EventContext(data={"sections": sections})
        await module._on_assemble_prompt(ctx)

        assert "policy" not in sections

    @pytest.mark.asyncio
    async def test_on_post_respond_periodic_evaluation(
        self, tmp_workspace: Path, mock_eval_model: AsyncMock
    ) -> None:
        """_on_post_respond triggers eval every eval_interval_turns."""
        config = {"eval_interval_turns": 3}
        module = PolicyModule(config=config, workspace=tmp_workspace)
        module._eval_model = mock_eval_model

        messages = [{"role": "user", "content": "test"}]
        ctx = EventContext(data={"messages": messages, "session_id": "s1"})

        # Turn 1, 2 → no eval spawned
        await module._on_post_respond(ctx)
        await module._on_post_respond(ctx)
        assert len(module._background_tasks) == 0

        # Turn 3 → eval triggered in background
        await module._on_post_respond(ctx)
        assert len(module._background_tasks) == 1

    @pytest.mark.asyncio
    async def test_on_post_respond_no_model_does_nothing(
        self, tmp_workspace: Path
    ) -> None:
        """_on_post_respond does nothing if no eval model."""
        module = PolicyModule(workspace=tmp_workspace)
        # No eval model configured

        messages = [{"role": "user", "content": "test"}]
        ctx = EventContext(data={"messages": messages, "session_id": "s1"})

        # Should not spawn any tasks
        for _ in range(10):  # Trigger multiple times
            await module._on_post_respond(ctx)

        assert len(module._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_on_shutdown_runs_final_eval(
        self, tmp_workspace: Path, mock_eval_model: AsyncMock
    ) -> None:
        """_on_shutdown runs policy eval once at session end."""
        module = PolicyModule(workspace=tmp_workspace)
        module._eval_model = mock_eval_model
        module._session_messages = [{"role": "user", "content": "test"}]

        policy_path = tmp_workspace / "policy.md"
        policy_path.write_text("# Policy\n\n")

        ctx = EventContext(data={"session_id": "s1"})
        await module._on_shutdown(ctx)

        # Verify model was called
        mock_eval_model.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_shutdown_no_messages_no_eval(
        self, tmp_workspace: Path, mock_eval_model: AsyncMock
    ) -> None:
        """_on_shutdown does nothing if no session messages."""
        module = PolicyModule(workspace=tmp_workspace)
        module._eval_model = mock_eval_model
        # No session messages accumulated

        ctx = EventContext(data={"session_id": "s1"})
        await module._on_shutdown(ctx)

        # Should not call model
        mock_eval_model.assert_not_called()


# ============================================================================
# T1.3: Fallback Behavior Tests
# ============================================================================

class TestFallbackBehavior:
    """Test fallback_behavior='skip' vs 'error'."""

    def test_get_eval_model_skip_returns_none_no_config(
        self, tmp_workspace: Path
    ) -> None:
        """fallback_behavior=skip returns None on missing config."""
        eval_cfg = EvalConfig(fallback_behavior="skip")
        module = PolicyModule(eval_config=eval_cfg, workspace=tmp_workspace)

        # No provider/model configured, no llm_config
        model = module._get_eval_model()
        assert model is None

    def test_get_eval_model_error_raises_no_config(
        self, tmp_workspace: Path
    ) -> None:
        """fallback_behavior=error raises on missing config."""
        eval_cfg = EvalConfig(fallback_behavior="error")
        module = PolicyModule(eval_config=eval_cfg, workspace=tmp_workspace)

        with pytest.raises(RuntimeError, match="No eval model config"):
            module._get_eval_model()

    @patch("arcagent.modules.policy.policy_module.load_eval_model")
    def test_get_eval_model_skip_on_load_failure(
        self, mock_load: MagicMock, tmp_workspace: Path
    ) -> None:
        """fallback_behavior=skip returns None on model load failure."""
        mock_load.side_effect = RuntimeError("model load failed")

        eval_cfg = EvalConfig(
            provider="test", model="test", fallback_behavior="skip"
        )
        module = PolicyModule(eval_config=eval_cfg, workspace=tmp_workspace)

        model = module._get_eval_model()
        assert model is None

    @patch("arcagent.modules.policy.policy_module.load_eval_model")
    def test_get_eval_model_error_on_load_failure(
        self, mock_load: MagicMock, tmp_workspace: Path
    ) -> None:
        """fallback_behavior=error raises on model load failure."""
        mock_load.side_effect = RuntimeError("model load failed")

        eval_cfg = EvalConfig(
            provider="test", model="test", fallback_behavior="error"
        )
        module = PolicyModule(eval_config=eval_cfg, workspace=tmp_workspace)

        with pytest.raises(RuntimeError, match="model load failed"):
            module._get_eval_model()

    @pytest.mark.asyncio
    async def test_safe_evaluate_skip_suppresses_errors(
        self, tmp_workspace: Path
    ) -> None:
        """fallback_behavior=skip suppresses eval errors."""
        eval_cfg = EvalConfig(fallback_behavior="skip")
        module = PolicyModule(eval_config=eval_cfg, workspace=tmp_workspace)

        # Mock engine to raise
        module._engine.evaluate = AsyncMock(side_effect=RuntimeError("boom"))

        # Should not raise
        await module._safe_evaluate([], None)

    @pytest.mark.asyncio
    async def test_safe_evaluate_error_propagates(
        self, tmp_workspace: Path
    ) -> None:
        """fallback_behavior=error propagates eval errors."""
        eval_cfg = EvalConfig(fallback_behavior="error")
        module = PolicyModule(eval_config=eval_cfg, workspace=tmp_workspace)

        module._engine.evaluate = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await module._safe_evaluate([], None)


# ============================================================================
# T1.4: Background Task Management Tests
# ============================================================================

class TestBackgroundTaskManagement:
    """Test _spawn_background with semaphore, backpressure, timeout."""

    @pytest.mark.asyncio
    async def test_spawn_background_normal_execution(
        self, tmp_workspace: Path
    ) -> None:
        """Background task spawned and executes normally."""
        module = PolicyModule(workspace=tmp_workspace)

        executed = False

        async def task() -> None:
            nonlocal executed
            await asyncio.sleep(0.01)
            executed = True

        module._spawn_background(task())
        assert len(module._background_tasks) == 1

        # Wait for task completion
        await asyncio.sleep(0.1)
        assert executed
        assert len(module._background_tasks) == 0  # Auto-removed on done

    @pytest.mark.asyncio
    async def test_spawn_background_with_backpressure(
        self, tmp_workspace: Path
    ) -> None:
        """Background task queue drops tasks when full."""
        module = PolicyModule(workspace=tmp_workspace)

        # Fill queue to limit (_MAX_BACKGROUND_QUEUE = 5)
        for _ in range(5):
            module._spawn_background(asyncio.sleep(1))
        assert len(module._background_tasks) == 5

        # Next spawn should drop and log warning
        with patch("arcagent.modules.policy.policy_module._logger") as mock_log:
            module._spawn_background(asyncio.sleep(1))
            mock_log.warning.assert_called_once()
            assert "queue full" in mock_log.warning.call_args[0][0].lower()

        assert len(module._background_tasks) == 5  # Not added

    @pytest.mark.asyncio
    async def test_spawn_background_error_audit(
        self, tmp_workspace: Path, mock_telemetry: MagicMock
    ) -> None:
        """Background task errors emit audit events."""
        module = PolicyModule(workspace=tmp_workspace, telemetry=mock_telemetry)

        async def failing_task() -> None:
            raise ValueError("task error")

        module._spawn_background(failing_task())
        await asyncio.sleep(0.1)  # Let task fail

        # Verify audit event emitted
        mock_telemetry.audit_event.assert_called_with(
            "policy.background_error",
            {"error": "task error", "type": "ValueError"},
        )

    @pytest.mark.asyncio
    async def test_spawn_background_cancelled_no_audit(
        self, tmp_workspace: Path, mock_telemetry: MagicMock
    ) -> None:
        """Cancelled background tasks do not emit audit events."""
        module = PolicyModule(workspace=tmp_workspace, telemetry=mock_telemetry)

        async def long_task() -> None:
            await asyncio.sleep(10)

        module._spawn_background(long_task())
        await asyncio.sleep(0.01)

        # Cancel the task
        await module.shutdown()

        # Should not emit audit event
        mock_telemetry.audit_event.assert_not_called()


# ============================================================================
# T1.5: Constructor and Configuration Tests
# ============================================================================

class TestConstructorAndConfiguration:
    """Test PolicyModule constructor with various config scenarios."""

    def test_constructor_defaults(self, tmp_workspace: Path) -> None:
        """PolicyModule constructor with no config uses defaults."""
        module = PolicyModule(workspace=tmp_workspace)

        assert module._config.eval_interval_turns == 10  # PolicyConfig default
        assert module.name == "policy"
        assert module._turn_count == 0
        assert len(module._session_messages) == 0

    def test_constructor_with_config_dict(self, tmp_workspace: Path) -> None:
        """PolicyModule constructor accepts dict config."""
        config = {"eval_interval_turns": 5}
        module = PolicyModule(config=config, workspace=tmp_workspace)

        assert module._config.eval_interval_turns == 5

    def test_constructor_with_none_config(self, tmp_workspace: Path) -> None:
        """PolicyModule constructor handles None config."""
        module = PolicyModule(config=None, workspace=tmp_workspace)

        assert module._config.eval_interval_turns == 10  # Uses defaults

    def test_constructor_invalid_config_raises(self, tmp_workspace: Path) -> None:
        """PolicyModule constructor raises ValidationError on invalid config."""
        config = {"eval_interval_turns": "not-an-int"}

        with pytest.raises(ValidationError):
            PolicyModule(config=config, workspace=tmp_workspace)

    def test_constructor_with_eval_config(self, tmp_workspace: Path) -> None:
        """PolicyModule constructor accepts EvalConfig."""
        eval_cfg = EvalConfig(max_concurrent=5, fallback_behavior="error")
        module = PolicyModule(eval_config=eval_cfg, workspace=tmp_workspace)

        assert module._eval_config.max_concurrent == 5
        assert module._eval_config.fallback_behavior == "error"

    def test_constructor_workspace_resolved(self, tmp_workspace: Path) -> None:
        """PolicyModule resolves workspace to absolute path."""
        module = PolicyModule(workspace=tmp_workspace)
        assert module._workspace.is_absolute()
```

---

## Success Criteria

**PASS Criteria:**
- PolicyModule reaches ≥85% line coverage
- All Phase 1 tests passing
- Integration tests pass with Module Bus
- Fallback behavior fully tested (skip vs error)
- Background task management verified

**CURRENT STATUS:**
- Overall coverage: 91% (PASS)
- PolicyModule: 23% (FAIL - needs Phase 1 implementation)
- All other components: ≥82% (PASS)

**NEXT STEPS:**
1. Implement `test_policy_module.py` with Phase 1 tests (~25 test cases)
2. Run coverage again to verify ≥85% on PolicyModule
3. Optionally add Phase 2 tests for config validation edge cases
4. Document any remaining uncovered lines with justification

---

## Conclusion

SPEC-003 Module Decoupling has **excellent overall coverage (91%)**, exceeding the 80% threshold. However, **PolicyModule integration is critically under-tested (23%)**, representing the primary risk area.

**Recommendation:** Implement Phase 1 tests (4-6 hours effort) before merging SPEC-003 to production. This will bring PolicyModule coverage to ~85-90%, ensuring the Module Bus integration, event handlers, and background task management are thoroughly validated.

**Confidence Level:** HIGH - The coverage gap is well-defined, and the test stub provides a clear implementation path.
