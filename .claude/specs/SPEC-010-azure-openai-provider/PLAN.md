# PLAN: Azure OpenAI Provider (Azure AI Foundry)

**Spec**: SPEC-010
**Status**: COMPLETE
**Route**: Fast-track (single phase)
**Estimated LOC**: ~80 new lines across 4 files

---

## What We're NOT Doing

- Azure AD / Managed Identity auth (requires azure-identity dependency)
- Azure `data_sources` / On Your Data RAG support
- Streaming support
- Azure Content Safety API integration
- Automatic deployment discovery
- Legacy deployment-based URL path (v1 API only for now)

---

## Phase 1: Implementation (Single Phase)

### Task 1: Create provider TOML config
- [x] Create `packages/arcllm/src/arcllm/providers/azure_openai.toml`
- [x] Include `[provider]` section with Azure-specific settings
- [x] Set `api_key_env = "AZURE_OPENAI_API_KEY"`
- [x] Set `base_url` placeholder for GCC endpoint
- [x] Include model metadata for `gpt-4o`, `gpt-4o-mini`, `gpt-4_1`, `gpt-4_1-mini`
- [x] Verify TOML loads: `load_provider_config("azure_openai")`

**Files**: `packages/arcllm/src/arcllm/providers/azure_openai.toml` (new)

### Task 2: Create adapter module
- [x] Create `packages/arcllm/src/arcllm/adapters/azure_openai.py`
- [x] Subclass `OpenaiAdapter`
- [x] Override `name` property -> `"azure_openai"`
- [x] Override `_build_headers()` -> `api-key` header instead of Bearer
- [x] Override `invoke()` -> Azure v1 URL path (`{base}/openai/v1/chat/completions`)
- [x] Docstring explaining Azure differences

**Files**: `packages/arcllm/src/arcllm/adapters/azure_openai.py` (new)

### Task 3: Register lazy import
- [x] Add `"Azure_OpenaiAdapter": "arcllm.adapters.azure_openai"` to `_LAZY_IMPORTS`
- [x] Add `"Azure_OpenaiAdapter"` to `__all__`

**Files**: `packages/arcllm/src/arcllm/__init__.py` (edit)

### Task 4: Add to test parametrization
- [x] Add azure_openai to `CLOUD_PROVIDERS` list in test file
- [x] Add `"azure_openai": "azure_openai"` to `EXPECTED_NAMES`
- [x] Verify all parametrized tests pass

**Files**: `packages/arcllm/tests/test_open_providers.py` (edit)

### Task 5: Write Azure-specific tests
- [x] Test `_build_headers()` returns `api-key` header (not Authorization)
- [x] Test `invoke()` URL construction includes `/openai/v1/chat/completions`
- [x] Test deployment name passed as `model` in request body
- [x] Test `content_filter` finish_reason handled (already in OpenAI base)

**Files**: `packages/arcllm/tests/test_azure_openai.py` (new)

### Task 6: Verify
- [x] Run `pytest packages/arcllm/tests/test_open_providers.py` — all pass
- [x] Run `pytest packages/arcllm/tests/test_azure_openai.py` — all pass
- [x] Run `pytest packages/arcllm/tests/` — all pass, 0 failures (685 passed)
- [x] Run `ruff check` — new files clean
- [x] Run `ruff format --check` — formatted

---

## Completion Criteria

- [x] All 6 tasks complete
- [x] All existing tests pass (0 regressions)
- [x] All new parametrized tests pass for azure_openai
- [x] Azure-specific tests pass (headers, URL, deployment name)
- [x] Linter clean (no new issues)
- [x] 0 new dependencies

**Total tasks**: 6
**Completed**: 6
**Remaining**: 0

---

## Research Insights

### Task 2 Implementation Notes

- **No query parameters**: The v1 API hard-fails (400) if `?api-version=` is appended. Ensure URL construction is clean: `f"{base_url}/openai/v1/chat/completions"` with no extras.
- **Handle null content**: When content filter triggers, `choices[0].message.content` is `null`. The `_parse_response()` in base OpenaiAdapter should handle this, but verify in Task 5 tests.
- **`api-key` header is lowercase**: Must be exactly `api-key`, case-sensitive.
- **Class name**: `Azure_openaiAdapter` with `# noqa: N801` — follows `Huggingface_TgiAdapter` precedent.

### Task 5 Additional Test Cases

- Test `_build_headers()` does NOT include `Authorization` key (only `api-key` and `Content-Type`)
- Test URL has no query parameters (no `?api-version=`)
- Test response with `null` content field (content filter scenario)
- Test 429 response — verify `_parse_retry_after()` extracts `Retry-After` header (inherited behavior)
- Test `name` property returns `"azure_openai"`

### Task 5 Content Filter Test Matrix

Three distinct content filter scenarios to test:

| Scenario | HTTP Status | `content` | `finish_reason` | Test Action |
|----------|------------|-----------|-----------------|-------------|
| Prompt blocked | 400 | N/A (no choices) | N/A | Assert `ArcLLMAPIError` raised |
| Output blocked | 200 | `null` | `"content_filter"` | Assert graceful handling, stop_reason mapped |
| Filter unavailable | 200 | Present (may be partial) | Normal | Assert content returned, error sub-object logged |

### URL Construction Safety

- `rstrip('/')` on base_url is safe for all Azure URL patterns (hostnames always have path after TLD)
- Full f-string construction avoids httpx relative-path pitfalls (no trailing-slash / leading-slash interaction)
- Assert `'?' not in url` in tests to catch accidental query parameter injection (v1 API hard-fails on `api-version`)

### Edge Cases to Document (Not Implement)

- DeploymentNotFound 404 after fresh deployment (5-min propagation delay)
- Entra ID auth uses different error format (future scope)
- `max_tokens` affects rate limit pre-allocation (user guidance)
- GCC context cap: 300k tokens (not 1M) — reflected in provider TOML metadata
- GCC model retirement schedule differs from commercial Azure
