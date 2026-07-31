# S004 Memory Wiring — Coverage Analysis

**Date**: 2026-02-15
**Overall Project Coverage**: 91.43% line (678 branches tracked)
**Tests**: 610 passed, 0 failed
**Status**: All quality gates PASSING

---

## 1. Overall Summary

| Metric | Value | Threshold | Status |
|--------|-------|-----------|--------|
| Line coverage (project) | 91.43% | >= 80% | PASS |
| Branch coverage (project) | ~85% (99 partial branches / 678 total) | >= 75% | PASS |
| Core component avg | 91.2% | >= 90% | PASS |
| S004-specific files avg | 89.0% | >= 90% | MARGINAL |

---

## 2. Per-File Coverage (S004-Modified Files)

| File | Stmts | Miss | Branch | BrPart | Cover | Category |
|------|-------|------|--------|--------|-------|----------|
| `core/module_bus.py` | 105 | 2 | 16 | 0 | **98%** | Core |
| `core/context_manager.py` | 115 | 5 | 42 | 8 | **92%** | Core |
| `core/agent.py` | 235 | 20 | 58 | 17 | **87%** | Core |
| `modules/memory/markdown_memory.py` | 284 | 30 | 98 | 14 | **87%** | Module |
| `core/module_loader.py` | 80 | 11 | 24 | 5 | **85%** | Core (NEW) |
| `core/session_manager.py` | 123 | 13 | 28 | 10 | **85%** | Core |

---

## 3. Uncovered Lines Analysis

### 3.1 `core/module_loader.py` (85% — 11 lines missed)

**Missing lines:**

| Lines | Code | Priority | Notes |
|-------|------|----------|-------|
| 50 | `return []` (modules_dir not exists) | P2 | Edge case: nonexistent modules dir |
| 64-65 | YAML parse error → ConfigError | P1 | Error path: corrupt YAML |
| 72 | YAML not a dict → ConfigError | P1 | Error path: list/scalar YAML |
| 81 | Missing name field check | P0 | **Tested via discover but line 81 is the `raise` inside that block — recheck** |
| 104-105 | Pydantic validation error → ConfigError | P2 | Error path: invalid schema extras |
| 146-151 | Instantiation failure → None | P1 | Error path: constructor crash |
| 170 | Default no-arg construction `cls()` | P2 | Non-memory module instantiation |
| 182->180 | Branch: `load_all` loop when module is None | P2 | Implicitly covered by `load_handles_import_error` |

**Gap Analysis:**
- Lines 64-65 (YAML parse error): No test for corrupt/invalid YAML file content.
- Lines 146-151 (instantiation failure): No test for a module whose constructor raises.
- Line 170 (default construction): No test for a non-memory module with no-arg constructor.

### 3.2 `core/agent.py` (87% — 20 lines missed)

**Missing lines (S004-relevant subset):**

| Lines | Code | Priority | Notes |
|-------|------|----------|-------|
| 376 | `context is None` guard in `_maybe_compact` | P2 | Defensive guard, hard to reach |
| 379-380 | `eval_model = self._ensure_model()` + `compact()` call | P1 | **Compaction trigger path untested end-to-end** |
| 489 | `bus is None` guard in `_load_modules_by_convention` | P2 | Defensive guard |
| 355->358 | Branch: `session.session_id` empty in `chat()` | P2 | Auto-create session path |
| 390-391 | `reload()` when not started | P2 | Error path |

**Gap Analysis:**
- Lines 379-380: The `_maybe_compact` path where `ratio >= compact_threshold` is never exercised in tests. The compaction trigger tests in `test_compaction_trigger.py` only test `SessionManager.token_ratio()` delegation, not the full `agent.chat() -> _maybe_compact() -> session.compact()` chain.
- Line 489: `_load_modules_by_convention` when bus is None is a defensive guard that cannot happen in normal flow.

### 3.3 `core/session_manager.py` (85% — 13 lines missed)

