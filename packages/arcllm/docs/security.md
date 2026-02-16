# ArcLLM Security Reference

ArcLLM is designed security-first for federal production environments where thousands of autonomous agents operate concurrently. Every security feature is opt-in, auditable, and maps to recognized compliance frameworks.

This document covers all security features, how to use them, and which NIST 800-53 controls and OWASP threats they address.

---

## Security Architecture Overview

```
Agent Request
    │
    ▼
┌──────────────┐
│  OtelModule  │  ── Distributed tracing with mTLS
├──────────────┤
│ TelemetryMod │  ── Cost/usage tracking per call
├──────────────┤
│  AuditModule │  ── Structured compliance logging (sees redacted data)
├──────────────┤
│ SecurityMod  │  ── PII redaction + request signing
├──────────────┤
│  RetryModule │  ── Retries with redacted+signed payloads
├──────────────┤
│FallbackModule│  ── Provider chain on failure
├──────────────┤
│RateLimitMod  │  ── Token-bucket throttling
├──────────────┤
│   Adapter    │  ── TLS-enforced HTTP via httpx
├──────────────┤
│ VaultResolver│  ── API key from vault with TTL cache
└──────────────┘
    │
    ▼
 LLM Provider (HTTPS)
```

Key principle: **Security wraps the call stack from outside in.** Audit sees only redacted data. Each retry sends a redacted, signed payload. Rate limiting prevents abuse before requests leave the process.

---

## Feature 1: API Key Isolation

### What It Does

API keys are never stored in configuration files, logged, or included in responses. Keys are resolved at runtime from environment variables or a vault backend.

### How to Use

**Environment variables (default):**

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

Each provider TOML declares which env var to read:

```toml
# providers/anthropic.toml
[provider]
api_key_env = "ANTHROPIC_API_KEY"
api_key_required = true
```

**Vault backend (enterprise):**

```toml
# config.toml
[vault]
backend = "my_vault:HashicorpBackend"
cache_ttl_seconds = 300
```

```toml
# providers/anthropic.toml
[provider]
api_key_env = "ANTHROPIC_API_KEY"
vault_path = "secret/data/llm/anthropic"
```

Resolution order:
1. Vault backend (if configured + vault_path set) — checks TTL cache first
2. Environment variable fallback
3. `ArcLLMConfigError` if neither source has the key

**Implementing a vault backend:**

```python
from arcllm.vault import VaultBackend

class MyVaultBackend:
    """Must implement the VaultBackend protocol."""

    def get_secret(self, path: str) -> str | None:
        """Fetch secret by path. Return None if not found."""
        # Your vault client logic here
        ...

    def is_available(self) -> bool:
        """Return True if vault is reachable."""
        ...
```

Register in config.toml as `"my_module:MyVaultBackend"`. ArcLLM imports and instantiates it automatically.

### Security Properties

- Keys exist only in memory after resolution
- TTL cache prevents repeated vault round-trips (default: 5 minutes)
- Vault unavailability falls back to env var gracefully (logged as warning)
- `raw` field on `LLMResponse` is excluded from serialization (`repr=False, exclude=True`)

---

## Feature 2: PII Redaction

### What It Does

Automatically detects and redacts personally identifiable information from both outbound messages (sent to LLM) and inbound responses (received from LLM). Redacted text is replaced with `[PII:TYPE]` placeholders.

### Built-In Detection Patterns

| PII Type | Pattern | Example Match |
|----------|---------|---------------|
| `SSN` | `\d{3}-\d{2}-\d{4}` | `123-45-6789` |
| `CREDIT_CARD` | `\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}` | `4111-1111-1111-1111` |
| `EMAIL` | Standard email regex | `user@example.com` |
| `PHONE` | US phone with optional country code | `(555) 123-4567` |
| `IPV4` | `\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}` | `192.168.1.1` |

### How to Use

```python
# Enable with defaults (all built-in patterns)
model = load_model("anthropic", security=True)

# Enable with custom patterns
model = load_model("anthropic", security={
    "pii_enabled": True,
    "pii_custom_patterns": [
        {"name": "EMPLOYEE_ID", "pattern": r"EMP-\d{6}"},
        {"name": "CASE_NUMBER", "pattern": r"CASE-\d{4}-\d{6}"},
    ],
})

# Disable PII but keep signing
model = load_model("anthropic", security={
    "pii_enabled": False,
    "signing_enabled": True,
})
```

