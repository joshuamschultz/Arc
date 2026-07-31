# PLAN: Tool Executor Extraction

**Spec:** 002-tool-executor
**Status:** COMPLETE
**Phases:** 1

---

## Phase 1: Extract and Verify

Single phase — this is a pure refactor with zero behavior change.

### Tasks

- [x] 1.1 Write test_executor.py with all 7 test cases (RED — tests fail, module doesn't exist)
- [x] 1.2 Create executor.py with execute_tool_call function (GREEN — all 7 tests pass)
- [x] 1.3 Modify react.py to use execute_tool_call (removed inline pipeline)
- [x] 1.4 Run full test suite (77 tests passed — 70 existing + 7 new)
- [x] 1.5 Run mypy and ruff (ruff clean; mypy only has pre-existing arcllm py.typed issue)

### Files Modified

| File | Action |
|------|--------|
| `tests/test_executor.py` | Created — 7 unit tests |
| `src/arcrun/executor.py` | Created — 68 lines, execute_tool_call function |
| `src/arcrun/strategies/react.py` | Modified — 178 → 135 lines (-43 lines) |

### Verification Results

```
pytest tests/ -v                     # 77 passed in 0.37s
ruff check src/arcrun/               # All checks passed!
mypy src/arcrun/                     # 3 errors (all pre-existing arcllm py.typed)
```

### Completion Criteria

- [x] execute_tool_call handles: success, denied, not found, invalid schema, exception
- [x] Events emitted identically (tool.start, tool.end, tool.error)
- [x] state.tool_calls_made incremented on success only
- [x] react.py contains no inline tool execution logic
- [x] All existing tests pass unchanged
- [x] mypy clean (no new issues), ruff clean
- [x] Net line impact: -1 line (622 vs 623)

### Final Counts

| Metric | Value |
|--------|-------|
| Tasks | 5/5 complete |
| New files | 2 (executor.py, test_executor.py) |
| Modified files | 1 (react.py) |
| New tests | 7 |
| Total tests | 77 |
| Line impact | -1 net (622 total) |
