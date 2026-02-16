# Spec 013 — Open Model Providers

## Metadata

| Field | Value |
|-------|-------|
| Spec ID | 013 |
| Step | 15 |
| Feature | Open Model Providers (Ollama, vLLM, Together, Groq, Fireworks, DeepSeek, Mistral, HuggingFace, HuggingFace TGI) |
| Status | COMPLETE |
| Created | 2026-02-11 |
| Author | Josh + Claude |

## Documents

| Document | Purpose |
|----------|---------|
| [PRD.md](PRD.md) | Problem, goals, requirements, user stories |
| [SDD.md](SDD.md) | Design, components, ADRs, edge cases |
| [PLAN.md](PLAN.md) | Phased tasks with checkboxes and acceptance criteria |

## Decisions Log

| ID | Decision | Rationale |
|----|----------|-----------|
| D-100 | Thin alias adapters for OpenAI-compatible providers | Preserves convention-based registry (D-041/D-042). Each provider gets a small file inheriting OpenaiAdapter with name override. |
| D-101 | `api_key_required` flag in ProviderSettings TOML | Explicit boolean in `[provider]` section. BaseAdapter skips auth validation when false. Still reads env var if set (optional auth). |
| D-102 | All 10 providers: Ollama, vLLM, Together, Groq, Fireworks, DeepSeek, Mistral, HuggingFace, HuggingFace TGI | Full open-model coverage. Local (air-gapped/federal) + cloud (cost optimization, model diversity). |
| D-103 | Common models pre-populated, graceful defaults for unknown | Popular models in TOML with known metadata. Missing models use adapter defaults (context_window not enforced). |
| D-104 | Zero cost defaults for local providers | cost_*_per_1m = 0.0 in local TOMLs. Orgs can override for GPU cost tracking. Telemetry still logs tokens. |
| D-105 | Mistral gets quirk overrides | Override tool_choice mapping ("required" → "any"), stop_reason mapping. Only provider that isn't a pure alias. |
| D-106 | `huggingface` + `huggingface_tgi` naming | Separate providers: cloud Inference API vs self-hosted TGI. Different auth, base_url, model naming. |

## Cross-References

- Prior decisions: D-041 (convention-based registry), D-042 (class name convention), D-044 (no kwargs)
- Related specs: 005-openai-adapter (base for all aliases), 007-fallback-retry (fallback chains across providers)
- PRD: Section "Competitive Context" — LiteLLM comparison (provider breadth)

## Learnings

- TOML model names with dots (e.g., `llama3.2`) must be quoted: `[models."llama3.2"]` — unquoted dots are parsed as nested tables
- 8 of 9 providers are pure OpenAI-compatible aliases (~10 lines each). Only Mistral needed quirk overrides.
- `api_key_required` flag required changes in only 2 places: BaseAdapter.__init__() conditional + OpenaiAdapter._build_headers() conditional
- HuggingFace TGI adapter class naming: `"huggingface_tgi".title()` = `"Huggingface_Tgi"` → `Huggingface_TgiAdapter` — valid Python, works with convention
- 83 new tests (69 parametrized in test_open_providers.py + 14 in test_mistral.py), total 538 passing
