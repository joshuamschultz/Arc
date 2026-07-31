# PRD: Audit Trail Module

> Feature-specific requirements for ArcLLM Step 11.
> References steering docs in `.claude/steering/`.

---

## Feature Overview

### Problem Statement

In federal production environments (BlackArc Systems, CTG Federal), every LLM interaction must have an audit trail for compliance, incident response, and forensic analysis. Without audit logging, operators cannot answer: "Which agent called which model, when, with how many messages, and what was the outcome?" Currently ArcLLM has telemetry (timing, tokens, cost) but no audit-specific logging that captures request/response metadata. Critically, audit logging must be PII-safe by default — logging raw message content would create a new data exposure vector in classified or PII-sensitive environments.

### Goal

1. **Implement an AuditModule** that logs request/response metadata per `invoke()` call — provider, model, message count, stop reason, tool info, content length.
2. **PII-safe by default** — no raw message content or response content logged unless explicitly opted in.
3. **Opt-in content logging** via `include_messages` (logs input message content) and `include_response` (logs response content), both at DEBUG level.
4. **Integrate in the module stack** between telemetry and retry: `Telemetry(Audit(Retry(Fallback(RateLimit(adapter)))))`.
5. **Configurable log level** (default INFO), same validated pattern as TelemetryModule.

### Success Criteria

- `AuditModule` wraps any `LLMProvider` and logs audit metadata after each `invoke()` call
- Default audit log includes: provider, model, message_count, stop_reason, content_length
- Tools-provided count logged conditionally (only when tools are passed)
- Tool-call count logged conditionally (only when response has tool_calls)
- Raw message content NOT logged by default (PII safety)
- Raw response content NOT logged by default (PII safety)
- `include_messages=True` in config logs message content at DEBUG level
- `include_response=True` in config logs response content at DEBUG level
- `load_model("anthropic", audit=True)` enables audit with config.toml defaults
- `load_model("anthropic", audit={"include_messages": True})` overrides config
- Stacking order: `Telemetry(Audit(Retry(Fallback(RateLimit(adapter)))))`
- Configurable log level (default INFO), validated at construction
- All existing 272 tests pass unchanged
- New audit tests fully mocked (no real API calls)
- Zero new dependencies (uses stdlib `logging`)

---

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|------------|----------|------------|
| FR-1 | `AuditModule` wraps invoke() and logs audit metadata after each call | P0 | Structured log line emitted with audit fields |
| FR-2 | Log includes `provider` name from inner.name | P0 | Identifies which provider handled the request |
| FR-3 | Log includes `model` from response.model | P0 | Identifies which model was used |
| FR-4 | Log includes `message_count` = len(messages) | P0 | Request size metric |
| FR-5 | Log includes `stop_reason` from response | P0 | Outcome classification (end_turn, tool_use, etc.) |
| FR-6 | Log includes `content_length` = len(response.content or "") | P0 | Response size without exposing actual content |
| FR-7 | Log includes `tools_provided` count when tools arg is not None | P0 | Conditional: omitted when no tools |
| FR-8 | Log includes `tool_calls` count when response has tool_calls | P0 | Conditional: omitted when no tool calls |
| FR-9 | No raw message content logged by default | P0 | PII safety — "You are helpful" NOT in logs |
| FR-10 | No raw response content logged by default | P0 | PII safety — response text NOT in logs |
| FR-11 | `include_messages=True` logs message content at DEBUG level | P1 | Opt-in for dev/staging environments |
| FR-12 | `include_response=True` logs response content at DEBUG level | P1 | Opt-in for dev/staging environments |
| FR-13 | Configurable log level via `log_level` config key | P0 | Default "INFO", validated against standard Python levels |
| FR-14 | Invalid log level raises `ArcLLMConfigError` | P0 | Fail-fast at construction |
| FR-15 | Registry integration: `audit=` kwarg on `load_model()` | P0 | Same 4-level resolution as other modules |
| FR-16 | Stack order: between telemetry (outer) and retry (inner) | P0 | Captures audit data for successful calls, telemetry wraps for total timing |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|------------|-----------|
| NFR-1 | Audit overhead | <0.5ms per call (calls take 500-5000ms) |
| NFR-2 | Zero new dependencies | Uses stdlib `logging` |
| NFR-3 | All tests run without real API calls | Fully mocked |
| NFR-4 | Existing 272 tests unaffected | Zero regressions |
| NFR-5 | Module independently testable | No adapter setup needed |
| NFR-6 | Federal compliance | NIST 800-53 AU-3 (content of audit records), AU-11 (audit record retention) compatible |

---

## User Stories

### Compliance Officer

> As a compliance officer, I want every LLM interaction logged with provider, model, and outcome metadata so I can satisfy NIST 800-53 AU-3 audit record requirements without exposing PII.

### Agent Developer

> As an agent developer, I want `load_model("anthropic", audit=True)` to automatically create audit trails so I don't have to add logging to every agent.

### Platform Engineer

> As a platform engineer, I want to enable `include_messages` in staging environments so I can debug agent behavior by reviewing full request/response content.

### Operations

> As an ops engineer, I want audit logs with message_count and content_length so I can detect anomalous patterns (e.g., empty requests, unusually large responses) without accessing raw content.

### Security Analyst

> As a security analyst, I want audit logs that DON'T contain raw content by default so a log compromise doesn't expose classified or PII data.

---

## Out of Scope (Step 11)

- Audit log storage backend (files, database, SIEM integration)
- Audit log rotation and retention policies
- Cryptographic signing of audit records
- Correlation IDs linking audit entries across multi-turn conversations
- Per-field PII redaction (entire content is either included or excluded)
- Audit for streaming responses (not yet supported)
- Real-time audit alerting
- Audit record tamper detection

---

## Personas Referenced

- **Compliance Officer** (primary) — see `steering/product.md`
- **Agent Developer** (secondary) — see `steering/product.md`
- **Security Analyst** (tertiary) — see `steering/product.md`

---

## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Step 1-8, 10 (Core + Modules + Telemetry) | Prerequisite | COMPLETE |
| `BaseModule` | Base class | Defined in modules/base.py |
| `LLMProvider` ABC | Interface | Defined in types.py |
| `LLMResponse` type | Response model | Has content, tool_calls, usage, model, stop_reason |
| `Message` type | Request model | Has role and content |
| `ToolCall` type | Tool call model | Has id, name, arguments |
| `Tool` type | Tool definition model | Has name, description, parameters |
| `load_model()` registry | Integration point | Has module stacking from Step 7-10 |
| `config.toml [modules.audit]` | Config | Has `enabled = false` |
| `ArcLLMConfigError` | Exception type | Defined in exceptions.py |
