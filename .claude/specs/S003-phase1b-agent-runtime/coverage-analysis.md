# Coverage Analysis: S003 Phase 1b -- Agent Runtime

**Generated:** 2026-02-15
**Tests:** 148 passed, 0 failed
**Overall S003 Coverage:** 86.11% (line + branch)

---

## Coverage Summary (S003 Files Only)

| File | Stmts | Miss | Branch | BrPart | Cover | Target | Status |
|------|-------|------|--------|--------|-------|--------|--------|
| `arcagent/tools/_validation.py` | 27 | 0 | 10 | 0 | **100%** | 80% | PASS |
| `arcagent/tools/find.py` | 27 | 1 | 8 | 1 | **94%** | 80% | PASS |
| `arcagent/core/skill_registry.py` | 90 | 6 | 28 | 3 | **92%** | 90% | PASS |
| `arcagent/core/extensions.py` | 189 | 14 | 34 | 6 | **91%** | 90% | PASS |
| `arcagent/tools/grep.py` | 58 | 7 | 26 | 1 | **90%** | 80% | PASS |
| `arcagent/core/agent.py` | 246 | 29 | 60 | 19 | **84%** | 90% | **FAIL** |
| `arcagent/core/settings_manager.py` | 83 | 10 | 34 | 5 | **84%** | 90% | **FAIL** |
| `arcagent/tools/ls.py` | 35 | 4 | 16 | 2 | **84%** | 80% | PASS |
| `arcagent/core/config.py` | 145 | 29 | 14 | 0 | **73%** | 90% | **FAIL** |
| **TOTAL** | **900** | **100** | **230** | **37** | **86%** | -- | -- |

---

## Verdict

**PASSED: false** -- 3 core files are below the 90% threshold.

- `agent.py` at 84% (needs 90%)
- `settings_manager.py` at 84% (needs 90%)
- `config.py` at 73% (needs 90%)

Tools all pass their 80% target. `_validation.py` is at 100%.

---

## Critical Gaps -- Prioritized by Impact

### P0: CRITICAL -- Security & Config Loading

#### Gap 1: `config.py` lines 241-262 -- `_apply_env_overrides()` (ENTIRE FUNCTION)

**Coverage:** 0% -- completely untested
**Risk:** HIGH SECURITY -- This function handles environment variable overrides including the security denylist that blocks injection of vault/identity/native-tool overrides. Without tests, an attacker could bypass the denylist by exploiting edge cases.

**Uncovered code:**
```python
# Lines 241-262: _apply_env_overrides()
for key, value in os.environ.items():
    if not key.startswith(_ENV_PREFIX):
        continue
    env_path = key[len(_ENV_PREFIX):].lower()
    # Block security-sensitive overrides
    if any(env_path.startswith(prefix) for prefix in _ENV_DENYLIST_PREFIXES):
        _logger.warning(...)
        continue
    parts = env_path.split(_ENV_DELIMITER)
    target = data
    for part in parts[:-1]:
        if part not in target:
            target[part] = {}
        if not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value
```

**Recommended tests:**
```python
class TestEnvVarOverrides:
    def test_env_override_simple_key(self, monkeypatch, tmp_path):
        """ARCAGENT_LLM__MODEL overrides llm.model"""

    def test_env_override_creates_nested_dict(self, monkeypatch, tmp_path):
        """ARCAGENT_AGENT__ORG creates nested path"""

    def test_env_denylist_blocks_vault_backend(self, monkeypatch, tmp_path):
        """ARCAGENT_VAULT__BACKEND is rejected (security)"""

    def test_env_denylist_blocks_native_tools(self, monkeypatch, tmp_path):
        """ARCAGENT_TOOLS__NATIVE__* is rejected (security)"""

    def test_env_denylist_blocks_identity_key_dir(self, monkeypatch, tmp_path):
        """ARCAGENT_IDENTITY__KEY_DIR is rejected (security)"""

    def test_env_override_non_dict_target_replaced(self, monkeypatch, tmp_path):
        """Overriding a scalar with a nested path converts to dict"""

    def test_non_arcagent_env_vars_ignored(self, monkeypatch, tmp_path):
        """Env vars without ARCAGENT_ prefix are skipped"""
```

#### Gap 2: `config.py` lines 274-300 -- `load_config()` (ENTIRE FUNCTION)

**Coverage:** 0% -- completely untested
**Risk:** HIGH -- This is the entry point for all config loading. Missing coverage means TOML parse errors, file-not-found errors, and Pydantic validation errors are untested.

**Uncovered code:** The entire `load_config()` function including:
- File existence check (line 274)
- TOML syntax error handling (lines 284-291)
- Pydantic validation error handling (lines 297-304)

**Recommended tests:**
```python
class TestLoadConfig:
    def test_load_valid_toml(self, tmp_path):
        """Loads and parses valid arcagent.toml"""

    def test_load_missing_file_raises_config_error(self, tmp_path):
        """ConfigError with CONFIG_FILE_NOT_FOUND code"""

    def test_load_invalid_toml_syntax_raises(self, tmp_path):
        """ConfigError with CONFIG_SYNTAX code"""

    def test_load_invalid_schema_raises(self, tmp_path):
        """ConfigError with CONFIG_VALIDATION code (e.g., missing required fields)"""

    def test_load_applies_env_overrides(self, monkeypatch, tmp_path):
        """Env vars are applied after TOML parse"""
```

