# S002 Memory Module — Coverage Analysis

**Generated**: 2026-02-15
**Test suite**: 427 tests, 0 failures
**Runtime**: 5.08s

---

## Coverage Summary

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| **Overall line coverage** | 93.71% | >= 80% | PASS |
| **Overall branch coverage** | 86.3% (416 branches, 57 partial) | >= 75% | PASS |
| **Core component avg** | 94.7% | >= 90% | PASS |
| **Memory module avg** | 93.0% | >= 90% | PASS |

---

## Per-File Coverage: Memory Module

| File | Stmts | Miss | Branch | BrPart | Cover | Missing Lines |
|------|-------|------|--------|--------|-------|---------------|
| `modules/memory/markdown_memory.py` | 187 | 4 | 62 | 6 | 96% | 226, 250, 256-257 |
| `modules/memory/entity_extractor.py` | 137 | 7 | 50 | 9 | 91% | 85, 108, 130, 168-169, 246-247 |
| `modules/memory/hybrid_search.py` | 142 | 7 | 46 | 6 | 93% | 196, 226-232, 283-285 |
| `modules/memory/policy_engine.py` | 123 | 4 | 34 | 8 | 92% | 155, 171-172, 192 |
| `modules/memory/__init__.py` | 5 | 0 | 0 | 0 | 100% | — |

## Per-File Coverage: Modified Core Files

| File | Stmts | Miss | Branch | BrPart | Cover | Missing Lines |
|------|-------|------|--------|--------|-------|---------------|
| `core/agent.py` | 183 | 19 | 38 | 7 | 88% | 41-43, 57-58, 120-121, 256-257, 333-334, 374-379, 398, 419, 442, 449-450 |
| `core/session_manager.py` | 113 | 13 | 26 | 10 | 83% | 82-83, 92, 143, 199, 210, 213, 215-216, 235-237, 245 |
| `core/config.py` | 136 | 1 | 14 | 1 | 99% | 242 |
| `core/context_manager.py` | 106 | 3 | 38 | 6 | 94% | 124, 169, 224 |
| `core/module_bus.py` | 86 | 2 | 12 | 0 | 98% | 193-194 |
| `core/protocols.py` | 7 | 7 | 0 | 0 | 0% | 8-26 |

---

## Critical Gaps Analysis

### P0 — Critical (Security / Data Integrity)

#### Gap 1: `session_manager.py` — Compaction and Pre-Flush Logic (lines 143, 199, 210, 213, 215-216, 235-237)

**Description**: The `compact()` method's core flow (lines 143-216) including the pre-compaction flush to context.md and the `_summarize_messages()` error fallback (lines 235-237) are only partially covered. The pre-compaction flush writes key facts from messages-about-to-be-compacted to context.md before they are lost. The fallback on summarization failure (line 237: `return f"[Compacted {len(messages)} messages]"`) is untested.

**Impact**: HIGH. Compaction is the mechanism that prevents unbounded memory growth. If the pre-flush fails silently, important conversation state could be permanently lost. The summarization fallback is the safety net — if it has a bug, compaction could crash or produce corrupt data.

**Missing coverage**:
- `compact()` with >= 4 messages triggering 30/70 split
- `_pre_compact_flush()` writing extracted facts to context.md
- `_pre_compact_flush()` error path (model failure during flush)
- `_summarize_messages()` success path
- `_summarize_messages()` failure fallback (line 237)
- `cleanup_old_sessions()` retention enforcement (lines 245+)

**Recommended tests**:
```python
@pytest.mark.asyncio()
async def test_compact_splits_messages_30_70(self, tmp_path):
    """Compaction should summarize oldest 30%, keep recent 70%."""
    sm = _make_session_manager(tmp_path)
    await sm.create_session()
    model = AsyncMock(return_value="Summary of conversation")
    for i in range(10):
        await sm.append_message({"role": "user", "content": f"msg {i}", "type": "message"})
    await sm.compact(model, tmp_path)
    # Should have summary + 7 recent messages
    assert sm.message_count == 8
    assert sm._messages[0]["type"] == "compaction_summary"

@pytest.mark.asyncio()
async def test_pre_compact_flush_writes_to_context_md(self, tmp_path):
    """Pre-flush should extract facts and append to context.md."""
    sm = _make_session_manager(tmp_path)
    await sm.create_session()
    model = AsyncMock(return_value="- Key fact extracted")
    messages = [{"role": "user", "content": "important data", "type": "message"}]
    await sm._pre_compact_flush(messages, tmp_path, model)
    context = (tmp_path / "context.md").read_text()
    assert "Key fact extracted" in context

@pytest.mark.asyncio()
async def test_summarize_messages_failure_returns_fallback(self, tmp_path):
    """Model failure during summarization should return safe fallback."""
    sm = _make_session_manager(tmp_path)
    model = AsyncMock(side_effect=RuntimeError("model down"))
    messages = [{"role": "user", "content": "test", "type": "message"}]
    result = await sm._summarize_messages(messages, model)
    assert "[Compacted" in result

@pytest.mark.asyncio()
async def test_cleanup_old_sessions_removes_beyond_retention(self, tmp_path):
    """Sessions beyond retention_count should be removed."""
    sm = _make_session_manager(tmp_path, retention_count=2)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    for i in range(5):
        (sessions_dir / f"sess-{i}.jsonl").touch()
    await sm.cleanup_old_sessions()
    remaining = list(sessions_dir.glob("*.jsonl"))
    assert len(remaining) == 2
```

