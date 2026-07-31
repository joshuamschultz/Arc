# Coverage Analysis: Spec 012 -- Security Layer

**Generated**: 2026-02-11
**Test Run**: 72 tests, 72 passed, 0 failed (0.39s)

---

## Coverage Summary

| File | Stmts | Miss | Branch | BrPart | Line % | Branch % |
|------|-------|------|--------|--------|--------|----------|
| `src/arcllm/_pii.py` | 48 | 0 | 20 | 1 | **99%** | 95% |
| `src/arcllm/_signing.py` | 30 | 1 | 6 | 0 | **97%** | 100% |
| `src/arcllm/vault.py` | 66 | 5 | 20 | 0 | **92%** | 100% |
| `src/arcllm/modules/security.py` | 91 | 10 | 38 | 5 | **87%** | 87% |
| **TOTAL** | **235** | **16** | **84** | **6** | **92%** | **93%** |

**Verdict**: PASSED (92% line coverage exceeds 80% threshold; all files exceed 85%)

---

## Per-File Gap Analysis

### 1. `_pii.py` -- 99% line, 95% branch

**Missing**: Line 105->104 (partial branch)

This is the overlap-filtering loop at line 104-107. The partial branch `105->104` means the `for match in all_matches` loop's "empty iteration / loop exit" path after entering the loop body is not separately exercised. This is a loop control-flow artifact, not a meaningful gap.

**Priority**: Low. No action needed. The overlap logic IS tested (custom patterns alongside builtins, multiple SSNs). The missing partial branch is a coverage reporting artifact for loop exits.

---

### 2. `_signing.py` -- 97% line, 100% branch

**Missing**: Line 74

```python
# Line 74:
raise ArcLLMConfigError(
    "ECDSA signing is available but not yet fully implemented"
)
```

This is the path where `cryptography` IS installed and `ecdsa-p256` is requested. The existing test `test_ecdsa_without_cryptography` only tests the `ImportError` branch (line 68-72). When cryptography is actually importable, execution reaches line 74 -- the "available but not implemented" error.

**Priority**: Medium. This is a real untested path. If someone installs `cryptography` and requests `ecdsa-p256`, they should get a clear error. Covering it validates the "ECDSA stub" behavior.

**Recommended test**:
```python
class TestCreateSigner:
    def test_ecdsa_with_cryptography_installed(self):
        """When cryptography IS available, ECDSA raises 'not yet implemented'."""
        with patch.dict(os.environ, {"TEST_KEY": "key-data"}):
            # Only patch if cryptography isn't actually installed
            with patch.dict(
                sys.modules, {"cryptography": MagicMock()}
            ):
                with pytest.raises(ArcLLMConfigError, match="not yet fully implemented"):
                    create_signer("ecdsa-p256", "TEST_KEY")
```

---

### 3. `vault.py` -- 92% line, 100% branch

**Missing**: Lines 72-79

```python
# Lines 72-79 (inside from_config):
backend_class = getattr(module, class_name, None)
if backend_class is None:
    raise ArcLLMConfigError(
        f"Vault backend class '{class_name}' not found in '{module_path}'"
    )

backend = backend_class()
return cls(backend=backend, cache_ttl_seconds=cache_ttl_seconds)
```

Two distinct gaps:

**Gap A (line 72-76)**: The "class not found" branch. The module imports successfully, but `getattr(module, class_name)` returns `None`. No test covers `from_config("real.module:NonexistentClass", 300)`.

**Gap B (lines 78-79)**: The happy path of `from_config()`. No test instantiates a real backend via the factory. Both existing tests (`test_backend_not_installed_string`, `test_invalid_backend_config_no_colon`) cover error paths only.

**Priority**: High. `from_config()` is the production entry point for vault configuration. The happy path and class-not-found path are both untested. This is the primary integration point between TOML config and the vault system.

**Recommended tests**:
```python
class TestVaultFromConfig:
    def test_from_config_class_not_found(self):
        """Module exists but class name is wrong."""
        with pytest.raises(ArcLLMConfigError, match="not found"):
            VaultResolver.from_config("os.path:NonexistentBackend", 300)

    def test_from_config_happy_path(self):
        """Full factory path: module:Class -> VaultResolver with backend."""
        # Use a test module with a known class
        VaultResolver.from_config(
            "tests.test_vault:MockVaultBackend", 300
        )
```

---

### 4. `modules/security.py` -- 87% line, 87% branch

**Missing**: Lines 56, 107, 127, 130-144 and partial branches at 162->176

This file has the most gaps. Breaking them down:

#### Gap 1: Line 56 -- Custom PII detector `else` branch (Priority: Low)

```python
# Lines 54-58:
else:
    # Future: importlib-based custom detector loading
    self._pii_detector = RegexPiiDetector(
        custom_patterns=custom_patterns or None
    )
```

When `pii_detector` config is anything other than `"regex"`, the else branch fires. Currently it still creates a `RegexPiiDetector` (the "future" comment indicates this is a stub). The existing `TestCustomDetector` test manually swaps `module._pii_detector` instead of exercising this config path.

