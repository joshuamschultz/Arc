# PRD: Azure OpenAI Provider (Azure AI Foundry)

## Problem Statement

ArcLLM supports 13 providers but none for Azure OpenAI Service / Azure AI Foundry. For deploying ArcAgent on Azure GCC (Government Community Cloud), we need a provider that handles Azure's unique URL construction, `api-key` header authentication, deployment-based model routing, and GCC-specific endpoints (`.openai.azure.us`). Without this, ArcAgent cannot use any LLM when running in Azure government environments.

## Requirements

### Functional Requirements

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-1 | Users can load Azure models via `load_model("azure_openai", "my-deployment")` | Must |
| FR-2 | Provider TOML supports configurable `base_url` for GCC vs commercial endpoints | Must |
| FR-3 | Auth uses `api-key` header (not `Authorization: Bearer`) | Must |
| FR-4 | URL construction uses v1 API path: `{base}/openai/v1/chat/completions` | Must |
| FR-5 | Legacy deployment URL path supported: `{base}/openai/deployments/{deployment}/chat/completions?api-version=X` | Should |
| FR-6 | `model` field in request body maps to deployment name | Must |
| FR-7 | `content_filter` finish_reason mapped to ArcLLM StopReason | Must |
| FR-8 | Tool calling works identically to OpenAI adapter (inherited) | Must |
| FR-9 | Lazy import registered in `__init__.py` | Must |
| FR-10 | TOML includes GCC-available model metadata (gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4.1-mini) | Must |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|-------------|-----------|
| NFR-1 | Zero additional dependencies (uses existing httpx) | 0 new packages |
| NFR-2 | Adapter LOC | <= 60 lines |
| NFR-3 | TOML validates against `ProviderConfig` schema | Pass |
| NFR-4 | All existing tests continue passing | 0 regressions |
| NFR-5 | HTTPS enforced for all non-localhost endpoints | Pass |

## Success Criteria

1. `load_model("azure_openai", "my-gpt4o-deployment")` returns a working adapter
2. Requests go to `{base_url}/openai/v1/chat/completions` with `api-key` header
3. `content_filter` finish_reason is handled gracefully
4. All parametrized provider tests pass with azure_openai included
5. `mypy --strict` passes
6. `ruff check` passes

## Out of Scope

- Azure AD / Managed Identity authentication (future enhancement — requires `azure-identity` dependency)
- Azure-specific `data_sources` field for RAG (On Your Data)
- Streaming support (same status as other adapters)
- Azure Content Safety API integration
- Automatic deployment name discovery
