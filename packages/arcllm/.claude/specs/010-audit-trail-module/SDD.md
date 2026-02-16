# SDD: Audit Trail Module

> System design for ArcLLM Step 11.
> References steering docs in `.claude/steering/`.

---

## Design Overview

Step 11 adds the audit module to the system: a structured logger that captures per-invoke() metadata for compliance, debugging, and operational analysis. Unlike telemetry (which focuses on timing and cost), audit focuses on **what happened** — who called what, with how many messages, what was the outcome, how many tools were involved.

Key design insight: audit must be PII-safe by default. In federal environments, audit logs may be stored in less-secured systems than the actual data. Logging raw message content would turn the audit trail into a PII exposure vector. The default mode logs only metadata (counts, lengths, identifiers). Content logging is explicitly opt-in for dev/staging.

Design priorities:
1. **PII safety** — no raw content by default, opt-in only
2. **Compliance** — every invoke() gets an audit record (NIST 800-53 AU-3)
3. **Conditional fields** — tool info only logged when relevant
4. **Consistency** — same log_level validation pattern as TelemetryModule

---

## Directory Map

```
src/arcllm/
├── modules/
│   ├── __init__.py                    # MODIFY: Add AuditModule export
│   ├── base.py                        # UNCHANGED
│   ├── retry.py                       # UNCHANGED
│   ├── fallback.py                    # UNCHANGED
│   ├── rate_limit.py                  # UNCHANGED
│   ├── telemetry.py                   # UNCHANGED
│   └── audit.py                       # NEW: AuditModule
├── registry.py                        # MODIFY: Add audit= kwarg
├── __init__.py                        # MODIFY: Add AuditModule to lazy imports
tests/
├── test_audit.py                      # NEW: Full test suite (written, RED state)
├── test_registry.py                   # MODIFY: Add audit stacking tests
```

---

## Component Design

### 1. AuditModule (`modules/audit.py`)

A BaseModule subclass that wraps invoke() to log request/response metadata for audit compliance.

| Attribute | Type | Purpose |
|-----------|------|---------|
| `_include_messages` | `bool` | Whether to log raw message content (default: False) |
| `_include_response` | `bool` | Whether to log raw response content (default: False) |
| `_log_level` | `int` | Python logging level for audit records (default: logging.INFO) |

| Method | Purpose |
|--------|---------|
| `__init__(config, inner)` | Validate config, extract settings |
| `async invoke(messages, tools, **kwargs) -> LLMResponse` | Delegate, log audit metadata, optionally log content |

#### Configuration

From `config.toml [modules.audit]`:
```toml
[modules.audit]
enabled = false
```

Additional config keys (via kwarg dict):
- `include_messages` (bool, default False): Log raw message content at DEBUG level
- `include_response` (bool, default False): Log raw response content at DEBUG level
- `log_level` (str, default "INFO"): Python log level name for audit records

#### Logic Flow

```
__init__(config, inner):
    super().__init__(config, inner)
    _include_messages = config.get("include_messages", False)
    _include_response = config.get("include_response", False)
    log_level_name = config.get("log_level", "INFO")
    validate: log_level_name in {DEBUG, INFO, WARNING, ERROR, CRITICAL}
    _log_level = getattr(logging, log_level_name)

invoke(messages, tools, **kwargs):
    response = await inner.invoke(messages, tools, **kwargs)

    # Build audit log parts
    parts = [
        f"provider={inner.name}",
        f"model={response.model}",
        f"message_count={len(messages)}",
        f"stop_reason={response.stop_reason}",
    ]

    # Conditional: tools provided
    if tools is not None:
        parts.append(f"tools_provided={len(tools)}")

    # Conditional: tool calls in response
    if response.tool_calls:
        parts.append(f"tool_calls={len(response.tool_calls)}")

    # Content length (safe metric — not the content itself)
    content_length = len(response.content) if response.content else 0
    parts.append(f"content_length={content_length}")

    logger.log(_log_level, "Audit | %s", " ".join(parts))

    # Optional: raw content logging (PII opt-in, DEBUG level)
    if _include_messages:
        logger.debug("Audit messages | %s", messages)
    if _include_response:
        logger.debug("Audit response | %s", response)

    return response
```

