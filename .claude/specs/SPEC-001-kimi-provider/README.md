# SPEC-001: Kimi (Moonshot AI) Provider Support

| Field | Value |
|-------|-------|
| **ID** | SPEC-001 |
| **Feature** | Kimi/Moonshot AI Provider |
| **Type** | Integration |
| **Status** | PENDING |
| **Confidence** | 95% |
| **Route** | Fast-track |
| **Created** | 2026-02-16 |

## Context

Kimi is an LLM by Moonshot AI with an OpenAI-compatible API. This follows the exact same thin-alias pattern used by DeepSeek, Groq, Together, and Fireworks adapters.

## Key Decisions

- Provider name: `moonshot` (company name, consistent with API domain `api.moonshot.ai`)
- Adapter class: `MoonshotAdapter` (thin alias over `OpenaiAdapter`)
- API key env var: `MOONSHOT_API_KEY`
- Base URL: `https://api.moonshot.ai` (global endpoint)
- Models: `kimi-k2`, `kimi-k2.5` (current generation)

## Prior Research

- Kimi API is fully OpenAI-compatible (same endpoint format, auth, streaming, tool calling)
- Base URL: `https://api.moonshot.ai/v1`
- Pricing: $0.60/M input, $2.50/M output, $0.15/M cached
- Context: up to 256K tokens
- Supports: tools, vision (K2.5), thinking mode (kimi-k2-thinking)

## Learnings

(Updated during implementation)