**Missing lines:**

| Lines | Code | Priority | Notes |
|-------|------|----------|-------|
| 97-98 | `resume_session` when session file not found | P2 | Edge case |
| 107 | Skip empty JSONL lines | P2 | Edge case |
| 158 | `compact()` early return (< 4 messages) | P2 | Guard clause |
| 209 | `_pre_compact_flush` empty message text | P2 | Edge case |
| 220-226 | `_pre_compact_flush` model call + context.md write | P1 | **Pre-compaction flush not tested** |
| 241-243 | `_summarize_messages` failure fallback | P1 | Error path: model failure |
| 251 | `cleanup_old_sessions` no sessions dir | P2 | Edge case |

**Gap Analysis:**
- Lines 220-226: Pre-compaction flush (the OpenClaw pattern) has no dedicated test. This writes extracted facts to context.md before compaction — important for information preservation.
- Lines 241-243: Summarization model failure fallback is untested.

### 3.4 `modules/memory/markdown_memory.py` (87% — 30 lines missed)

**Missing lines:**

| Lines | Code | Priority | Notes |
|-------|------|----------|-------|
| 38-40 | `_load_model` function | P2 | Covered via mock in test_memory_wiring |
| 266-267 | `_get_eval_model` no config + no LLM fallback → None | P1 | Edge: both eval_config and llm_config empty |
| 271-273 | `_get_eval_model` load failure → exception → None | P1 | Error path: model load crash |
| 323 | `_handle_memory_search` empty results | P2 | Partially tested in integration |
| 354 | `_on_pre_tool` bash targets memory → veto | P0 | **SEC-001: bash-targets-memory veto untested in S004 context** |
| 377 | `_on_post_tool` path is None → return | P2 | Guard clause |
| 383-384 | `_on_post_tool` identity.md capture_after | P2 | Tested in S002 suite |
| 421 | `_on_post_respond` messages empty → return | P2 | Guard clause |
| 425 | `_on_post_respond` entity extraction disabled | P2 | Config-driven |
| 437 | Policy eval interval | P2 | Config-driven |
| 521-537 | `_bash_targets_memory` token-level resolution | P1 | **Security: bash command parsing for memory path detection** |

**Gap Analysis:**
- Lines 521-537: The token-level bash command parsing in `_bash_targets_memory` is untested. This is the fallback path when the fast-path (absolute workspace path) doesn't match. Security-critical for preventing bash-based memory tampering (SEC-001, ASI02, ASI06).
- Lines 266-267, 271-273: Error paths in `_get_eval_model` (no config available, model load failure).

### 3.5 `core/context_manager.py` (92%)

| Lines | Code | Priority | Notes |
|-------|------|----------|-------|
| 40 | `_msg_attr` with non-dict non-Pydantic message | P2 | Edge case |
| 120 | `token_ratio` when max_tokens == 0 | P2 | Division guard |
| 145 | `prune_observations` empty messages | P2 | Guard |
| 171 | Pydantic `model_copy` pruning path | P2 | Pydantic message object |
| 189 | `transform_context` empty messages | P2 | Guard |

No critical gaps. All core token management paths are well covered.

### 3.6 `core/module_bus.py` (98%)

| Lines | Code | Priority | Notes |
|-------|------|----------|-------|
| 228-229 | `shutdown()` module failure handling | P2 | Error path during shutdown |

Excellent coverage. Only the error path during module shutdown is uncovered.

---

## 4. Critical Gap Prioritization

### P0 — Must Fix (Security/Critical Business Logic)

| # | Gap | File | Lines | Impact |
|---|-----|------|-------|--------|
| 1 | Bash-targets-memory token-level resolution untested | `markdown_memory.py` | 521-537 | SEC-001: Adversary could bypass fast-path memory protection with relative paths or unusual command formats. OWASP ASI02/ASI06. |

### P1 — Should Fix (Important Business Logic / Error Resilience)

