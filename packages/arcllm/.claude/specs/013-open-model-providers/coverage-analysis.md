# Coverage Analysis: 013 Open Model Providers

**Branch**: `feature/013-open-llms`
**Date**: 2026-02-11
**Test Suite**: 538 passed, 1 skipped (10.05s)

---

## Coverage Summary

| Metric | Current | Target | Status |
|--------|---------|--------|--------|
| Line Coverage | **94%** (1118/1179) | >=80% | PASS |
| Branch Coverage | **95%** (286/300) | >=75% | PASS |
| New File Coverage | **99%+** (see below) | >=90% | PASS |

**Overall Verdict: PASS** -- Both the 80% overall threshold and the 90% new-file threshold are met.

---

## Coverage by File (Spec 013 Scope)

### New Adapter Files (all at 100%)

| File | Stmts | Miss | Branch | BrPart | Cover |
|------|-------|------|--------|--------|-------|
| `adapters/deepseek.py` | 5 | 0 | 0 | 0 | **100%** |
| `adapters/fireworks.py` | 5 | 0 | 0 | 0 | **100%** |
| `adapters/groq.py` | 5 | 0 | 0 | 0 | **100%** |
| `adapters/huggingface.py` | 5 | 0 | 0 | 0 | **100%** |
| `adapters/huggingface_tgi.py` | 5 | 0 | 0 | 0 | **100%** |
| `adapters/mistral.py` | 18 | 0 | 4 | 0 | **100%** |
| `adapters/ollama.py` | 5 | 0 | 0 | 0 | **100%** |
| `adapters/together.py` | 5 | 0 | 0 | 0 | **100%** |
| `adapters/vllm.py` | 5 | 0 | 0 | 0 | **100%** |

### Modified Files

| File | Stmts | Miss | Branch | BrPart | Cover | Missing |
|------|-------|------|--------|--------|-------|---------|
| `__init__.py` | 14 | 0 | 2 | 0 | **100%** | -- |
| `config.py` | 88 | 0 | 8 | 0 | **100%** | -- |
| `adapters/base.py` | 57 | 3 | 10 | 0 | **96%** | L41, L85-86 |
| `adapters/openai.py` | 84 | 1 | 30 | 3 | **96%** | L96 |

### Pre-existing Files Outside Spec Scope (for context)

| File | Cover | Missing |
|------|-------|---------|
| `registry.py` | 86% | L83-86, L181-193, L222-224 |
| `modules/otel.py` | 59% | L44, L50-119 |
| `vault.py` | 92% | L72-79 |
| `modules/fallback.py` | 95% | L17-19 |
| `modules/security.py` | 96% | L123, L158 |
| `modules/retry.py` | 97% | L92 |
| `_signing.py` | 97% | L74 |
| `_pii.py` | 99% | (partial branch) |
| `adapters/anthropic.py` | 97% | L97 |

---

## Coverage Gaps Analysis (Spec 013 Scope Only)

### Gap 1: `base.py` L41 -- BaseAdapter.name fallback (Low)

```python
@property
def name(self) -> str:
    return self._config.provider.api_format  # L41 -- never hit
```

**Impact**: Low. Every concrete adapter overrides `name` with its own property, so the base class fallback is dead code in practice. No adapter ever calls `super().name`.

**Risk**: Minimal. If a new adapter forgot to override `name`, this fallback would still return a reasonable value (`api_format` from TOML).

**Recommendation**: Either (a) add a single test that instantiates a minimal subclass without overriding `name`, or (b) mark as acceptable dead code since all 11 adapters override it. Priority: P2.

### Gap 2: `base.py` L85-86 -- Retry-After non-numeric parsing (Medium)

```python
try:
    return float(value)
except ValueError:         # L85
    return None            # L86
```

**Impact**: Medium. This handles the edge case where a provider sends a non-numeric `Retry-After` header (e.g., an HTTP-date string). The happy path (numeric) and None path are both tested, but the `ValueError` branch is not.

**Risk**: Moderate. If a provider sends `Retry-After: Thu, 01 Dec 2025 16:00:00 GMT`, the system should gracefully fall back to exponential backoff rather than crashing.

**Recommendation**: Add one test with a non-numeric Retry-After header. Priority: P1.

### Gap 3: `openai.py` L96 -- Empty content block fallback (Low)

```python
if parts:
    return {"role": message.role, "content": parts}
return {"role": message.role, "content": ""}  # L96 -- never hit
```

**Impact**: Low. This handles the edge case where a message has a `content: list[ContentBlock]` with no `TextBlock` or `ImageBlock` entries (e.g., only `ToolUseBlock` content that was already extracted). In practice, `_format_message` is only called for non-tool messages, and those always have text or image blocks.

**Risk**: Minimal. Defensive code for an unlikely edge case.

**Recommendation**: Add one test with a message containing an empty content block list. Priority: P2.

---

## Coverage Gaps Analysis (Outside Spec 013 Scope -- For Context)

### Gap 4: `registry.py` L181-193 -- Vault resolver in load_model() (Medium)

The vault integration path within `load_model()` is not exercised by any test that goes through the full registry flow. The `vault.py` module itself is tested (92%), but the registry's vault wiring (lines 181-193) is uncovered.

**Impact**: Medium. Vault integration is enterprise functionality. The `test_vault.py` file tests the vault module directly, but the integration with `load_model()` is not validated.

