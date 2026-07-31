# Spec: 010-audit-trail-module

## Metadata

| Field | Value |
|-------|-------|
| **ID** | 010 |
| **Name** | Audit Trail Module |
| **Type** | Library/Backend |
| **Status** | COMPLETE |
| **Created** | 2026-02-08 |
| **Confidence** | High (>70%) — Module pattern established in 007-009, all decisions made |

## Summary

Implements the AuditModule for ArcLLM — a structured audit logger that captures request/response metadata per `invoke()` call for compliance and debugging. Designed PII-safe by default: no raw message content or response content is logged unless explicitly opted in via `include_messages` and `include_response` config flags. Audit sits between telemetry (outermost) and retry in the stack: `Telemetry(Audit(Retry(Fallback(RateLimit(adapter)))))`. Logs provider, model, message count, stop reason, tool info (conditional), tool call count (conditional), and content length. Configurable log level (default INFO). Federal compliance use case: every LLM interaction has an audit trail without exposing PII.

## Source

ArcLLM Build Step 11. Decisions made interactively via `/build-arcllm` session.

## Decisions Log

| Decision | Choice | Rationale | Date |
|----------|--------|-----------|------|
| D-070 Output | Structured logging (same pattern as telemetry) | Consistent with telemetry, ops-friendly, parseable by log aggregation systems. | 2026-02-08 |
| D-071 Log level | INFO by default, configurable via log_level | Audit events should be visible by default. Same validation pattern as TelemetryModule. | 2026-02-08 |
| D-072 Audit fields | provider, model, message_count, stop_reason, tools_provided (conditional), tool_calls (conditional), content_length | Covers all compliance-relevant metadata without PII exposure. | 2026-02-08 |
| D-073 PII safety | No raw content by default — messages and response content NOT logged | Federal compliance (NIST 800-53 AU-3): audit trail must exist but must not create new PII exposure. | 2026-02-08 |
| D-074 Content opt-in | include_messages and include_response boolean flags | Explicit opt-in for environments where logging raw content is acceptable (dev, staging). Logged at DEBUG level. | 2026-02-08 |

## Learnings

- Using the shared `log_structured()` helper from `_logging.py` made audit implementation trivial — 85 lines total, most of which is config validation.
- Conditional fields via `None` omission in `log_structured()` is elegant — `tools_provided` and `tool_calls` just pass `None` when not applicable.
- PII safety test pattern: check at DEBUG level that content does NOT appear. Important to test at the most permissive log level.
- `_VALID_CONFIG_KEYS` pattern (from telemetry review) applied here too — catches `include_mesages` typos at construction.
- Content logging at DEBUG creates double opt-in: config flag must be True AND DEBUG must be enabled. Good safety design for federal environments.
- **Review fix**: Extracted shared `validate_log_level()` to `_logging.py` — eliminates DRY violation between telemetry and audit (both had identical `_VALID_LOG_LEVELS` + validation logic).
- **Review fix**: Added `isEnabledFor()` guard in `log_structured()` — prevents string building when logging is disabled. Reduces overhead from ~0.15ms to ~0.001ms when level is off.
- **Review fix**: Opt-in content logging now uses `_sanitize()` + `isEnabledFor(DEBUG)` guard — prevents log injection via crafted message/response content and avoids expensive serialization when DEBUG is disabled.
- **Review fix**: 7 additional edge case tests (exception propagation, empty tools list, empty tool_calls list, unknown config key rejection, model name sanitization, include_response with None, content logging sanitization).
- **Review fix**: 5 additional tests for `validate_log_level()` shared helper.

## Cross-References

- PRD: `PRD.md` (this directory)
- SDD: `SDD.md` (this directory)
- PLAN: `PLAN.md` (this directory)
- Step 9 Spec: `.claude/specs/009-telemetry-module/`
- Step 8 Spec: `.claude/specs/008-rate-limiter/`
- Step 7 Spec: `.claude/specs/007-module-system-retry-fallback/`
- Registry: `src/arcllm/registry.py`
- BaseModule: `src/arcllm/modules/base.py`
- TelemetryModule: `src/arcllm/modules/telemetry.py`
- Config: `src/arcllm/config.toml` (`[modules.audit]`)
- Implementation: `src/arcllm/modules/audit.py`
- Tests: `tests/test_audit.py` (26 tests, GREEN)
- Product PRD: `docs/arcllm-prd.md`
- Decision Log: `.claude/decision-log.md`
- Steering: `.claude/steering/`