---

### P1: HIGH -- Agent Lifecycle & Error Paths

#### Gap 3: `agent.py` lines 408-413 -- `chat()` error event emission

**Coverage:** 0% for the error path in `chat()`
**Risk:** MEDIUM-HIGH -- If the LLM fails during a chat session, the error event path is untested. The analogous `run()` error path IS tested.

**Recommended test:**
```python
class TestChatErrorHandling:
    @patch("arcagent.core.agent._load_model")
    @patch("arcagent.core.agent._run_loop")
    async def test_chat_emits_error_event_on_failure(self, ...):
        """chat() emits agent:error when loop raises"""
```

#### Gap 4: `agent.py` lines 430-456 -- `reload()` method

**Coverage:** 0% -- completely untested
**Risk:** MEDIUM -- Hot reload is a runtime operation. Bugs here could leave the agent in an inconsistent state (tools from old extensions still registered, skills stale).

**Missing lines:** 431-432 (not-started check), 437 (null check), 440-456 (clear + re-discover logic)

**Recommended tests:**
```python
class TestReload:
    async def test_reload_requires_startup(self, agent):
        """reload() raises if not started"""

    async def test_reload_clears_and_rediscovers(self, agent):
        """reload() clears extensions/skills then re-discovers"""

    async def test_reload_emits_loaded_events(self, agent):
        """reload() emits extensions_loaded and skills_loaded"""
```

#### Gap 5: `agent.py` lines 44-46, 60-61 -- `_load_model()` and `_run_loop()`

**Coverage:** 0% for the actual function bodies (always mocked in tests)
**Risk:** LOW -- These are thin wrappers around `arcllm` and `arcrun`. Mocking is correct for unit tests; integration tests would cover these.

#### Gap 6: `agent.py` lines 530, 534-543, 553, 560-561 -- `_register_modules()` and `_create_vault_resolver()`

**Coverage:** Partial -- the memory module registration path (534-543) is untested, and the successful vault resolver creation path (553-561) is only tested for failure.

**Recommended tests:**
```python
class TestModuleRegistration:
    async def test_memory_module_registered_when_enabled(self, tmp_path):
        """Memory module registers with bus when config.modules.memory.enabled=True"""

    async def test_memory_module_skipped_when_not_configured(self, agent):
        """No module registered when 'memory' not in config.modules"""
```

---

### P2: MEDIUM -- Settings & Tool Edge Cases

#### Gap 7: `settings_manager.py` lines 140-142, 159, 164, 171-174, 183

**Coverage:** Missing branches in `_load_overlay_from_toml()` and `_persist_to_toml()`
**Risk:** MEDIUM -- Uncovered paths:
- Line 140-142: TOML parse failure during overlay load (exception branch)
- Line 159: empty overlay early return in persist
- Line 164: config file doesn't exist during persist (creates new)
- Lines 171-174: float and int serialization branches in persist
- Line 183: appending [settings] when content doesn't end with newline

**Recommended tests:**
```python
class TestSettingsEdgeCases:
    def test_load_overlay_corrupt_toml_graceful(self, tmp_path, ...):
        """Corrupt TOML file doesn't crash, just warns"""

    async def test_persist_creates_file_when_missing(self, tmp_path, ...):
        """Writes [settings] to new file if TOML doesn't exist"""

    async def test_persist_float_setting(self, tmp_path, ...):
        """Float values (compaction_threshold) serialized correctly"""

    async def test_persist_int_setting(self, tmp_path, ...):
        """Int values (tool_timeout) serialized correctly"""

    async def test_persist_appends_newline_if_missing(self, tmp_path, ...):
        """Adds newline before [settings] if file doesn't end with one"""
```

#### Gap 8: `grep.py` lines 58-59, 84-85, 100, 106-107

**Coverage:** Missing paths:
- Lines 58-59: `_is_binary()` OSError branch (returns True)
- Lines 84-85: Invalid regex pattern error message
- Line 100: File exceeds `_MAX_FILE_SIZE` skip
- Lines 106-107: `UnicodeDecodeError` / `OSError` during read_text

**Recommended tests:**
```python
class TestGrepEdgeCases:
    async def test_grep_invalid_regex_returns_error(self, workspace, grep_tool):
        """Invalid regex returns error message, not exception"""

    async def test_grep_skips_oversized_files(self, workspace, grep_tool):
        """Files > 5MB are skipped silently"""

    async def test_grep_handles_unreadable_file(self, workspace, grep_tool):
        """Files with read errors are skipped"""
```

#### Gap 9: `ls.py` lines 33-35, 57

**Coverage:** Missing:
- Lines 33-35: `_format_size()` KB and MB branches (only B tested)
- Line 57: "Not a directory" error when path is a file