### Gap 5: `modules/otel.py` L50-119 -- OpenTelemetry module (High, but pre-existing)

59% coverage. The core OTel span creation and attribute setting are not tested. This is a pre-existing gap from Step 11, not introduced by Spec 013.

### Gap 6: `registry.py` L222-224 -- SecurityModule wiring in load_model() (Low)

The `load_model(security={...})` path. SecurityModule itself is tested at 96%, but the registry wiring is uncovered.

---

## Improvement Plan

### Phase 1: P1 Gaps -- Should Fix (Est: 15 min)

These are low-effort, medium-impact improvements that close the remaining gaps in spec 013's modified files.

#### Task 1.1: Test non-numeric Retry-After header (base.py L85-86)

**File**: `tests/test_open_providers.py` or `tests/test_anthropic.py` (where BaseAdapter tests live)
**Expected Coverage Increase**: +2 lines in `base.py` (96% -> 100%)
**Effort**: 5 minutes

```python
class TestRetryAfterParsing:
    def test_non_numeric_retry_after_returns_none(self):
        """Non-numeric Retry-After (e.g., HTTP-date) should return None."""
        response = httpx.Response(
            status_code=429,
            headers={"retry-after": "Thu, 01 Dec 2025 16:00:00 GMT"},
        )
        result = BaseAdapter._parse_retry_after(response)
        assert result is None
```

### Phase 2: P2 Gaps -- Nice to Have (Est: 15 min)

#### Task 2.1: Test BaseAdapter.name fallback (base.py L41)

```python
class TestBaseAdapterNameFallback:
    def test_base_name_returns_api_format(self, monkeypatch):
        """BaseAdapter.name should return api_format when not overridden."""
        monkeypatch.setenv("ARCLLM_TEST_KEY", "test-key")
        config = _make_fake_config("test", api_key_env="ARCLLM_TEST_KEY")

        class BareAdapter(BaseAdapter):
            async def invoke(self, messages, tools=None, **kwargs):
                pass

        adapter = BareAdapter(config, "test-model")
        assert adapter.name == "openai-chat"  # from _make_fake_config
```

#### Task 2.2: Test empty content block fallback (openai.py L96)

```python
class TestEmptyContentBlocks:
    def test_format_message_empty_content_list(self, adapter):
        """Message with empty content list should produce empty string content."""
        msg = Message(role="user", content=[])
        formatted = adapter._format_message(msg)
        assert formatted == {"role": "user", "content": ""}
```

### Phase 3: Outside Scope -- Track for Later

| Gap | File | Est. Effort | When |
|-----|------|-------------|------|
| Vault wiring in registry | `registry.py` L181-193 | 30 min | Step 14 (Vault) |
| OTel module coverage | `otel.py` L50-119 | 1 hour | Step 11 follow-up |
| Security wiring in registry | `registry.py` L222-224 | 15 min | Step 12 follow-up |

---

## Test Quality Assessment

### What is well-tested:

1. **All 9 new provider TOML files** load and validate correctly (parametrized across all providers)
2. **api_key_required=false** behavior for local providers (ollama, vllm, huggingface_tgi)
3. **api_key_required=true** enforcement for cloud providers (6 providers)
4. **Conditional Authorization header** presence/absence based on key availability
5. **Adapter .name property** returns correct value for all 9 providers
6. **Full invoke cycle** with mocked httpx for all 9 providers (parametrized)
7. **Registry discovery** via `load_model()` for all 9 providers
8. **Mistral quirks** thoroughly tested: tool_choice translation (5 cases), stop reason mapping (5 cases including `model_length`), full invoke with tool_choice verification
9. **Backward compatibility** for existing anthropic/openai providers

### Test design strengths:

- Good use of parametrization -- 9 providers tested with shared test logic
- Proper fixture isolation with `clear_cache()` before/after each test
- Both positive and negative auth paths tested
- Mistral gets dedicated test file for its non-trivial quirk overrides
- Integration tests via `load_model()` validate the full stack

### What could be improved:

- No error-path tests for new adapters (e.g., HTTP 500 from a Groq/Together/etc endpoint). The OpenAI adapter's error handling is tested in `test_openai.py`, and since all thin aliases inherit it unchanged, this is acceptable but not redundant-tested.
- No test for lazy import via `from arcllm import GroqAdapter` (the `__getattr__` path). The `__init__.py` reports 100% but only because the Anthropic/OpenAI imports exercise it -- the new adapter names in `_LAZY_IMPORTS` are not individually verified.

---

## Verdict

| Check | Result |
|-------|--------|
| Overall line coverage >=80% | **PASS** (94%) |
| Overall branch coverage >=75% | **PASS** (95%) |
| New adapter files >=90% | **PASS** (100% on all 9 files) |
| Modified files >=90% | **PASS** (96%+ on base.py, openai.py, config.py, __init__.py) |
| Mistral quirks tested | **PASS** (12 dedicated tests) |
| api_key_required behavior tested | **PASS** (positive + negative for both local and cloud) |
| Registry integration tested | **PASS** (all 9 via load_model()) |
| Critical gaps | **None** |

**Final: PASS** -- Coverage is strong across all new code. The 3 uncovered lines in spec scope are all defensive edge cases (P1/P2), not critical paths.