Or in `config.toml`:

```toml
[modules.security]
enabled = true
pii_enabled = true
pii_detector = "regex"
pii_custom_patterns = [
    { name = "EMPLOYEE_ID", pattern = "EMP-\\d{6}" },
]
```

### Redaction Coverage

PII is scanned and redacted in:

- **Outbound**: All `Message.content` (string and `ContentBlock` lists), including `TextBlock.text`, `ToolResultBlock.content`, and `ToolUseBlock.arguments` (serialized as JSON)
- **Inbound**: `LLMResponse.content` text

### Security Properties

- Redaction happens before the request leaves the process (LLM never sees raw PII)
- Audit module sees only redacted data (Security wraps inside Audit in the stack)
- Non-overlapping match resolution — longer matches win when patterns overlap
- Custom patterns validated at init time — invalid regex raises `ArcLLMConfigError`
- Pluggable detector protocol (`PiiDetector`) for ML-based detection in the future

---

## Feature 3: Request Signing

### What It Does

Signs every request payload with HMAC-SHA256 (or optionally ECDSA P-256) to provide tamper detection and non-repudiation. The signature is attached to the `LLMResponse.metadata` for downstream verification.

### How to Use

```bash
# Set signing key
export ARCLLM_SIGNING_KEY=your-secret-signing-key
```

```python
# Enable with defaults (HMAC-SHA256)
model = load_model("anthropic", security=True)

# Custom signing config
model = load_model("anthropic", security={
    "signing_enabled": True,
    "signing_algorithm": "hmac-sha256",
    "signing_key_env": "MY_CUSTOM_SIGNING_KEY",
})
```

Or in `config.toml`:

```toml
[modules.security]
enabled = true
signing_enabled = true
signing_algorithm = "hmac-sha256"
signing_key_env = "ARCLLM_SIGNING_KEY"
```

### How Signing Works

1. Messages, tools, and model name are serialized to **canonical JSON** (sorted keys, compact separators)
2. The canonical payload is signed with HMAC-SHA256 using the key from the env var
3. The hex-encoded signature and algorithm are attached to `response.metadata`:

```python
response.metadata["request_signature"]   # "a1b2c3d4..."
response.metadata["signing_algorithm"]   # "hmac-sha256"
```

### Supported Algorithms

| Algorithm | Dependency | Use Case |
|-----------|------------|----------|
| `hmac-sha256` | None (stdlib) | Default. Shared-secret signing. |
| `ecdsa-p256` | `pip install arcllm[signing]` | Asymmetric signing for zero-trust environments. |

### Security Properties

- Canonical serialization ensures deterministic signatures regardless of dict ordering
- Signing key never appears in config files — env var only
- Each retry in the retry module sends the same signed payload (signature is stable)
- Missing signing key raises `ArcLLMConfigError` at load time, not at request time

---

## Feature 4: Audit Trail

### What It Does

Structured logging of every LLM interaction for compliance and forensics. PII-safe by default — logs only metadata (provider, model, message count, stop reason, content length, tool counts). Raw content logging is opt-in and gated behind DEBUG level.

### How to Use

```python
# Enable with defaults (metadata only)
model = load_model("anthropic", audit=True)

# Enable with raw content logging (DEBUG level only)
model = load_model("anthropic", audit={
    "include_messages": True,
    "include_response": True,
    "log_level": "INFO",
})
```

Or in `config.toml`:

```toml
[modules.audit]
enabled = true
include_messages = false
include_response = false
log_level = "INFO"
```

### What Gets Logged

**Always (at configured log level):**

| Field | Description |
|-------|-------------|
| `provider` | Provider name (e.g., `anthropic-messages`) |
| `model` | Model that responded |
| `message_count` | Number of messages in the request |
| `stop_reason` | Why the model stopped (`end_turn`, `tool_use`, etc.) |
| `tools_provided` | Number of tools sent (if any) |
| `tool_calls` | Number of tool calls in response (if any) |
| `content_length` | Character length of response content |

**Opt-in (DEBUG level only):**

| Field | Config Flag | Content |
|-------|-------------|---------|
| Messages | `include_messages: true` | Sanitized message content |
| Response | `include_response: true` | Sanitized response content |

### Security Properties

- Default behavior logs zero PII — only counts and metadata
- When Security module is enabled, audit sees already-redacted data
- Raw content logging requires both config flag AND DEBUG log level
- Log output is sanitized (control characters stripped)
- OTel span attributes attached for distributed trace correlation

