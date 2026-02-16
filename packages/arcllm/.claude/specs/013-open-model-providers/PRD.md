# PRD — Open Model Providers (Step 15)

## Problem Statement

ArcLLM currently supports only two providers (Anthropic and OpenAI). In federal production environments with thousands of concurrent agents:

1. **Air-Gapped Deployments**: Government networks that cannot reach external APIs need local inference via Ollama or vLLM. No local provider support means ArcLLM cannot be used in classified or air-gapped environments.

2. **Cost Optimization**: Simple agent tasks (classification, extraction, routing) don't need frontier models. Open-weight models running locally or on cheap cloud providers reduce cost by 10-100x per call.

3. **Fallback Diversity**: The existing FallbackModule can only chain between Anthropic and OpenAI. Adding open-model providers enables diverse fallback chains (cloud → cloud-open → local) for maximum resilience.

4. **Model Flexibility**: Different agent tasks need different models. A multi-agent system should be able to mix Claude for complex reasoning, Llama for simple tasks, and DeepSeek for cost-sensitive workloads.

## Goals

| # | Goal | Success Metric |
|---|------|----------------|
| G1 | Support local inference servers (Ollama, vLLM) | load_model("ollama") and load_model("vllm") work with no API key |
| G2 | Support major cloud open-model providers | load_model("together"), load_model("groq"), etc. work with API key |
| G3 | Support HuggingFace ecosystem (cloud + self-hosted) | load_model("huggingface") and load_model("huggingface_tgi") both work |
| G4 | Handle optional authentication gracefully | Local providers work without API keys; keys used if provided |
| G5 | Minimal code duplication | All OpenAI-compatible providers reuse OpenaiAdapter via inheritance |
| G6 | Pre-populated model metadata for popular models | Common models have context_window, capabilities, and cost data in TOMLs |
| G7 | Handle Mistral-specific API quirks | Mistral adapter overrides tool_choice and stop_reason mapping |

## Success Criteria

- [ ] SC-1: load_model("ollama") works without OLLAMA_API_KEY set
- [ ] SC-2: load_model("vllm") works without VLLM_API_KEY set
- [ ] SC-3: load_model("together") works with TOGETHER_API_KEY set
- [ ] SC-4: load_model("groq") works with GROQ_API_KEY set
- [ ] SC-5: load_model("fireworks") works with FIREWORKS_API_KEY set
- [ ] SC-6: load_model("deepseek") works with DEEPSEEK_API_KEY set
- [ ] SC-7: load_model("mistral") works with MISTRAL_API_KEY set
- [ ] SC-8: load_model("huggingface") works with HF_TOKEN set
- [ ] SC-9: load_model("huggingface_tgi") works without auth
- [ ] SC-10: Mistral adapter translates tool_choice="required" to "any"
- [ ] SC-11: All alias adapters return correct provider name from .name property
- [ ] SC-12: No auth header sent when api_key_required=false and no key set
- [ ] SC-13: Auth header sent when api_key_required=false but key IS set (optional auth)
- [ ] SC-14: All existing tests pass (451+)
- [ ] SC-15: >=90% coverage on new code

## Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|-------------|----------|------------|
| FR-1 | Add `api_key_required: bool = True` to ProviderSettings | P0 | Unit test: config loads with new field |
| FR-2 | BaseAdapter skips API key validation when api_key_required=false | P0 | Unit test: no raise on missing key |
| FR-3 | BaseAdapter still reads env var if set (optional auth) | P0 | Unit test: key available even if not required |
| FR-4 | OpenaiAdapter conditionally includes Authorization header | P0 | Unit test: no header when key empty |
| FR-5 | OllamaAdapter inherits OpenaiAdapter, name="ollama" | P0 | Unit test: load + invoke |
| FR-6 | VllmAdapter inherits OpenaiAdapter, name="vllm" | P0 | Unit test: load + invoke |
| FR-7 | TogetherAdapter inherits OpenaiAdapter, name="together" | P0 | Unit test: load + invoke |
| FR-8 | GroqAdapter inherits OpenaiAdapter, name="groq" | P0 | Unit test: load + invoke |
| FR-9 | FireworksAdapter inherits OpenaiAdapter, name="fireworks" | P0 | Unit test: load + invoke |
| FR-10 | DeepseekAdapter inherits OpenaiAdapter, name="deepseek" | P0 | Unit test: load + invoke |
| FR-11 | MistralAdapter inherits OpenaiAdapter with quirk overrides | P0 | Unit test: tool_choice + stop_reason |
| FR-12 | HuggingfaceAdapter inherits OpenaiAdapter, name="huggingface" | P0 | Unit test: load + invoke |
| FR-13 | Huggingface_TgiAdapter inherits OpenaiAdapter, name="huggingface_tgi" | P0 | Unit test: load + invoke |
| FR-14 | Provider TOMLs with model metadata for each provider | P0 | Config loads without errors |
| FR-15 | Local provider TOMLs have api_key_required=false | P0 | Config validation |
| FR-16 | Local provider TOMLs have cost_*_per_1m = 0.0 | P1 | Config field values |
| FR-17 | Popular models pre-populated per provider | P1 | At least 2 models per provider |
| FR-18 | All adapters loadable via registry load_model() | P0 | Integration test per provider |
| FR-19 | Existing anthropic.toml and openai.toml gain api_key_required=true | P0 | Backward compatibility |
| FR-20 | .env.example updated with all new env var names | P1 | File contains all key env vars |