**Effort**: 2-3 hours
**Expected coverage increase**: session_manager.py 83% -> 95%+

---

#### Gap 2: `agent.py` — `chat()` error path and vault resolver (lines 333-334, 374-379, 442, 449-450)

**Description**: The `chat()` method's error handling path (lines 374-379) — which emits `agent:error` events when the ArcRun loop throws during multi-turn conversation — is not covered. The vault resolver creation (lines 442-455), which validates backend references and dynamically imports vault modules, is also uncovered. The `chat()` method's guard for uninitialized components (lines 333-334) lacks a test.

**Impact**: HIGH. `chat()` is the primary user-facing API for multi-turn conversations. If error emission fails during exceptions, monitoring and alerting systems won't capture agent failures. The vault resolver handles credential management for federal environments — if dynamic import fails ungracefully, agents can't authenticate.

**Missing coverage**:
- `chat()` when ArcRun loop raises (lines 374-379)
- `chat()` with components not initialized (lines 333-334)
- `_create_vault_resolver()` success path (lines 446-452)
- `_create_vault_resolver()` failure path (lines 453-455)

**Recommended tests**:
```python
@pytest.mark.asyncio()
async def test_chat_emits_error_on_loop_failure(self, ...):
    """chat() should emit agent:error event when loop raises."""
    agent = _make_agent(...)
    await agent.startup()
    with patch("arcagent.core.agent._run_loop", side_effect=RuntimeError("LLM failed")):
        with pytest.raises(RuntimeError):
            await agent.chat("Hello")
    # Verify bus.emit was called with "agent:error"

@pytest.mark.asyncio()
async def test_chat_raises_when_not_started(self, ...):
    """chat() before startup() should raise RuntimeError."""
    agent = _make_agent(...)
    with pytest.raises(RuntimeError, match="not started"):
        await agent.chat("Hello")

@pytest.mark.asyncio()
async def test_vault_resolver_creation_valid_backend(self, ...):
    """Valid backend ref creates resolver instance."""
    # Test with a mock module:class reference

@pytest.mark.asyncio()
async def test_vault_resolver_creation_invalid_backend_raises(self, ...):
    """Invalid backend ref raises on import failure."""
```

**Effort**: 2-3 hours
**Expected coverage increase**: agent.py 88% -> 94%+

---

### P1 — High Impact

#### Gap 3: `entity_extractor.py` — Edge cases in pair extraction and empty entity names (lines 85, 108, 130, 168-169, 246-247)

**Description**: Several branch edges are uncovered: (1) `_get_recent_pair()` when messages contain only non-user/assistant roles (line 85 returns empty), (2) entity with empty name skipped (line 130), (3) malformed JSONL lines in existing facts.jsonl (lines 168-169), (4) corrupt index.json fallback (lines 246-247).

**Impact**: MEDIUM. These are defensive paths that handle corrupt or unexpected data. In production, LLMs may return malformed entity names, or disk corruption could produce invalid JSONL/JSON. Without tests, these error paths could silently fail or crash.