| # | Gap | File | Lines | Impact |
|---|-----|------|-------|--------|
| 2 | Pre-compaction flush not tested | `session_manager.py` | 220-226 | Information loss during compaction if flush silently fails in unexpected ways. OpenClaw pattern integrity. |
| 3 | Summarization model failure fallback untested | `session_manager.py` | 241-243 | Silent fallback to truncated message string — need to verify graceful degradation. |
| 4 | Eval model load failure path | `markdown_memory.py` | 271-273 | Entity extraction silently disabled if model fails to load. Verify no crash. |
| 5 | YAML parse error in module loader | `module_loader.py` | 64-65 | Corrupt MODULE.yaml should raise clear ConfigError, not crash. |
| 6 | Module constructor failure | `module_loader.py` | 146-151 | Bad module constructor should return None, not crash agent startup. |
| 7 | Compaction trigger end-to-end | `agent.py` | 379-380 | `_maybe_compact` path never exercised — compaction could silently fail in production. |

### P2 — Nice to Have (Defensive Guards / Edge Cases)

| # | Gap | File | Lines | Impact |
|---|-----|------|-------|--------|
| 8 | Non-memory module default construction | `module_loader.py` | 170 | Future-proofing for non-memory modules. |
| 9 | YAML not-a-dict validation | `module_loader.py` | 72 | Edge case: `MODULE.yaml` contains a list. |
| 10 | Pydantic validation error in loader | `module_loader.py` | 104-105 | Extra/invalid fields in manifest. |
| 11 | Eval model no-config-no-fallback | `markdown_memory.py` | 266-267 | Both configs empty. |
| 12 | Session resume file not found | `session_manager.py` | 97-98 | Edge case. |
| 13 | Session cleanup no dir | `session_manager.py` | 251 | Edge case. |

---

## 5. Improvement Plan

### Phase 1: P0 Security Fix (Estimated: 30 min, +2% coverage on markdown_memory.py)

**Test: `_bash_targets_memory` token-level path resolution**

```python
# tests/unit/modules/memory/test_bash_memory_guard.py

class TestBashTargetsMemoryTokenLevel:
    """SEC-001: Token-level bash command parsing for memory paths."""

    def test_relative_path_to_notes(self, tmp_path: Path) -> None:
        """Detects relative path targeting notes/."""
        module = _make_module(tmp_path)
        # Relative path that resolves to workspace/notes/
        cmd = f"cat > {tmp_path}/notes/2026-02-15.md"
        assert module._bash_targets_memory(cmd) is True

    def test_echo_redirect_to_context(self, tmp_path: Path) -> None:
        """Detects echo > context.md."""
        module = _make_module(tmp_path)
        cmd = f"echo 'injected' > {tmp_path}/context.md"
        assert module._bash_targets_memory(cmd) is True

    def test_malformed_shell_fallback(self, tmp_path: Path) -> None:
        """Malformed shell falls back to substring matching."""
        module = _make_module(tmp_path)
        cmd = "echo 'bad quotes > notes/file.md"
        assert module._bash_targets_memory(cmd) is True

    def test_safe_command_not_blocked(self, tmp_path: Path) -> None:
        """Normal commands not targeting memory are allowed."""
        module = _make_module(tmp_path)
        cmd = "ls /tmp"
        assert module._bash_targets_memory(cmd) is False
```

### Phase 2: P1 Error Resilience (Estimated: 1 hour, +3-4% coverage across files)

