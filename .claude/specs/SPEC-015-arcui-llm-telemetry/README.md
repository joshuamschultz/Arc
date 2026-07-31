# SPEC-015: ArcUI LLM Telemetry

## Metadata

| Field | Value |
|-------|-------|
| **ID** | SPEC-015 |
| **Feature** | arcui-llm-telemetry |
| **Type** | Integration (cross-package: ArcLLM + ArcUI + ArcAgent bridge) |
| **Status** | PENDING |
| **Created** | 2026-03-01 |
| **Author** | Claude (from /brainstorm → /build → /deepen → /specify) |
| **Priority** | High |
| **Confidence** | 95% (fast-track: build decisions + deepened research) |

## Prior Work

| Source | Location | Status |
|--------|----------|--------|
| Brainstorm | Inline conversation | Complete |
| Build decisions | `.claude/decisions-log.md` § "ArcUI LLM Telemetry" | 34 decisions |
| Deepen research | Embedded in decisions-log.md `### Research Insights` | 6 agents |
| Build state | `.claude/builds/arcui-llm-telemetry/state.json` | Complete |
| Demo reference | `demo/` (15 HTML/CSS/JS files from S3) | Available |

## Scope

**Three packages touched:**

1. **ArcLLM** (changes) — TraceStore, on_event callback, ConfigController, CircuitBreakerModule, span sub-phase timing, raw body capture
2. **ArcUI** (new package) — Starlette server, WebSocket, REST API, vanilla JS dashboard
3. **ArcAgent** (bridge only) — `create_arcllm_bridge()` wiring

## Key Decisions

All 34 decisions logged in `.claude/decisions-log.md`. Critical ones:

- **D-8**: TraceRecord includes raw request/response bodies (configurable per tier)
- **D-3**: `on_event` callback on `load_model()` matching ArcRun's pattern
- **D-4**: ConfigController lives in ArcLLM
- **D-30**: New CircuitBreakerModule for per-provider health state
- **D-29**: Span sub-phase timing (prompt, tokens, LLM, tool, post)
- **D-6**: WebSocket JSON messages with `type` field
- **D-23**: Vanilla HTML/CSS/JS (zero build step)

## Solutions Referenced

- `security-issues/` — reviewed for audit trail patterns

## Learnings

_Updated during implementation._