## Non-Functional Requirements

| ID | Requirement | Target | Measurement |
|----|-------------|--------|-------------|
| NFR-1 | Zero import overhead for unused providers | No httpx load from import arcllm | Lazy import test |
| NFR-2 | Alias adapter file size | <20 lines each | Line count |
| NFR-3 | TOML parse time for 11 providers | <50ms total | Benchmark |
| NFR-4 | No new core dependencies | Zero new packages | pip freeze diff |
| NFR-5 | Backward compatibility | Existing code unchanged | All 451 existing tests pass |

## User Stories

### US-1: Federal Air-Gapped Deployment

> As a federal ops engineer, I need to use ArcLLM with Ollama on an air-gapped network where no external API calls are possible.

**Acceptance**:
- load_model("ollama") works with no internet and no API key
- Agents run the same tool-calling loop pattern as with Anthropic/OpenAI
- base_url defaults to localhost but is configurable for remote Ollama servers

### US-2: Cost-Conscious Multi-Agent System

> As an agent architect, I want to route simple tasks to cheap/free open models and complex tasks to Claude, minimizing total LLM spend.

**Acceptance**:
- load_model("groq") provides fast inference for classification tasks
- load_model("deepseek") provides cheap inference for extraction tasks
- Telemetry tracks $0.00 for local models and actual cost for cloud providers
- FallbackModule can chain across provider types

### US-3: Self-Hosted Inference at Scale

> As an ML engineer, I run vLLM with Llama-3.1-70B on a GPU cluster. I want ArcLLM agents to use my vLLM endpoint.

**Acceptance**:
- load_model("vllm", model="meta-llama/Llama-3.1-70B-Instruct") works
- base_url points to vLLM server (configurable in TOML)
- Optional API key for secured deployments

### US-4: HuggingFace Inference API User

> As a developer using HuggingFace's Inference API, I want ArcLLM to connect to HF's OpenAI-compatible endpoint with my HF token.

**Acceptance**:
- load_model("huggingface", model="meta-llama/Llama-3.3-70B-Instruct") works
- Authorization: Bearer {HF_TOKEN} sent in headers
- Works with both serverless and dedicated Inference Endpoints

### US-5: Mistral API User

> As a developer using Mistral's API, I want tool-calling to work correctly despite Mistral's slightly different tool_choice format.

**Acceptance**:
- load_model("mistral", model="mistral-large-latest") works
- tool_choice="required" in kwargs automatically maps to "any" for Mistral
- Stop reasons correctly normalized to ArcLLM's StopReason type

## Out of Scope

- **Google Gemini**: Different API format entirely, separate step
- **Azure OpenAI / AWS Bedrock**: Enterprise cloud wrappers, separate step
- **Streaming**: All providers use non-streaming invoke() for now
- **Model discovery at runtime**: No auto-querying provider APIs for available models
- **Provider-specific features**: Only normalized chat + tool calling via invoke()
- **Load balancing across providers**: Router module (Step 9, deferred)

## Dependencies

- Step 5 (OpenAI adapter) — base class for all aliases
- Step 6 (Registry) — convention-based loading for new providers
- No new package dependencies (all adapters use httpx via BaseAdapter)