### 2. Registry Integration (`registry.py` changes)

#### load_model() signature change

```python
def load_model(
    provider: str,
    model: str | None = None,
    *,
    retry: bool | dict | None = None,
    fallback: bool | dict | None = None,
    rate_limit: bool | dict | None = None,
    telemetry: bool | dict | None = None,
    audit: bool | dict | None = None,  # NEW
) -> LLMProvider:
```

#### Stacking order change

Current (Step 10): `Telemetry(Retry(Fallback(RateLimit(adapter))))`
New (Step 11): `Telemetry(Audit(Retry(Fallback(RateLimit(adapter)))))`

Audit sits between telemetry (outermost) and retry. This means:
- Audit captures metadata from the successful response (after retries resolve)
- Telemetry wraps everything for total wall-clock
- Wait — reviewing test_audit.py, the tests show audit wrapping inner directly and getting the response from inner.invoke(). The stacking puts audit between telemetry and retry.

```python
# Stacking order (innermost first):
# 1. Rate limit (closest to adapter)
# 2. Fallback
# 3. Retry
# 4. Audit (captures post-retry successful response metadata)
# 5. Telemetry (outermost — total wall-clock)

rate_limit_config = _resolve_module_config("rate_limit", rate_limit)
if rate_limit_config is not None:
    result = RateLimitModule(rate_limit_config, result)

fallback_config = _resolve_module_config("fallback", fallback)
if fallback_config is not None:
    result = FallbackModule(fallback_config, result)

retry_config = _resolve_module_config("retry", retry)
if retry_config is not None:
    result = RetryModule(retry_config, result)

audit_config = _resolve_module_config("audit", audit)
if audit_config is not None:
    result = AuditModule(audit_config, result)

telemetry_config = _resolve_module_config("telemetry", telemetry)
if telemetry_config is not None:
    # ... pricing injection ...
    result = TelemetryModule(telemetry_config, result)
```

### 3. Config (No changes needed)

The existing `[modules.audit]` section in config.toml already has `enabled = false`. No additional config keys are needed in TOML — `include_messages`, `include_response`, and `log_level` are passed via the kwarg dict or default to safe values.

```toml
[modules.audit]
enabled = false
```

---

## ADRs

### ADR-029: PII-Safe Audit by Default

**Context**: Audit logs in federal environments may be stored in systems with different classification levels. Logging raw message content would create PII exposure.

**Decision**: AuditModule logs only metadata by default (provider, model, message_count, stop_reason, content_length, tool counts). Raw content is opt-in via `include_messages` and `include_response` flags, logged at DEBUG level.

**Rationale**: NIST 800-53 AU-3 requires audit records to contain enough information to reconstruct events, but not necessarily the full data. Message count, content length, and tool information provide operational context. Content logging should be an explicit decision per-environment (dev/staging yes, production no).

**Alternatives rejected**:
- Always log content — unacceptable for classified environments
- Truncate content — still exposes partial PII, harder to reason about
- Separate content log file — adds complexity, still creates exposure

### ADR-030: Content Logging at DEBUG Level

**Context**: When content opt-in is enabled, what log level should raw content be logged at?

**Decision**: Raw message and response content logged at DEBUG level, separate from the main audit record (which uses the configured log_level, default INFO).

**Rationale**: In production, DEBUG is typically disabled. Enabling `include_messages=True` in config means the content is available when someone also enables DEBUG logging — double opt-in provides additional safety. The main audit record (metadata only) uses the configured log level for visibility.

### ADR-031: Audit Between Telemetry and Retry

**Context**: Where should audit sit in the module stack?

**Decision**: Between telemetry (outermost) and retry: `Telemetry(Audit(Retry(...)))`.

