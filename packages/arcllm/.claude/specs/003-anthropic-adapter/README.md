# Spec: 003-anthropic-adapter

## Metadata

| Field | Value |
|-------|-------|
| **ID** | 003 |
| **Name** | Anthropic Adapter + Tool Support |
| **Type** | Library/Backend |
| **Status** | PENDING |
| **Created** | 2026-02-07 |
| **Confidence** | High (>70%) — all decisions made, Anthropic API well-documented |

## Summary

First LLM adapter for ArcLLM. Translates between ArcLLM's universal types and Anthropic's Messages API. Introduces `BaseAdapter` (shared plumbing for all adapters), `AnthropicAdapter` (Anthropic-specific translation), and `ArcLLMAPIError` (provider API error exception). Uses httpx for async HTTP with connection reuse.

## Source

ArcLLM Build Step 3. Decisions made interactively via `/build-arcllm 3` session.

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| Adapter init pattern | Config object injection (ProviderConfig + model name) | Clean separation — adapter doesn't know how config was loaded. Testable with fake configs. | 2026-02-07 |
| API key resolution | At adapter init (fail-fast) | Consistent with validate-on-load philosophy. Missing key caught at startup. | 2026-02-07 |
| Translation structure | Private methods per concern | Each method independently testable. Easy to update when API changes. | 2026-02-07 |
| HTTP client lifecycle | Created in __init__, adapter owns it, explicit close / async context manager | Connection reuse across agentic loop calls. Clean ownership. | 2026-02-07 |
| API error handling | New ArcLLMAPIError with status_code + body + provider | Clean abstraction, enables intelligent retry by status code. | 2026-02-07 |
| Base adapter class | Yes — concrete BaseAdapter(LLMProvider) with shared plumbing | DRY — three adapters share client, config, key resolution, cleanup. | 2026-02-07 |

## Learnings

(To be filled during implementation)

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Step 1 Spec: `.claude/specs/001/`
- Step 2 Spec: `.claude/specs/002-config-loading/`
- Master Prompt: `/Users/joshschultz/AI/arcllm/docs/arcllm-master-prompt.md`
- Product PRD: `/Users/joshschultz/AI/arcllm/docs/arcllm-prd.md`
- Decision Log: `/Users/joshschultz/AI/arcllm/.claude/decision-log.md`
- Steering: `/Users/joshschultz/AI/arcllm/.claude/steering/`