---

## Feature 5: TLS Enforcement

### What It Does

All provider communication uses HTTPS by default. httpx (the HTTP client) enforces TLS certificate verification on every request.

### How It Works

- Provider base URLs are configured as `https://` in TOML files
- httpx verifies TLS certificates using the system CA bundle
- No option to disable TLS verification in production config

### Security Properties

- Data in transit is encrypted between ArcLLM and every LLM provider
- Certificate verification prevents MITM attacks
- Connection pooling via httpx reuses TLS sessions for performance

---

## Feature 6: Rate Limiting

### What It Does

Token-bucket rate limiting prevents API quota exhaustion and protects against runaway agents. Buckets are shared per-provider across all model instances in the same process.

### How to Use

```python
model = load_model("anthropic", rate_limit=True)

# Custom limits
model = load_model("anthropic", rate_limit={
    "requests_per_minute": 120,
    "burst_capacity": 150,
})
```

Or in `config.toml`:

```toml
[modules.rate_limit]
enabled = true
requests_per_minute = 60
burst_capacity = 60
```

### Security Properties

- Prevents cost overruns from agent loops that spin out of control
- Shared buckets ensure a single misbehaving agent can't starve others
- Async-safe with `asyncio.Lock` — no race conditions under concurrency

---

## Feature 7: OpenTelemetry with mTLS

### What It Does

Distributed tracing export via OTLP with full mTLS support for secure telemetry pipelines. Follows GenAI semantic conventions for LLM-specific span attributes.

### How to Use

```python
model = load_model("anthropic", otel=True)

# Custom OTel config
model = load_model("anthropic", otel={
    "exporter": "otlp",
    "endpoint": "https://otel-collector.internal:4317",
    "protocol": "grpc",
    "service_name": "my-agent",
    "sample_rate": 0.5,
    "certificate_file": "/etc/ssl/ca.pem",
    "client_key_file": "/etc/ssl/client-key.pem",
    "client_cert_file": "/etc/ssl/client-cert.pem",
})
```

Or in `config.toml`:

```toml
[modules.otel]
enabled = true
exporter = "otlp"
endpoint = "https://otel-collector.internal:4317"
protocol = "grpc"
service_name = "arcllm"
sample_rate = 1.0
insecure = false
certificate_file = "/etc/ssl/ca.pem"
client_key_file = "/etc/ssl/client-key.pem"
client_cert_file = "/etc/ssl/client-cert.pem"
timeout_ms = 10000
```

### Span Attributes (GenAI Conventions)

| Attribute | Value |
|-----------|-------|
| `gen_ai.system` | Provider name |
| `gen_ai.request.model` | Requested model |
| `gen_ai.response.model` | Actual model that responded |
| `gen_ai.usage.input_tokens` | Input token count |
| `gen_ai.usage.output_tokens` | Output token count |
| `gen_ai.response.finish_reasons` | Stop reason array |

### Security Properties

- mTLS support for secure collector communication (CA cert + client cert + client key)
- `insecure: false` by default — requires valid TLS
- Custom headers for auth tokens to collectors
- Trace sampling to control data volume (`sample_rate: 0.0` to `1.0`)
- Batch processing with configurable queue limits to prevent memory exhaustion

---

## Feature 8: Error Handling

### What It Does

Structured error hierarchy with raw data attachment for forensics. Errors fail fast and loud — no silent swallowing.

### Exception Hierarchy

```
ArcLLMError (base)
├── ArcLLMConfigError      — Configuration validation failure
├── ArcLLMParseError       — Tool call argument parsing failure (carries raw_string)
└── ArcLLMAPIError         — HTTP error from provider (carries status_code, body, provider)
```

### Security Properties

- `ArcLLMAPIError` truncates response body in `__str__` to 500 chars — prevents verbose provider errors from leaking into logs
- Full body available on the attribute for authorized inspection
- `retry_after` extracted from headers for smart retry behavior
- Non-retryable errors (401, 403) are never retried — prevents credential brute-force

---

## NIST 800-53 Control Mapping

ArcLLM's security features map to the following NIST 800-53 Rev 5 control families. This mapping shows which controls ArcLLM helps satisfy — full compliance requires organizational policies, procedures, and additional tooling beyond what a library provides.