**Recommended test**:
```python
async def test_non_regex_detector_type_falls_back(self):
    """Unknown detector type still creates RegexPiiDetector (stub behavior)."""
    inner = _make_inner()
    module = SecurityModule(
        _base_config(pii_detector="custom-nlp"), inner
    )
    # Should still work -- falls back to regex
    messages = [Message(role="user", content="SSN 123-45-6789")]
    await module.invoke(messages)
    sent = inner.invoke.call_args[0][0]
    assert "[PII:SSN]" in sent[0].content
```

#### Gap 2: Line 107 -- Non-str, non-list message content (Priority: Low)

```python
# Lines 106-107:
else:
    result.append(msg)
```

This is the fallback when `msg.content` is neither `str` nor `list`. Given ArcLLM's type system (`content: str | list[ContentBlock]`), this branch is defensive code that cannot be reached under normal Pydantic validation. Low priority but still worth a defensive test.

**Recommended test**:
```python
async def test_message_with_non_standard_content_passes_through(self):
    """Defensive: if content is somehow neither str nor list, pass through."""
    inner = _make_inner()
    module = SecurityModule(_base_config(), inner)
    # Force a message with unexpected content type
    msg = Message(role="user", content="placeholder")
    object.__setattr__(msg, "content", 42)  # bypass pydantic
    await module.invoke([msg])
    sent = inner.invoke.call_args[0][0]
    assert sent[0].content == 42
```

#### Gap 3: Line 127 -- ToolResultBlock with non-string content (Priority: Medium)

```python
# Lines 126-127 (inside _redact_blocks):
else:
    result.append(block)
```

When a `ToolResultBlock` has `content` that is `list[ContentBlock]` rather than `str`, it passes through unredacted. This is a real scenario -- tool results can contain nested ContentBlocks. PII in nested blocks would NOT be redacted.

**Recommended test**:
```python
async def test_tool_result_with_block_content_passes_through(self):
    """ToolResultBlock with list[ContentBlock] content passes through."""
    inner = _make_inner()
    module = SecurityModule(_base_config(), inner)
    messages = [
        Message(
            role="tool",
            content=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content=[TextBlock(text="SSN 123-45-6789")],
                )
            ],
        )
    ]
    await module.invoke(messages)
    sent = inner.invoke.call_args[0][0]
    block = sent[0].content[0]
    # Currently passes through -- content is list, not str
    assert isinstance(block.content, list)
```

#### Gap 4: Lines 130-144 -- ToolUseBlock PII scanning (Priority: CRITICAL)

```python
# Lines 128-144:
elif isinstance(block, ToolUseBlock):
    import json
    args_str = json.dumps(block.arguments)
    redacted_str = self._redact_str(args_str)
    if redacted_str != args_str:
        redacted_args = json.loads(redacted_str)
        result.append(
            ToolUseBlock(
                id=block.id,
                name=block.name,
                arguments=redacted_args,
            )
        )
    else:
        result.append(block)
```

The ENTIRE ToolUseBlock PII scanning path is uncovered. This is security-critical code -- tool call arguments containing PII (SSNs, emails, credit cards) would be sent to the LLM unredacted if this path had a bug. Zero tests exercise it.

Two sub-paths:
- **Lines 130-142**: PII found in tool arguments -> redact and rebuild block
- **Lines 143-144**: No PII in tool arguments -> pass through

**Recommended tests**:
```python
async def test_redacts_pii_from_tool_use_block_arguments(self):
    """ToolUseBlock arguments containing PII should be redacted."""
    inner = _make_inner()
    module = SecurityModule(_base_config(), inner)
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="call_1",
                    name="lookup_user",
                    arguments={"ssn": "123-45-6789", "query": "find user"},
                )
            ],
        )
    ]
    await module.invoke(messages)
    sent = inner.invoke.call_args[0][0]
    block = sent[0].content[0]
    assert isinstance(block, ToolUseBlock)
    assert "123-45-6789" not in json.dumps(block.arguments)
    assert "[PII:SSN]" in block.arguments["ssn"]

async def test_tool_use_block_no_pii_passes_through(self):
    """ToolUseBlock without PII passes through unchanged."""
    inner = _make_inner()
    module = SecurityModule(_base_config(), inner)
    messages = [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="call_1",
                    name="get_weather",
                    arguments={"city": "Denver"},
                )
            ],
        )
    ]
    await module.invoke(messages)
    sent = inner.invoke.call_args[0][0]
    block = sent[0].content[0]
    assert block.arguments == {"city": "Denver"}
```

#### Gap 5: Lines 162->176 -- Response content as list[ContentBlock] (Priority: High)

```python
# Lines 162-176:
if isinstance(response.content, str):
    redacted = self._redact_str(response.content)
    if redacted != response.content:
        return LLMResponse(
            content=redacted,
            ...
        )

return response  # line 176
```

The partial branch `162->176` means: when `response.content` is NOT a string (i.e., it is `list[ContentBlock]`), `_redact_response` skips redaction entirely and returns the response as-is. If a provider returns structured content blocks containing PII, those blocks pass through unscanned.

