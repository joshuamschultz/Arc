# ArcLLM Roadmap

> Last updated: 2026-02-21

## Current State

ArcLLM has completed Steps 1-15 of its original 16-step build plan (Step 9: Router was intentionally deferred). The security analysis extended the plan to Steps 17-18 + Phase 6 docs. A competitive gap analysis against LiteLLM, pi-ai (badlogic/pi-mono), and Vercel AI SDK revealed significant missing capabilities.

This roadmap incorporates incomplete items from the existing plan, new capabilities from competitive analysis, and compliance documentation requirements.

---

## What's Built (Steps 1-15)

| Step | Feature | Status | Notes |
|------|---------|--------|-------|
| 1 | Project setup + Pydantic types | Complete | types.py, config.py |
| 2 | Config loading (global + provider TOMLs) | Complete | config.toml + providers/ |
| 3 | Anthropic adapter | Complete | Custom adapter with full type mapping |
| 4 | Test harness | Complete | pytest + pytest-asyncio |
| 5 | OpenAI adapter | Complete | Custom adapter |
| 6 | Provider registry + load_model() | Complete | Convention-based, cached, vault-aware |
| 7 | Fallback + retry | Complete | Modules with backoff+jitter |
| 8 | Rate limiter | Complete | Token bucket algorithm |
| 9 | Router | Skipped | Deferred -- revisit as compliance-aware routing |
| 10 | Telemetry | Complete | Per-call timing, tokens, cost |
| 11 | Audit trail | Complete | PII-safe metadata logging |
| 12 | Budget manager | Config only | config.toml has `[modules.budget]` -- no code |
| 13 | OpenTelemetry export | Complete | gRPC/HTTP OTLP, mTLS, BatchSpanProcessor |
| 14 | Security layer | Complete | PII redaction (both directions), HMAC-SHA256 signing, vault integration, TLS enforcement |
| 15 | Local/open providers | Complete | Ollama, vLLM + 10 OpenAI-compatible thin adapters |

## Incomplete Items

| Item | Status | Gap |
|------|--------|-----|
| Budget module (Step 12) | Config exists, no implementation | `modules/budget.py` never created |
| ECDSA P-256 signing | Stub in `_signing.py:64-72` | Raises "not yet fully implemented" |
| Step 16: Full integration test | Not started | No end-to-end test with all modules active |
| Step 17: Tool call validator | Not started | Agent-cooperative module for tool allowlisting |
| Step 18: Content scanner | Not started | Injection pattern detection |
| SBOM generation | Not started | Federal compliance documentation |
| Threat model document | Not started | Architecture-specific data flow + trust boundaries |
| IR runbook | Not started | Incident response procedures |

## Competitive Gaps (vs LiteLLM / pi-ai / Vercel AI SDK)

| Gap | Impact | Who Has It? |
|-----|--------|-------------|
| **No streaming** | Blocks real-time UX, chat interfaces, long-generation monitoring | All three (LiteLLM, pi-ai, Vercel) |
| **No structured output** | Can't request JSON mode or schema-constrained responses | LiteLLM, Vercel |
| **No embeddings API** | Can't use ArcLLM for RAG pipelines | LiteLLM, Vercel |
| **No prompt caching (send-side)** | Reads cache tokens but can't send `cache_control` params | Anthropic SDK, LiteLLM |
| **No thinking/reasoning params** | Reads `thinking` from response but can't enable extended thinking | pi-ai (streaming thinking content), Anthropic SDK |
| **No token counting (pre-call)** | Can't estimate tokens before sending, can't enforce budgets pre-flight | LiteLLM, pi-ai (token + cost tracking) |
| **No response caching** | Every identical request hits the provider | LiteLLM (Redis/in-memory) |
| **No model discovery** | Can't list available models from a provider | LiteLLM, pi-ai (auto-discovery) |
| **No batch API** | Can't send batches for async processing | LiteLLM, OpenAI SDK |
| **No multimodal beyond image+text** | No audio, video, or document input support | Vercel AI SDK |
| **No advanced routing** | Fallback chain only -- no least-latency, round-robin, cost-optimized | LiteLLM |
| **No guardrail hooks** | No pre/post invoke hooks for custom validation | Vercel |
| **No cross-provider handoffs** | Can't transfer conversation context between providers mid-session | pi-ai (serializable context, seamless handoffs) |