**Recommended tests:**
```python
class TestLsEdgeCases:
    async def test_ls_shows_kb_size(self, workspace, ls_tool):
        """Files > 1KB show KB format"""

    async def test_ls_shows_mb_size(self, workspace, ls_tool):
        """Files > 1MB show MB format"""

    async def test_ls_file_path_returns_error(self, workspace, ls_tool):
        """ls on a file (not directory) returns error"""
```

#### Gap 10: `find.py` line 61

**Coverage:** Missing "Not a directory" error path

**Recommended test:**
```python
async def test_find_file_as_path_returns_error(self, workspace, find_tool):
    """find with path pointing to a file returns error"""
```

#### Gap 11: `skill_registry.py` lines 114-116, 129-130, 159

**Coverage:** Missing:
- Lines 114-116: OSError when reading skill file
- Lines 129-130: Frontmatter is not a dict
- Line 159: `_extract_frontmatter()` with missing closing `---`

**Recommended tests:**
```python
class TestSkillRegistryEdgeCases:
    def test_unreadable_skill_file_skipped(self, tmp_path):
        """OSError on read doesn't crash discovery"""

    def test_non_dict_frontmatter_skipped(self, tmp_path):
        """Frontmatter that parses to a list/string is skipped"""

    def test_unclosed_frontmatter_skipped(self, tmp_path):
        """File with --- but no closing --- returns None"""
```

#### Gap 12: `extensions.py` lines 76, 132, 211-212, 217-223, 380-381, 426-427, 450

**Coverage:** Missing:
- Line 76: `register_tool()` when source already has prefix
- Line 132: `manifests` property (trivial)
- Lines 211-212: Extension disabled in config
- Lines 217-223: Import failure with telemetry audit
- Lines 380-381: `_remove_extension_handlers()` with actual handlers
- Lines 426-427: `_BlockedPopen.__init__()` in strict sandbox
- Line 450: Python 3.9-3.11 compat path for entry points

---

## Improvement Plan

### Phase 1: Critical (P0) -- Must Fix

| # | File | Gap | Tests to Add | Effort | Expected Delta |
|---|------|-----|-------------|--------|----------------|
| 1 | config.py | `_apply_env_overrides()` | 7 tests | 30 min | +15% on config.py |
| 2 | config.py | `load_config()` | 5 tests | 20 min | +12% on config.py |

**Expected result:** config.py 73% -> ~95%

### Phase 2: High (P1) -- Should Fix

| # | File | Gap | Tests to Add | Effort | Expected Delta |
|---|------|-----|-------------|--------|----------------|
| 3 | agent.py | chat() error path | 1 test | 10 min | +1% on agent.py |
| 4 | agent.py | reload() method | 3 tests | 20 min | +4% on agent.py |
| 5 | agent.py | _register_modules() | 2 tests | 15 min | +2% on agent.py |
| 6 | settings_manager.py | Edge cases | 5 tests | 20 min | +6% on settings.py |

**Expected result:** agent.py 84% -> ~91%, settings_manager.py 84% -> ~90%

### Phase 3: Medium (P2) -- Nice to Have

| # | File | Gap | Tests to Add | Effort | Expected Delta |
|---|------|-----|-------------|--------|----------------|
| 7 | grep.py | Edge cases | 3 tests | 15 min | +5% on grep.py |
| 8 | ls.py | Size format + file path | 3 tests | 10 min | +10% on ls.py |
| 9 | find.py | File-as-path error | 1 test | 5 min | +3% on find.py |
| 10 | skill_registry.py | Error paths | 3 tests | 10 min | +4% on skill.py |
| 11 | extensions.py | Minor branches | 4 tests | 15 min | +3% on ext.py |

---

## Success Criteria

| Metric | Current | Target | After Phase 1 | After Phase 2 |
|--------|---------|--------|---------------|---------------|
| config.py | 73% | 90% | ~95% | 95% |
| agent.py | 84% | 90% | 84% | ~91% |
| settings_manager.py | 84% | 90% | 84% | ~90% |
| extensions.py | 91% | 90% | 91% | 91% |
| skill_registry.py | 92% | 90% | 92% | 92% |
| _validation.py | 100% | 80% | 100% | 100% |
| grep.py | 90% | 80% | 90% | 90% |
| find.py | 94% | 80% | 94% | 94% |
| ls.py | 84% | 80% | 84% | 84% |
| **Overall S003** | **86%** | **80%** | **~90%** | **~93%** |

Phases 1 and 2 bring all core files to their 90% target. Phase 3 is incremental polish.

---

## Security-Critical Coverage Notes

1. **`_validation.py` at 100%** -- Path traversal, null byte injection, symlink denial all tested. Good.
2. **`_apply_env_overrides()` at 0%** -- CRITICAL GAP. The denylist that blocks `vault__backend`, `tools__native`, `tools__process`, `identity__key_dir` from env var override is completely untested. An adversary could inject a malicious vault backend or native tool via environment variables if the denylist logic has bugs.
3. **Strict sandbox** in `extensions.py` is tested (subprocess blocking, filesystem restriction). The `_BlockedPopen` path is untested but low-risk since `_blocked_run` covers the same attack surface.
4. **Vault backend validation** is tested (traversal rejection, missing colon rejection). Good.
