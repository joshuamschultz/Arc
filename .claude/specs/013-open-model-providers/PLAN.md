# PLAN — Open Model Providers (Step 15)

**Status**: COMPLETE
**Spec**: 013-open-model-providers
**Estimated tasks**: 16
**Estimated new tests**: ~50

---

## Phase 1: Core Auth Changes (Tasks 1-4)

### T15.1 — Add `api_key_required` to ProviderSettings

- [x] Add `api_key_required: bool = True` field to ProviderSettings in config.py
- [x] Default to True for backward compatibility
- [x] Add `api_key_required = true` to existing anthropic.toml and openai.toml
- [x] Write test: config loads with new field, default is True

**Acceptance**:
- [x] All existing config tests pass
- [x] New field has correct default
- [x] Existing provider TOMLs load without errors

### T15.2 — Update BaseAdapter for optional auth (TDD RED → GREEN)

- [x] Write test: BaseAdapter skips key validation when api_key_required=false
- [x] Write test: BaseAdapter reads env var when api_key_required=false and key IS set
- [x] Write test: BaseAdapter raises when api_key_required=true and key missing (existing behavior)
- [x] Modify BaseAdapter.__init__() — conditional key validation based on api_key_required
- [x] Verify all 3 tests pass

**Acceptance**:
- [x] No regression on existing adapter tests
- [x] Local providers constructible without env var
- [x] Optional auth preserved (key used if set)

### T15.3 — Update OpenaiAdapter for conditional auth headers (TDD RED → GREEN)

- [x] Write test: _build_headers() returns no Authorization when _api_key empty
- [x] Write test: _build_headers() returns Authorization when _api_key present
- [x] Modify OpenaiAdapter._build_headers() — only add Authorization if self._api_key
- [x] Verify tests pass

**Acceptance**:
- [x] All existing OpenAI adapter tests pass
- [x] Headers correct for both auth and no-auth cases

### T15.4 — Write test_open_providers.py scaffold (TDD RED)

- [x] Create parametrized test fixture for provider configs
- [x] Write test: each provider TOML loads correctly (parametrized over all 9)
- [x] Write test: adapter class discoverable via registry convention (parametrized)
- [x] Write test: adapter name property returns correct value (parametrized)
- [x] Write test: no-auth headers for local providers (ollama, vllm, huggingface_tgi)
- [x] Write test: auth headers for cloud providers (together, groq, fireworks, deepseek, huggingface)
- [x] Write test: optional auth — local provider with key set sends Authorization
- [x] Write test: basic invoke with mock response (parametrized)
- [x] All tests should FAIL (RED) — no adapter files exist yet

**Acceptance**:
- [x] ~35 test cases written (parametrized)
- [x] All tests fail for correct reason (missing adapters/TOMLs)

---

## Phase 2: Local Providers (Tasks 5-6)

### T15.5 — Create Ollama provider (TOML + adapter)

- [x] Create providers/ollama.toml:
  - api_key_required = false
  - base_url = "http://localhost:11434"
  - api_key_env = "OLLAMA_API_KEY"
  - default_model = "llama3.2"
  - Pre-populate models: llama3.2, llama3.1, mistral (7b), qwen2.5, deepseek-r1
  - All costs = 0.0
- [x] Create adapters/ollama.py — OllamaAdapter(OpenaiAdapter) with name="ollama"
- [x] Verify Ollama-related tests pass (GREEN)

**Acceptance**:
- [x] load_model("ollama") works without API key env var
- [x] Ollama TOML has 5+ pre-populated models

### T15.6 — Create vLLM provider (TOML + adapter)

- [x] Create providers/vllm.toml:
  - api_key_required = false
  - base_url = "http://localhost:8000"
  - api_key_env = "VLLM_API_KEY"
  - default_model = "meta-llama/Llama-3.1-8B-Instruct"
  - Pre-populate models: Llama-3.1-8B, Llama-3.1-70B, Mistral-7B-v0.3
  - All costs = 0.0
- [x] Create adapters/vllm.py — VllmAdapter(OpenaiAdapter) with name="vllm"
- [x] Verify vLLM-related tests pass (GREEN)

