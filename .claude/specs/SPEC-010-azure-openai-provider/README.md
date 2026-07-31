# SPEC-010: Azure OpenAI Provider (Azure AI Foundry)

| Field | Value |
|-------|-------|
| **ID** | SPEC-010 |
| **Feature** | Azure OpenAI Provider |
| **Type** | Integration |
| **Status** | COMPLETE |
| **Confidence** | 90% |
| **Route** | Fast-track |
| **Created** | 2026-02-25 |

## Context

ArcLLM needs to support Azure OpenAI Service models deployed via Azure AI Foundry, specifically targeting Azure GCC (Government Community Cloud) endpoints. Azure OpenAI uses an OpenAI-compatible API but with different URL construction, authentication headers, and deployment-based model routing.

This follows the established adapter pattern but requires more than a thin alias — the URL construction, auth headers, and deployment name handling all differ from standard OpenAI.

## Key Decisions

- Provider name: `azure_openai` (underscore convention, matches `base_url` domain pattern)
- Adapter class: `Azure_openaiAdapter` (convention: `{provider.title()}Adapter`, but see note below)
- Special handling: Override class name to `AzureOpenaiAdapter` for readability
- API strategy: Support v1 API (`/openai/v1/`) as primary, legacy deployment API as fallback
- Auth: `api-key` header (not `Authorization: Bearer`)
- GCC support: Configurable base_url — `.openai.azure.us` for GCC, `.openai.azure.com` for commercial
- Model field: Maps to deployment name, not canonical model name
- Config fields: `deployment_name`, `api_version` (for legacy API path)

## Prior Research

- Azure OpenAI v1 API (GA since August 2025): `{base}/openai/v1/chat/completions` — no api-version needed
- Legacy API: `{base}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21`
- Auth header: `api-key: {key}` (not Bearer token)
- GCC endpoints: `https://{resource}.openai.azure.us`
- Available GCC models: gpt-4.1, gpt-4.1-mini, gpt-4o, gpt-4o-mini, o3-mini
- Tool calling: Identical to OpenAI (1024 char description limit is only Azure difference)
- Request/response format: Identical except `content_filter` finish_reason

## Deepened: 2026-02-25

Research enrichment applied to SDD and PLAN with findings on:
- v1 API hard-fails on `api-version` query param (400 Bad Request)
- `api-key` header is case-sensitive (lowercase required)
- Content filter returns `null` content + `content_filter_results` object
- Per-deployment rate limits with `Retry-After` header
- Azure-specific error codes (`DeploymentNotFound`, `ResponsibleAIPolicyViolation`)
- Class naming convention (`Azure_openaiAdapter` with `# noqa: N801`) confirmed via `Huggingface_TgiAdapter` precedent
- DeploymentNotFound 404 during 5-min propagation window

## Learnings

- **Class name is `Azure_OpenaiAdapter`**, not `Azure_openaiAdapter` as originally planned. Python's `.title()` capitalizes after underscores: `"azure_openai".title()` → `"Azure_Openai"`. The registry uses `f"{provider_name.title()}Adapter"`, so the class must be `Azure_OpenaiAdapter` to match. The SDD/PLAN incorrectly specified `Azure_openaiAdapter`.
- **`rstrip('/')` on base_url** prevents double-slash when users configure trailing slash. Added to `invoke()`.
- **13 Azure-specific tests** cover: name property, api-key header presence, no Authorization header, empty key handling, v1 URL path, no query params, trailing slash safety, deployment name in body, content_filter finish_reason, prompt blocked (400), 429 retry-after, 404 deployment not found, successful round-trip.