**Rationale**: Audit should capture the final successful response metadata (after retries resolve). Telemetry wraps everything for total wall-clock timing. Audit doesn't need to know about retries — it records the final outcome. If audit were inside retry, it would log failed attempts (which is noise for compliance purposes).

**Alternatives rejected**:
- Inside retry — would log failed attempts, noisy
- Outermost — would duplicate timing concerns with telemetry
- Innermost — would miss fallback provider information

### ADR-032: Conditional Tool Fields

**Context**: Should tool-related fields always be present in audit logs, even when no tools are involved?

**Decision**: `tools_provided` logged only when tools arg is not None. `tool_calls` logged only when response has tool_calls. Both omitted otherwise.

**Rationale**: Most calls don't involve tools. Including `tools_provided=0` and `tool_calls=0` on every audit line adds noise. Conditional fields make tool-using calls stand out in logs, which is useful for identifying agentic vs. simple chat patterns.

---

## Edge Cases

| Case | Handling |
|------|----------|
| No tools provided | `tools_provided` field omitted from log |
| Tools provided but no tool calls in response | `tools_provided=N` logged, `tool_calls` omitted |
| Response content is None (tool_use response) | `content_length=0` |
| Response content is empty string | `content_length=0` |
| include_messages=True but DEBUG disabled | Content logged at DEBUG level — not visible unless DEBUG enabled |
| include_response=True but DEBUG disabled | Content logged at DEBUG level — not visible unless DEBUG enabled |
| include_messages and include_response both True | Both logged separately at DEBUG level |
| Invalid log_level | `ArcLLMConfigError` raised at construction |
| audit=False kwarg | Disables even if config.toml has enabled=true |
| Inner provider raises exception | Exception propagates — no audit log (call failed) |
| Very large message list (1000+) | Only logs `message_count=1000`, not the messages themselves |
| Message content contains PII | NOT logged by default — only message_count appears |

---

## Test Strategy

One new test file + additions to test_registry.py.

| File | Tests | Priority |
|------|-------|----------|
| `test_audit.py` | AuditModule (all scenarios) | P0 |
| `test_registry.py` (additions) | Audit stacking integration | P0 |

### Key Test Scenarios

**AuditModule Core (TestAuditModule):**
- `test_invoke_delegates_to_inner` — messages passed, response returned
- `test_invoke_passes_tools_and_kwargs` — tools, max_tokens forwarded
- `test_returns_response_unchanged` — same object reference
- `test_logs_basic_audit_fields` — provider, model, message_count, stop_reason in log
- `test_logs_tool_info_when_tools_provided` — tools_provided=N in log
- `test_logs_no_tools_field_when_none` — tools_provided omitted
- `test_logs_tool_call_count` — tool_calls=N when response has tool calls
- `test_logs_content_length` — content_length=12 for "Hello there!"
- `test_content_length_zero_when_none` — content_length=0 when content is None
- `test_no_messages_logged_by_default` — PII safety: raw message NOT in log
- `test_no_response_logged_by_default` — PII safety: raw response NOT in log

**Content Logging (TestAuditContentLogging):**
- `test_include_messages_logs_message_content` — content appears at DEBUG
- `test_include_response_logs_response_content` — response appears at DEBUG
- `test_include_both` — both message and response content logged

**Log Level (TestAuditLogLevel):**
- `test_default_log_level_is_info` — audit visible at INFO
- `test_custom_log_level` — DEBUG not visible at INFO, visible at DEBUG
- `test_invalid_log_level_rejected` — ArcLLMConfigError

**Provider Info (TestAuditProviderInfo):**
- `test_provider_name_from_inner` — module.name == inner.name
- `test_model_name_from_inner` — module.model_name == inner.model_name

**Registry Integration:**
- `test_load_model_with_audit` — wraps with AuditModule
- `test_load_model_audit_false_overrides_config` — kwarg disables
- `test_load_model_full_stack_with_audit` — Telemetry(Audit(Retry(Fallback(RateLimit(adapter)))))