---

## Roadmap

### Phase 3: Observability -- Finish (1 step remaining)

#### Step 12 -- Budget Module

- **Status**: Config exists at `config.toml [modules.budget]`, no implementation
- **What**: Hard spend caps per period (daily/monthly), per-agent or per-provider isolation, alert thresholds (warn 80%, block 100%)
- **File**: `modules/budget.py` (~120 LOC)
- **Config**: `monthly_limit_usd`, `daily_limit_usd`, `alert_threshold_pct`, `enforcement` (warn|block)
- **NIST**: SI-4(4), MANAGE
- **OWASP**: T4, LLM10

---

### Phase 4: Enterprise -- Finish (2 steps remaining)

#### Step 15.1 -- ECDSA P-256 Signing (complete the stub)

- **Status**: Stub at `_signing.py:64-72`, raises "not yet fully implemented"
- **What**: Complete ECDSA P-256 signer using `cryptography` library (optional dep via `arcllm[signing]`)
- **File**: `_signing.py` -- add `EcdsaSigner` class (~40 LOC)
- **Why**: NIST SP 800-186 recommends P-256 for federal systems

#### Step 16 -- Full Integration Test

- **Status**: Not started
- **What**: End-to-end test: `load_model()` with all modules enabled -> invoke with tool calls -> verify audit trail, telemetry, OTel spans, PII redaction, signing
- **File**: `tests/integration/test_full_stack.py` (~200 LOC)

---

### Phase 5: Security Hardening (2 new modules)

#### Step 17 -- Tool Call Validator Module

- **Status**: Not started
- **What**: Agents declare allowed tool names at `load_model()` time. Module rejects any `tool_use` in LLM response not in allowlist. Validates arguments against Tool.parameters JSON Schema.
- **File**: `modules/tool_validator.py` (~100 LOC)
- **OWASP**: T2, LLM06

#### Step 18 -- Content Scanner Module

- **Status**: Not started
- **What**: Configurable regex/pattern scanning on messages and responses. Detect injection patterns ("ignore previous instructions", "system:", etc.). Configurable action: log, warn, or raise.
- **File**: `modules/content_scanner.py` (~120 LOC)
- **OWASP**: LLM01, LLM02, LLM07

---

### Phase 6: Streaming & Structured Output (NEW -- critical gap)

These are the two features that every major competitor has and ArcLLM lacks. Without them, ArcLLM cannot serve as a LiteLLM/pi-ai replacement.

#### Step 19 -- Streaming Support

- **What**: Add `stream()` method to `LLMProvider` ABC and all adapters. Returns `AsyncIterator[StreamChunk]`.
- **Types** (new in `types.py`):
  - `StreamChunk(BaseModel)`: `delta_text`, `delta_tool_call`, `usage` (partial), `done: bool`
  - `StreamResponse(BaseModel)`: Accumulated final result (same as `LLMResponse`)
- **Provider changes**: Each adapter implements `stream()` using provider's streaming API (Anthropic SSE, OpenAI SSE, etc.)
- **Module support**: All modules in the stack must handle streaming -- `BaseModule` gets `stream()` that wraps inner stream with pre/post hooks. OTel creates a span for the full stream. Telemetry accumulates tokens. Audit logs final result. Security redacts final accumulated content. Rate limiter checks before stream starts.
- **Why this is hard**: Module middleware chain must stream through without buffering (defeats the purpose). Each module needs both `invoke()` and `stream()` paths.
- **File changes**: `types.py` (+2 types), `adapters/base.py` (+stream ABC), `adapters/anthropic.py` (+stream impl), `adapters/openai.py` (+stream impl), `modules/base.py` (+stream default), every module (+stream override), `registry.py` (load_model returns streamable provider)
- **LOC estimate**: ~400 across all files

#### Step 20 -- Structured Output

- **What**: Add `response_format` kwarg to `invoke()` and `stream()`. Supports JSON mode and JSON Schema constrained output.
- **Types** (new in `types.py`):
  - `ResponseFormat`: `{"type": "json_object"}` or `{"type": "json_schema", "schema": {...}}`