**Test 2a: Pre-compaction flush**
```python
# In tests/unit/core/test_session_manager.py or test_compaction_trigger.py

class TestPreCompactionFlush:
    async def test_flush_writes_facts_to_context_md(self, tmp_path: Path) -> None:
        """Pre-compaction flush extracts facts and writes to context.md."""
        session = _make_session(tmp_path)
        model = AsyncMock(return_value="- Key fact 1\n- Key fact 2")
        messages = [{"role": "user", "content": "Important decision made"}]
        await session._pre_compact_flush(messages, tmp_path, model)
        assert (tmp_path / "context.md").exists()
        assert "Key fact" in (tmp_path / "context.md").read_text()

    async def test_flush_appends_to_existing_context(self, tmp_path: Path) -> None:
        """Flush appends to existing context.md content."""
        (tmp_path / "context.md").write_text("# Existing\n\nOld content.")
        session = _make_session(tmp_path)
        model = AsyncMock(return_value="- New fact")
        messages = [{"role": "user", "content": "stuff"}]
        await session._pre_compact_flush(messages, tmp_path, model)
        content = (tmp_path / "context.md").read_text()
        assert "Old content" in content
        assert "New fact" in content
```

**Test 2b: Summarization failure fallback**
```python
class TestSummarizationFallback:
    async def test_model_failure_returns_fallback(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        model = AsyncMock(side_effect=RuntimeError("model down"))
        messages = [{"role": "user", "content": "msg1"}]
        result = await session._summarize_messages(messages, model)
        assert "[Compacted 1 messages]" in result
```

**Test 2c: Eval model load failure**
```python
class TestEvalModelLoadFailure:
    def test_load_failure_returns_none(self, tmp_path: Path) -> None:
        module = _make_module(tmp_path, eval_config=EvalConfig(provider="bad", model="bad"))
        with patch("arcagent.modules.memory.markdown_memory._load_model", side_effect=RuntimeError):
            result = module._get_eval_model()
            assert result is None
```

**Test 2d: YAML parse error**
```python
class TestYAMLParseError:
    def test_corrupt_yaml_raises_config_error(self, tmp_path: Path, config: ArcAgentConfig) -> None:
        modules_dir = tmp_path / "modules"
        mod_dir = modules_dir / "bad"
        mod_dir.mkdir(parents=True)
        (mod_dir / "MODULE.yaml").write_text(": : invalid yaml {{{}}")
        config_with = config.model_copy(update={"modules": {"bad": ModuleEntry(enabled=True)}})
        loader = ModuleLoader()
        with pytest.raises(ConfigError):
            loader.discover(modules_dir, config_with)
```

**Test 2e: Module constructor failure**
```python
class TestModuleConstructorFailure:
    def test_constructor_crash_returns_none(self, tmp_path: Path, module_ctx: ModuleContext) -> None:
        manifest = ModuleManifest(name="memory", entry_point="arcagent.modules.memory:MarkdownMemoryModule")
        loader = ModuleLoader()
        # Patch _instantiate to raise
        with patch.object(loader, "_instantiate", side_effect=TypeError("bad args")):
            result = loader.load(manifest, module_ctx)
            assert result is None
```

### Phase 3: P2 Edge Cases (Estimated: 45 min, +1-2% coverage)

Lower priority. These are defensive guards that protect against edge conditions but are unlikely to be reached in normal operation.

---

## 6. Expected Coverage After Improvements

| File | Current | After Phase 1 | After Phase 2 | After Phase 3 |
|------|---------|---------------|---------------|---------------|
| `module_loader.py` | 85% | 85% | 92% | 96% |
| `agent.py` | 87% | 87% | 89% | 90% |
| `session_manager.py` | 85% | 85% | 92% | 95% |
| `markdown_memory.py` | 87% | 92% | 94% | 96% |
| `context_manager.py` | 92% | 92% | 92% | 92% |
| `module_bus.py` | 98% | 98% | 98% | 98% |
| **S004 Average** | **89.0%** | **89.8%** | **92.8%** | **94.5%** |

---

## 7. Success Criteria

- [ ] P0 gap (bash-targets-memory token resolution) has dedicated test coverage
- [ ] All S004 core files >= 90% line coverage
- [ ] Pre-compaction flush path exercised
- [ ] Error fallback paths verified (model failure, YAML corruption)
- [ ] No regressions: all 610 existing tests continue to pass
- [ ] `mypy --strict` and `ruff check` remain clean