**Acceptance**:
- [x] load_model("vllm") works without API key env var
- [x] vLLM TOML has 3+ pre-populated models

---

## Phase 3: Cloud Open Providers (Tasks 7-10)

### T15.7 — Create Together AI provider (TOML + adapter)

- [x] Create providers/together.toml:
  - api_key_required = true
  - base_url = "https://api.together.xyz"
  - api_key_env = "TOGETHER_API_KEY"
  - default_model = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
  - Pre-populate models: Llama-3.3-70B, Llama-3.1-8B, Mixtral-8x7B, Qwen2.5-72B, DeepSeek-V3
  - Costs from Together pricing
- [x] Create adapters/together.py — TogetherAdapter(OpenaiAdapter) with name="together"
- [x] Verify Together-related tests pass (GREEN)

**Acceptance**:
- [x] load_model("together") requires API key
- [x] Together TOML has 5+ pre-populated models with costs

### T15.8 — Create Groq provider (TOML + adapter)

- [x] Create providers/groq.toml:
  - api_key_required = true
  - base_url = "https://api.groq.com/openai"
  - api_key_env = "GROQ_API_KEY"
  - default_model = "llama-3.3-70b-versatile"
  - Pre-populate models: llama-3.3-70b-versatile, llama-3.1-8b-instant, mixtral-8x7b-32768
  - Costs from Groq pricing
- [x] Create adapters/groq.py — GroqAdapter(OpenaiAdapter) with name="groq"
- [x] Verify Groq-related tests pass (GREEN)

**Acceptance**:
- [x] load_model("groq") requires API key
- [x] Groq TOML has 3+ pre-populated models with costs

### T15.9 — Create Fireworks AI + DeepSeek providers (TOML + adapter)

- [x] Create providers/fireworks.toml:
  - api_key_required = true
  - base_url = "https://api.fireworks.ai/inference"
  - api_key_env = "FIREWORKS_API_KEY"
  - default_model = "accounts/fireworks/models/llama-v3p1-70b-instruct"
  - Pre-populate 3+ models with costs
- [x] Create adapters/fireworks.py — FireworksAdapter(OpenaiAdapter) with name="fireworks"
- [x] Create providers/deepseek.toml:
  - api_key_required = true
  - base_url = "https://api.deepseek.com"
  - api_key_env = "DEEPSEEK_API_KEY"
  - default_model = "deepseek-chat"
  - Pre-populate: deepseek-chat, deepseek-reasoner
  - DeepSeek pricing (very cheap)
- [x] Create adapters/deepseek.py — DeepseekAdapter(OpenaiAdapter) with name="deepseek"
- [x] Verify Fireworks + DeepSeek tests pass (GREEN)

**Acceptance**:
- [x] Both providers load correctly with API keys
- [x] DeepSeek models include V3 and R1

### T15.10 — Create HuggingFace providers (TOML + adapter × 2)

- [x] Create providers/huggingface.toml:
  - api_key_required = true
  - base_url = "https://api-inference.huggingface.co"
  - api_key_env = "HF_TOKEN"
  - default_model = "meta-llama/Llama-3.3-70B-Instruct"
  - Pre-populate 3+ popular HF models
- [x] Create adapters/huggingface.py — HuggingfaceAdapter(OpenaiAdapter) with name="huggingface"
- [x] Create providers/huggingface_tgi.toml:
  - api_key_required = false
  - base_url = "http://localhost:8080"
  - api_key_env = "TGI_API_KEY"
  - default_model = "tgi-model"
  - Models section minimal (user-dependent)
  - All costs = 0.0
- [x] Create adapters/huggingface_tgi.py — Huggingface_TgiAdapter(OpenaiAdapter) with name="huggingface_tgi"
- [x] Verify HuggingFace tests pass (GREEN)

**Acceptance**:
- [x] load_model("huggingface") requires HF_TOKEN
- [x] load_model("huggingface_tgi") works without auth
- [x] Both use OpenAI-compatible endpoint

---