- **Provider mapping**: Anthropic (use tool_use with schema as workaround, or native JSON mode when available), OpenAI (native `response_format`), others (pass-through or adapt)
- **Module support**: Tool validator can also validate structured output against schema
- **File changes**: `types.py` (+1 type), `adapters/anthropic.py` (+format translation), `adapters/openai.py` (+format pass-through), `adapters/base.py` (+kwarg)
- **LOC estimate**: ~150 across all files

---

### Phase 7: LLM Parity Features (NEW -- competitive completeness)

#### Step 21 -- Prompt Caching (Send-Side)

- **What**: Support Anthropic's `cache_control` on messages and OpenAI's prompt caching. ArcLLM already tracks cache_read/write tokens in Usage -- this completes the loop by sending cache hints.
- **Types**: Add optional `cache_control` field to `Message` or pass via kwargs
- **Provider mapping**: Anthropic (native `cache_control` on content blocks), OpenAI (automatic, no action needed), others (ignored)
- **LOC estimate**: ~60

#### Step 22 -- Extended Thinking / Reasoning Params

- **What**: Support Anthropic's `thinking` config (type, budget_tokens) and OpenAI's reasoning effort. ArcLLM already reads `thinking` from response -- this adds the request-side params.
- **Types**: Add `ThinkingConfig` to kwargs or `invoke()` signature
- **Provider mapping**: Anthropic (`thinking` param in request body), OpenAI (`reasoning_effort`), others (ignored)
- **LOC estimate**: ~80

#### Step 23 -- Pre-Call Token Counting

- **What**: Estimate token count before sending to provider. Used by budget module for pre-flight checks and by agents for context window management.
- **Approach**: Use provider's tokenizer when available (tiktoken for OpenAI, Anthropic's counter), fall back to character-based estimate
- **API**: `model.count_tokens(messages, tools)` -> `TokenCount(input_tokens: int, estimated: bool)`
- **File**: `tokenizer.py` (~100 LOC) + provider-specific tokenizer adapters
- **LOC estimate**: ~150

#### Step 24 -- Embeddings API

- **What**: `model.embed(texts)` returns `EmbeddingResponse` with vectors and usage.
- **Types** (new in `types.py`):
  - `EmbeddingResponse(BaseModel)`: `embeddings: list[list[float]]`, `usage`, `model`
- **Provider support**: OpenAI (native), Anthropic (not supported -- skip), Ollama (native), HuggingFace (native), Together (native)
- **Module support**: Telemetry, audit, rate limiting apply. Security (PII redaction on input texts). OTel spans.
- **LOC estimate**: ~200

#### Step 25 -- Response Caching Module

- **What**: Cache LLM responses keyed on (provider, model, messages hash, tools hash). Pluggable backends: in-memory (default), Redis (optional).
- **Config**: `[modules.cache] enabled`, `backend` (memory|redis), `ttl_seconds`, `max_entries`
- **Module position**: Just inside rate limiter (cache hit skips provider call entirely)
- **File**: `modules/cache.py` (~120 LOC)
- **LOC estimate**: ~120

#### Step 26 -- Compliance-Aware Routing (Step 9 revisited)

- **What**: Route requests to specific providers based on data classification. Federal data stays on FedRAMP-authorized endpoints. Non-sensitive data can go to cheaper providers.
- **Config**: Routing rules in `config.toml [modules.routing]` with classification -> provider mapping
- **Why deferred originally**: Didn't have enough providers. Now with 12 providers, routing becomes valuable.
- **File**: `modules/routing.py` (~150 LOC)
- **LOC estimate**: ~150

---

### Phase 8: Advanced Capabilities (NEW -- future)

#### Step 27 -- Model Discovery

- **What**: `list_models(provider)` returns available models with metadata. Useful for dynamic model selection and UI.
- **Provider support**: OpenAI (list models API), Anthropic (hardcoded -- no list API), Ollama (list API), others (from provider TOML)
- **LOC estimate**: ~80

#### Step 28 -- Batch API