**Recommended test**:
```python
async def test_response_with_content_blocks_skips_redaction(self):
    """Response with list[ContentBlock] content currently passes through."""
    response = LLMResponse(
        content=[TextBlock(text="SSN 123-45-6789")],
        tool_calls=[],
        usage=_USAGE,
        model="test-model",
        stop_reason="end_turn",
    )
    inner = _make_inner(response)
    module = SecurityModule(_base_config(), inner)
    messages = [Message(role="user", content="test")]

    result = await module.invoke(messages)

    # Currently: list content is NOT redacted (potential security gap)
    assert isinstance(result.content, list)
    assert result.content[0].text == "SSN 123-45-6789"
```

Also: when `response.content` IS a string but contains NO PII, the `if redacted != response.content` check falls through to `return response` at line 176. This path IS covered by the `test_no_redaction_on_none_content` test indirectly, but a direct test of "string content, no PII" would be clearer.

---

## Critical Gaps Summary (Prioritized by Business Impact)

| # | Gap | File:Lines | Priority | Why |
|---|-----|-----------|----------|-----|
| 1 | ToolUseBlock PII scanning entirely untested | security.py:130-144 | **CRITICAL** | Tool arguments with PII sent to LLM unscanned. Security feature with zero test coverage. |
| 2 | Response list[ContentBlock] PII not redacted | security.py:162->176 | **HIGH** | Provider responses with structured content blocks skip PII redaction. Security blind spot. |
| 3 | `VaultResolver.from_config()` happy path + class-not-found | vault.py:72-79 | **HIGH** | Production factory method entirely untested. Config integration gap. |
| 4 | ECDSA with cryptography installed | _signing.py:74 | **MEDIUM** | Stub error path untested. Low risk but easy to cover. |
| 5 | ToolResultBlock with list content passes through | security.py:127 | **MEDIUM** | Nested ContentBlock PII not scanned. Edge case but real scenario. |
| 6 | Custom detector type else branch | security.py:56 | **LOW** | Stub falls back to regex. Future-proofing, not current risk. |
| 7 | Non-str/non-list message content | security.py:107 | **LOW** | Defensive code. Cannot reach via normal Pydantic validation. |
| 8 | Overlap filter partial branch | _pii.py:105->104 | **LOW** | Coverage artifact. Logic is tested. |

---

## Improvement Plan

### Phase 1: Critical (MUST FIX) -- Expected +5% coverage on security.py

| Test | Covers | Effort |
|------|--------|--------|
| `test_redacts_pii_from_tool_use_block_arguments` | security.py:130-142 | 10 min |
| `test_tool_use_block_no_pii_passes_through` | security.py:143-144 | 5 min |

### Phase 2: High Impact (SHOULD FIX) -- Expected +3% coverage on security.py, +8% on vault.py

| Test | Covers | Effort |
|------|--------|--------|
| `test_response_with_content_blocks_skips_redaction` | security.py:162->176 branch | 10 min |
| `test_response_str_no_pii_passes_through` | security.py:176 direct | 5 min |
| `test_from_config_class_not_found` | vault.py:72-76 | 5 min |
| `test_from_config_happy_path` | vault.py:78-79 | 10 min |

### Phase 3: Medium Impact (NICE TO HAVE) -- Expected +1% on signing.py, +1% on security.py

| Test | Covers | Effort |
|------|--------|--------|
| `test_ecdsa_with_cryptography_installed` | _signing.py:74 | 5 min |
| `test_tool_result_with_block_content_passes_through` | security.py:127 | 5 min |
| `test_non_regex_detector_type_falls_back` | security.py:56 | 5 min |

### Total Effort: ~60 minutes
### Expected Result After All Phases:
- `_pii.py`: 99% (unchanged, already excellent)
- `_signing.py`: 100% (from 97%)
- `vault.py`: 100% (from 92%)
- `security.py`: 97%+ (from 87%)
- **Overall: 98%+ (from 92%)**

---

## Security-Specific Observations

1. **ToolUseBlock scanning (Gap 1)** is the most concerning. An agent could pass PII through tool arguments to a third-party LLM, and the "security module" would not catch it. The code exists but is entirely untested.

2. **Response content blocks (Gap 2)** reveal a potential design gap, not just a test gap. If a provider returns `list[ContentBlock]`, `_redact_response()` does not scan it. Consider whether this should be fixed in implementation (scan content blocks in responses) or documented as a known limitation.

3. **ToolResultBlock nested content (Gap 5)** has the same issue -- when `content` is `list[ContentBlock]`, the PII scanner skips it. A recursive scan would be more thorough.

---

## Success Criteria

- [ ] All Phase 1 tests pass (ToolUseBlock PII scanning)
- [ ] All Phase 2 tests pass (response blocks, vault factory)
- [ ] Line coverage >= 95% across all four files
- [ ] Branch coverage >= 90% across all four files
- [ ] No security-critical paths remain uncovered