| Control | Name | ArcLLM Feature | How It Helps |
|---------|------|----------------|--------------|
| **AC-3** | Access Enforcement | API Key Isolation, Vault | Keys resolved at runtime from authorized sources only. No hardcoded credentials. |
| **AC-6** | Least Privilege | Rate Limiting, Config-driven | Per-provider rate limits prevent over-consumption. Modules load only when enabled. |
| **AU-2** | Event Logging | Audit Module | Structured logging of every LLM interaction with metadata. |
| **AU-3** | Content of Audit Records | Audit Module | Records provider, model, message count, stop reason, token counts, tool usage. |
| **AU-6** | Audit Record Review | Audit + OTel | Structured logs and OTel spans enable automated analysis and alerting. |
| **AU-8** | Time Stamps | OTel Module | Spans include precise timestamps for correlation. |
| **AU-9** | Protection of Audit Info | Audit Module | PII-safe by default. Raw content requires explicit opt-in + DEBUG level. |
| **AU-12** | Audit Record Generation | Audit + OTel | Every `invoke()` produces audit records and optional OTel spans. |
| **CM-7** | Least Functionality | Opt-in Modules | Zero modules loaded by default. Only requested functionality is active. |
| **IA-5** | Authenticator Management | Vault, API Key Isolation | Keys from env vars or vault with TTL rotation. Never in config files. |
| **IA-9** | Service Identification | Request Signing | HMAC-SHA256 signatures verify request origin and integrity. |
| **MP-4** | Media Storage | API Key Isolation | Secrets never written to disk (no config file storage). |
| **SC-8** | Transmission Confidentiality | TLS Enforcement | All provider communication encrypted via HTTPS. |
| **SC-12** | Cryptographic Key Management | Vault, Signing Key Env | Keys managed through vault backends or protected env vars. |
| **SC-13** | Cryptographic Protection | Request Signing, TLS | HMAC-SHA256 for integrity, TLS for confidentiality. |
| **SC-28** | Protection of Information at Rest | PII Redaction | PII replaced with `[PII:TYPE]` before it reaches logs or LLM providers. |
| **SI-4** | System Monitoring | Telemetry, OTel | Per-call cost tracking, token usage, and distributed tracing. |
| **SI-10** | Information Input Validation | Pydantic Types | All inputs validated via Pydantic v2 models. Invalid data rejected at boundary. |
| **SI-11** | Error Handling | Exception Hierarchy | Structured errors with truncated output. No verbose stack traces in production logs. |

---

## OWASP Threat Mapping

ArcLLM's security features mitigate or prevent the following threats from the OWASP Top 10 (2021) and the OWASP Top 10 for LLM Applications (2025).

### OWASP Top 10 (2021)

| Threat | ArcLLM Mitigation |
|--------|-------------------|
| **A01: Broken Access Control** | API keys isolated in env vars/vault. No hardcoded secrets. Rate limiting prevents unauthorized over-use. |
| **A02: Cryptographic Failures** | TLS enforced on all provider communication. HMAC-SHA256 request signing. No secrets in config files or logs. |
| **A03: Injection** | Pydantic input validation on all types. Tool call arguments type-checked then JSON-parsed with `ArcLLMParseError` on failure. No string interpolation in API calls. |
| **A04: Insecure Design** | Security-first architecture. Fail-fast error handling. Raw response data excluded from serialization by default. |
| **A05: Security Misconfiguration** | Secure defaults (PII redaction on, signing on, TLS on). Unknown config keys raise errors. Module config validated at load time. |
| **A06: Vulnerable Components** | Minimal dependencies (pydantic, httpx only). No provider SDK dependencies. Direct HTTP reduces supply chain surface. |
| **A07: Auth Failures** | Non-retryable HTTP 401/403 errors. Missing API keys fail at load time. Vault TTL cache for rotation. |
| **A08: Data Integrity Failures** | HMAC-SHA256 canonical payload signing. Deterministic serialization. Signature attached to every response. |
| **A09: Logging & Monitoring Failures** | Structured audit logging on every call. OTel distributed tracing. PII-safe metadata by default. |
| **A10: SSRF** | ArcLLM only connects to URLs defined in provider TOML configs. No user-controlled URL construction. |

### OWASP Top 10 for LLM Applications (2025)