- **What**: `model.batch(requests)` submits a batch for async processing. Returns batch ID for polling.
- **Provider support**: OpenAI (native batch API), Anthropic (native batch API)
- **LOC estimate**: ~150

#### Step 29 -- Guardrail Hooks

- **What**: Pre-invoke and post-invoke callback system for custom validation. Agents register guardrail functions that run before/after every invoke.
- **API**: `load_model("anthropic", guardrails=[my_pii_check, my_toxicity_check])`
- **Different from modules**: Guardrails are agent-provided functions, not config-driven modules. They're custom per-agent.
- **LOC estimate**: ~80

#### Step 30 -- Multimodal Expansion

- **What**: Audio input (Gemini, OpenAI), video input (Gemini), document/PDF input.
- **Types**: Add `AudioBlock`, `VideoBlock`, `DocumentBlock` to ContentBlock union
- **Provider support**: Provider-specific, graceful degradation for unsupported types
- **LOC estimate**: ~200

---

### Phase 9: Compliance Documentation

| Deliverable | What | NIST |
|-------------|------|------|
| SBOM generation | `pip-audit` + `cyclonedx-bom` in CI | SA-10, LLM03 |
| Threat model document | Data flow diagrams, trust boundaries, attack surfaces | RA-3, RA-5 |
| IR runbook | Detection criteria mapped to telemetry/audit fields | IR-1, IR-4, IR-5, IR-6 |

---

## Can ArcLLM Replace LiteLLM / pi-ai?

### Today: No

Missing streaming and structured output are dealbreakers. Any package that calls `arcllm.load_model()` gets only synchronous `invoke()` -- no way to stream responses or request JSON-constrained output.

### After Phase 6 (Steps 19-20): Partial

With streaming + structured output, ArcLLM covers the core LLM call surface. arcagent and arcrun can use it as their sole LLM layer. But external packages looking for a LiteLLM drop-in would still miss embeddings, caching, and model discovery.

### After Phase 7 (Steps 21-26): Yes

With prompt caching, thinking params, token counting, embeddings, response caching, and compliance routing, ArcLLM exceeds LiteLLM's feature set in security while matching it in functionality. At this point it's a credible unified LLM layer for any Python project.

### What ArcLLM Already Does Better Than All Competitors

| Capability | ArcLLM | LiteLLM | pi-ai |
|-----------|--------|---------|-------|
| PII redaction (both directions) | Built | No | No |
| Request signing (HMAC-SHA256) | Built | No | No |
| Vault-backed API keys | Built | No | No |
| mTLS OTel export | Built | No | No |
| Audit trail (PII-safe) | Built | No | No |
| OWASP/NIST compliance mapping | Documented | No | No |
| Federal/FedRAMP readiness | Core design principle | No | No |

### What pi-ai Does That ArcLLM Should Learn From

| Capability | pi-ai | ArcLLM |
|-----------|-------|--------|
| Cross-provider context handoffs | Built (serializable context) | Not yet (Step TBD) |
| Auto model discovery | Built (18+ providers) | Not yet (Step 27) |
| TypeBox schema tool validation | Built (compile-time + runtime) | Planned (Step 17, JSON Schema) |
| Streaming thinking content | Built | Not yet (Steps 19 + 22) |
| Partial JSON streaming for tool args | Built | Not yet (Step 19) |

---

## Priority Order

| Priority | Steps | Rationale |
|----------|-------|-----------|
| **P0** | 12 (Budget), 19 (Streaming), 20 (Structured Output) | Budget completes existing plan. Streaming + structured output are blocking gaps. |
| **P1** | 15.1 (ECDSA), 16 (Integration Test), 21-22 (Caching + Thinking params) | Complete existing stubs, enable extended model capabilities |
| **P2** | 17-18 (Tool Validator + Content Scanner), 23-24 (Token Counting + Embeddings) | Security hardening + expand API surface |
| **P3** | 25-26 (Response Cache + Routing) | Operational efficiency |
| **P4** | 27-30 (Discovery, Batch, Guardrails, Multimodal) | Competitive completeness |
| **P5** | Phase 9 (SBOM, Threat Model, IR Runbook) | Documentation after all code complete |