**Recommended tests**:
```python
async def test_only_system_messages_skips_extraction(self, tmp_path):
    """Messages with no user/assistant roles should skip extraction."""
    ext = _make_extractor(tmp_path)
    model = AsyncMock()
    messages = [{"role": "system", "content": "You are an agent"}]
    await ext.extract(messages, model)
    model.assert_not_called()

async def test_empty_entity_name_skipped(self, tmp_path):
    """Entity with empty name should not create directory."""
    ext = _make_extractor(tmp_path)
    model = AsyncMock(return_value=json.dumps({
        "entities": [{"name": "", "type": "concept", "aliases": [], "facts": []}]
    }))
    messages = [
        {"role": "user", "content": "Something about nothing in particular here"},
        {"role": "assistant", "content": "I see, let me think about that carefully"},
    ]
    await ext.extract(messages, model)
    assert not (tmp_path / "entities").exists()

async def test_malformed_jsonl_lines_skipped(self, tmp_path):
    """Corrupt lines in facts.jsonl should be skipped gracefully."""

def test_corrupt_index_returns_empty(self, tmp_path):
    """Corrupt index.json should return fresh empty index."""
    ext = _make_extractor(tmp_path)
    index_path = tmp_path / "entities" / "index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text("not valid json {{{")
    result = ext._load_index()
    assert result == {"version": 1, "entities": {}}
```

**Effort**: 1-2 hours
**Expected coverage increase**: entity_extractor.py 91% -> 97%+

---

#### Gap 4: `hybrid_search.py` — BM25 error handling, large document chunking edges (lines 196, 226-232, 283-285)

**Description**: (1) `_bm25_search()` OperationalError catch (lines 283-285) when FTS5 query syntax is invalid, (2) `_chunk_document()` fallback when no chunks produced (lines 226-232 — the per-line splitting within large sections), (3) entity discovery via `entities/` glob (line 196).

**Impact**: MEDIUM. Invalid search queries (e.g., unbalanced quotes, FTS5 syntax errors) could crash search if the error path is broken. The chunking edge case affects indexing of very large documents.

**Recommended tests**:
```python
async def test_bm25_invalid_query_returns_empty(self, tmp_path):
    """Invalid FTS5 query syntax should return empty results, not crash."""
    search = _make_search(tmp_path)
    search._ensure_db()
    results = search._bm25_search('"unclosed quote', top_k=5)
    assert results == []

def test_chunk_large_section_by_lines(self, tmp_path):
    """Section larger than target chunk size should split by lines."""
    search = _make_search(tmp_path)
    # Single heading section that exceeds target
    content = "# Big Section\n" + "\n".join(["x" * 80 for _ in range(50)])
    chunks = search._chunk_document(content, "test.md")
    assert len(chunks) > 1

async def test_entity_files_discoverable(self, tmp_path):
    """Entity markdown files should be included in file discovery."""
    search = _make_search(tmp_path)
    entities_dir = tmp_path / "entities" / "josh"
    entities_dir.mkdir(parents=True)
    (entities_dir / "summary.md").write_text("Josh is an engineer")
    files = search._discover_files()
    assert any("entities" in str(f) for f in files)
```

**Effort**: 1-2 hours
**Expected coverage increase**: hybrid_search.py 93% -> 98%+

---

#### Gap 5: `policy_engine.py` — Reflect error paths and empty delta edges (lines 155, 171-172, 192)

**Description**: (1) `_reflect()` returning `None` when model returns valid JSON but with all empty arrays (line 192 — already covered by the `not additions and not updates and not rewrites` check, but the branch where model returns valid JSON with empty entities is not explicitly tested), (2) `_reflect()` when model returns invalid JSON (lines 171-172), (3) partial branch at line 155 — policy path not existing during reflect.

**Impact**: LOW-MEDIUM. The reflector is called periodically, not on every request. If it fails gracefully (which is the intended behavior), no user data is lost. However, silent failures could mask issues with the eval model.

**Recommended tests**:
```python
async def test_reflect_returns_none_on_empty_delta(self, tmp_path):
    """Model returning empty arrays should produce no delta."""
    engine = _make_engine(tmp_path)
    model = AsyncMock(return_value=json.dumps({
        "additions": [], "updates": [], "rewrites": []
    }))
    messages = [{"role": "user", "content": "test"}]
    await engine.evaluate(messages, model)
    # policy.md should NOT be created (no delta to apply)
    assert not (tmp_path / "policy.md").exists()

async def test_reflect_invalid_json_skips(self, tmp_path):
    """Invalid JSON from model should return None delta."""
    engine = _make_engine(tmp_path)
    model = AsyncMock(return_value="not json at all!")
    messages = [{"role": "user", "content": "test"}]
    await engine.evaluate(messages, model)
    assert not (tmp_path / "policy.md").exists()
```

**Effort**: 1 hour
**Expected coverage increase**: policy_engine.py 92% -> 97%+

---

### P2 — Medium Impact

#### Gap 6: `core/protocols.py` — Zero coverage (lines 8-26)