| Threat | ArcLLM Mitigation |
|--------|-------------------|
| **LLM01: Prompt Injection** | ArcLLM is a transport layer — it does not interpret prompt content. Agents are responsible for prompt construction. ArcLLM's PII redaction removes sensitive data before it reaches the LLM, reducing the data available if injection succeeds. |
| **LLM02: Sensitive Information Disclosure** | PII redaction on outbound messages strips SSN, credit cards, emails, phone, IP addresses before they reach the provider. Custom patterns for domain-specific PII. Audit module logs metadata only, never raw content by default. |
| **LLM03: Supply Chain** | Two runtime dependencies (pydantic, httpx). No provider SDKs — direct HTTP. Minimal attack surface. |
| **LLM04: Data and Model Poisoning** | Out of scope for transport layer. ArcLLM does not manage training data or model weights. |
| **LLM05: Improper Output Handling** | `LLMResponse.raw` excluded from serialization (`repr=False, exclude=True`). PII redaction on inbound responses. Strict type validation on all response fields via Pydantic. Tool call arguments always parsed to `dict` — no raw string pass-through. |
| **LLM06: Excessive Agency** | Rate limiting prevents runaway agent loops. Telemetry tracks cost per call. Token-bucket throttling is shared per provider, so one agent can't starve others. |
| **LLM07: System Prompt Leakage** | Transport layer does not inspect or log system prompts by default. Audit module's `include_messages` is `false` by default and gated behind DEBUG level. |
| **LLM08: Vector and Embedding Weaknesses** | Out of scope. ArcLLM handles text/tool LLM calls, not embedding operations. |
| **LLM09: Misinformation** | Out of scope for transport layer. ArcLLM faithfully relays LLM responses without modification (except PII redaction). |
| **LLM10: Unbounded Consumption** | Rate limiting (`requests_per_minute` + `burst_capacity`). Telemetry cost tracking per call. Budget module (planned) for spending caps. |

---

## Configuration Reference

### Security Module

```toml
[modules.security]
enabled = false                          # Master toggle
pii_enabled = true                       # PII detection and redaction
pii_detector = "regex"                   # Detection backend ("regex")
pii_custom_patterns = []                 # Additional patterns [{name, pattern}]
signing_enabled = true                   # Request payload signing
signing_algorithm = "hmac-sha256"        # "hmac-sha256" or "ecdsa-p256"
signing_key_env = "ARCLLM_SIGNING_KEY"   # Env var for signing key
```

### Audit Module

```toml
[modules.audit]
enabled = false               # Master toggle
include_messages = false      # Log raw messages (DEBUG only)
include_response = false      # Log raw response (DEBUG only)
log_level = "INFO"            # Python log level
```

### Vault

```toml
[vault]
backend = ""                  # "module:Class" format, empty = disabled
cache_ttl_seconds = 300       # TTL for cached keys
```

### OpenTelemetry Module

```toml
[modules.otel]
enabled = false
exporter = "otlp"                       # "otlp", "console", or "none"
endpoint = "http://localhost:4317"       # OTLP collector endpoint
protocol = "grpc"                        # "grpc" or "http"
service_name = "arcllm"                  # OTel service.name
sample_rate = 1.0                        # 0.0 to 1.0
headers = {}                             # Auth headers for collector
insecure = false                         # Allow insecure gRPC
certificate_file = ""                    # TLS CA certificate
client_key_file = ""                     # mTLS client key
client_cert_file = ""                    # mTLS client certificate
timeout_ms = 10000                       # Export timeout
max_batch_size = 512                     # Batch processor tuning
max_queue_size = 2048                    # Queue limit
schedule_delay_ms = 5000                 # Batch flush interval
```

### Rate Limit Module

```toml
[modules.rate_limit]
enabled = false
requests_per_minute = 60      # Sustained rate
burst_capacity = 60           # Max burst allowance
```

---

## Security Checklist for Deployment

- [ ] API keys set via environment variables or vault — not in config files
- [ ] `ARCLLM_SIGNING_KEY` set if request signing is enabled
- [ ] PII redaction enabled for any workflow handling user data
- [ ] Audit module enabled for compliance-required environments
- [ ] Rate limits configured per provider to prevent quota exhaustion
- [ ] OTel exporter pointed at a secure collector with mTLS if applicable
- [ ] Provider TOML `base_url` values use `https://`
- [ ] `include_messages` and `include_response` left `false` unless debugging
- [ ] Log level set to `INFO` or higher in production (not `DEBUG`)
- [ ] Vault TTL configured to balance freshness vs. round-trip overhead