## Phase 4: Mistral with Quirk Overrides (Tasks 11-12)

### T15.11 — Write test_mistral.py (TDD RED)

- [x] Test tool_choice "required" → "any" translation
- [x] Test tool_choice "auto" passes unchanged
- [x] Test tool_choice "none" passes unchanged
- [x] Test tool_choice dict (specific function) passes unchanged
- [x] Test no tool_choice kwarg → no tool_choice in body
- [x] Test _map_stop_reason: "stop" → "end_turn"
- [x] Test _map_stop_reason: "tool_calls" → "tool_use"
- [x] Test _map_stop_reason: "length" → "max_tokens"
- [x] Test _map_stop_reason: "model_length" → "max_tokens"
- [x] Test name property returns "mistral"
- [x] Test full invoke cycle with mock response

**Acceptance**:
- [x] ~10 Mistral-specific tests written
- [x] All tests fail (RED) — MistralAdapter not yet implemented

### T15.12 — Create Mistral provider (TOML + adapter with overrides)

- [x] Create providers/mistral.toml:
  - api_key_required = true
  - base_url = "https://api.mistral.ai"
  - api_key_env = "MISTRAL_API_KEY"
  - default_model = "mistral-large-latest"
  - Pre-populate: mistral-large-latest, mistral-small-latest, open-mistral-nemo, codestral-latest
  - Costs from Mistral pricing
- [x] Create adapters/mistral.py:
  - MistralAdapter(OpenaiAdapter)
  - Override name → "mistral"
  - Override _build_request_body: translate tool_choice "required" → "any"
  - Override _map_stop_reason: custom map including "model_length"
- [x] Verify all Mistral tests pass (GREEN)

**Acceptance**:
- [x] All test_mistral.py tests pass
- [x] tool_choice correctly translated
- [x] Stop reasons correctly mapped

---

## Phase 5: Integration and Finalization (Tasks 13-16)

### T15.13 — Update __init__.py with lazy imports for all new adapters

- [x] Add all 9 new adapter classes to _LAZY_IMPORTS dict
- [x] Add all 9 to __all__ list
- [x] Verify lazy import works for each

**Acceptance**:
- [x] `from arcllm import OllamaAdapter` works (lazy)
- [x] `import arcllm` does NOT trigger loading of new adapter modules

### T15.14 — Update .env.example with all new env var names

- [x] Add OLLAMA_API_KEY (optional), VLLM_API_KEY (optional)
- [x] Add TOGETHER_API_KEY, GROQ_API_KEY, FIREWORKS_API_KEY
- [x] Add DEEPSEEK_API_KEY, MISTRAL_API_KEY, HF_TOKEN
- [x] Add TGI_API_KEY (optional)
- [x] Mark optional keys clearly in comments

**Acceptance**:
- [x] .env.example lists all provider env vars
- [x] Optional vs required clearly noted

### T15.15 — Add registry integration tests

- [x] Write test: load_model() with each new provider (9 tests, mocked)
- [x] Write test: fallback chain mixing local + cloud providers
- [x] Verify convention-based adapter discovery works for all 11 total providers

**Acceptance**:
- [x] Registry correctly discovers all 11 providers (2 existing + 9 new)
- [x] Fallback chain works across provider types

### T15.16 — Full test suite verification

- [x] Run full test suite: all existing 451+ tests pass
- [x] Verify new test count: ~50 new tests
- [x] Run coverage: >=90% on new files
- [x] Verify no import-time side effects

**Acceptance**:
- [x] Zero regressions
- [x] All new tests pass
- [x] Coverage target met
- [x] Total tests: ~500+

---

## Completion Checklist

- [x] All 16 tasks complete
- [x] All tests pass (existing + new): 500+ target
- [x] Coverage >=90% on new files
- [x] State file updated with decisions D-100 through D-106
- [x] Decision log updated
- [x] .env.example updated
- [x] 9 new provider TOMLs created
- [x] 9 new adapter files created
- [x] BaseAdapter + OpenaiAdapter auth changes
- [x] MistralAdapter quirk overrides verified