**Description**: The `VaultResolverProtocol` and `TelemetryProtocol` are `@runtime_checkable` Protocol classes. They define structural interfaces but have zero coverage.

**Impact**: LOW. These are type-system contracts, not runtime logic. Protocol classes with `runtime_checkable` can be tested with `isinstance()` checks to verify that real implementations match the protocol.

**Recommended tests**:
```python
def test_telemetry_implements_protocol():
    """AgentTelemetry should satisfy TelemetryProtocol."""
    from arcagent.core.protocols import TelemetryProtocol
    from arcagent.core.telemetry import AgentTelemetry
    assert isinstance(AgentTelemetry(...), TelemetryProtocol)
```

**Effort**: 30 minutes
**Expected coverage increase**: protocols.py 0% -> 100%

---

#### Gap 7: `session_manager.py` — Resume with malformed JSONL (lines 82-83, 92)

**Description**: `resume_session()` when the session file does not exist (line 82-83) and when empty lines exist in JSONL (line 92) are uncovered branches.

**Impact**: LOW. These are defensive error handling paths for corrupted or missing session files. Important for robustness but not security-critical.

**Recommended tests**:
```python
async def test_resume_missing_session_returns_empty(self, tmp_path):
    """Resuming a non-existent session should return empty list."""
    sm = _make_session_manager(tmp_path)
    messages = await sm.resume_session("nonexistent-id")
    assert messages == []

async def test_resume_with_blank_lines_in_jsonl(self, tmp_path):
    """Blank lines in JSONL should be skipped."""
    sm = _make_session_manager(tmp_path)
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    jsonl = sessions_dir / "test-id.jsonl"
    jsonl.write_text('{"type":"message","content":"hi"}\n\n{"type":"message","content":"bye"}\n')
    messages = await sm.resume_session("test-id")
    assert len(messages) == 2
```

**Effort**: 30 minutes
**Expected coverage increase**: session_manager.py +3-4%

---

#### Gap 8: `context_manager.py` — Emergency truncation edges (line 224)

**Description**: The `_emergency_truncate()` method's branch where a message has non-string content (line 224: `msg_tokens = 0`) is not tested. This occurs when messages contain structured content (lists, dicts) rather than plain strings.

**Impact**: LOW. Emergency truncation is a last-resort safety mechanism. The non-string content branch is defensive but unlikely to cause data loss.

**Effort**: 30 minutes

---

## Improvement Plan

### Phase 1 — Critical (P0): Must Fix

| # | Gap | File | Est. Coverage Gain | Effort |
|---|-----|------|--------------------|--------|
| 1 | Compaction & pre-flush | session_manager.py | 83% -> 95% | 2-3h |
| 2 | chat() error path & vault | agent.py | 88% -> 94% | 2-3h |

**Expected project coverage after Phase 1**: 95.5%+

### Phase 2 — High Impact (P1): Should Fix

| # | Gap | File | Est. Coverage Gain | Effort |
|---|-----|------|--------------------|--------|
| 3 | Entity edge cases | entity_extractor.py | 91% -> 97% | 1-2h |
| 4 | Search error paths | hybrid_search.py | 93% -> 98% | 1-2h |
| 5 | Policy reflect edges | policy_engine.py | 92% -> 97% | 1h |

**Expected project coverage after Phase 2**: 96.5%+

### Phase 3 — Medium Impact (P2): Nice to Have

| # | Gap | File | Est. Coverage Gain | Effort |
|---|-----|------|--------------------|--------|
| 6 | Protocol verification | protocols.py | 0% -> 100% | 30m |
| 7 | Session resume edges | session_manager.py | +3% | 30m |
| 8 | Emergency truncation | context_manager.py | +1% | 30m |

**Expected project coverage after Phase 3**: 97%+

---

## Success Criteria

- [ ] Overall line coverage >= 80% (CURRENTLY PASSING: 93.71%)
- [ ] Core component coverage >= 90% (CURRENTLY PASSING: 94.7%)
- [ ] Branch coverage >= 75% (CURRENTLY PASSING: 86.3%)
- [ ] All P0 critical gaps addressed
- [ ] session_manager.py compaction paths tested
- [ ] agent.py chat() error path tested
- [ ] All 427+ tests pass with 0 failures

---

## Verdict

**PASSED** — All three quality gates are met. The codebase has strong coverage at 93.71% overall with 86.3% branch coverage. The primary gaps are in error/fallback paths (compaction failure, model unavailability, corrupt data handling) rather than in core business logic. Phase 1 improvements would close the two most impactful gaps: session compaction and the chat() error path.
