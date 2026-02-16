# Spec: 005-openai-adapter

## Metadata

| Field | Value |
|-------|-------|
| **ID** | 005 |
| **Name** | OpenAI Adapter + StopReason Normalization |
| **Type** | Library/Backend |
| **Status** | PENDING |
| **Created** | 2026-02-07 |
| **Confidence** | High (>70%) — BaseAdapter pattern proven, OpenAI API well-documented |

## Summary

Second LLM adapter for ArcLLM. Translates between ArcLLM's universal types and OpenAI's Chat Completions API. Validates the abstraction by proving the same `invoke(messages, tools)` contract works across providers. Introduces `StopReason` type normalization so agents write provider-agnostic stop checks. Also introduces tool result message flattening (one ArcLLM message with multiple ToolResultBlocks expands to multiple OpenAI messages).

## Source

ArcLLM Build Step 5. Decisions made interactively via `/build-arcllm 5` session.

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| Stop reason normalization | Canonical `StopReason` Literal type | Whole point of unified interface — agents check one set of values regardless of provider | 2026-02-07 |
| Stop reason values | `Literal["end_turn", "tool_use", "max_tokens", "stop_sequence"]` | Matches Anthropic's native values (already in use), OpenAI maps to them | 2026-02-07 |
| Tool result message format | Flatten in adapter — one ArcLLM message with N ToolResultBlocks becomes N OpenAI messages | Agents use same ToolResultBlock pattern regardless of provider. Adapter owns translation complexity. | 2026-02-07 |
| Adapter structure | Mirror Anthropic adapter pattern — private methods per concern | Proven pattern from Step 3. Each method independently testable. | 2026-02-07 |

## Learnings

(To be filled during implementation)

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Step 3 Spec: `.claude/specs/003-anthropic-adapter/`
- Anthropic Adapter: `src/arcllm/adapters/anthropic.py`
- Base Adapter: `src/arcllm/adapters/base.py`
- Master Prompt: `/Users/joshschultz/AI/arcllm/docs/arcllm-master-prompt.md`
- Product PRD: `/Users/joshschultz/AI/arcllm/docs/arcllm-prd.md`
- Decision Log: `/Users/joshschultz/AI/arcllm/.claude/decision-log.md`
- Steering: `/Users/joshschultz/AI/arcllm/.claude/steering/`
