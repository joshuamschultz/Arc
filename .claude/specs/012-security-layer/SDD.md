# SDD — Security Layer (Step 14)

## Design Overview

Step 14 adds two security components to ArcLLM:

1. **VaultResolver** — Construction-time API key resolution from vault backends (HashiCorp Vault, AWS Secrets Manager) with TTL caching and env var fallback. Integrates into `registry.py` before adapter construction.

2. **SecurityModule** — Per-invoke middleware that (a) redacts PII from messages in both directions using configurable detectors and (b) signs request payloads with HMAC-SHA256 or ECDSA P-256 for audit integrity.

### Architecture Fit

```
Agent calls load_model("anthropic", security=True)
  │
  ├── VaultResolver: resolve API key (vault → env var fallback)
  │   └── TTL cache: skip vault if key is fresh
  │
  └── Build module stack:
      Otel → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter
                                    │
                                    ├── Pre-invoke: redact PII from messages
                                    ├── Call inner.invoke() with redacted messages
                                    ├── Post-invoke: redact PII from response
                                    ├── Sign: HMAC(redacted_messages + tools + model)
                                    └── Attach signature to response metadata
```

## Directory Map

### New Files

```
src/arcllm/
├── vault.py                    # VaultResolver + VaultBackend protocol + TTL cache
├── modules/
│   └── security.py             # SecurityModule (PII + signing)
├── _pii.py                     # RegexPiiDetector + PiiMatch + PiiDetector protocol
└── _signing.py                 # HMAC signer + ECDSA signer protocol + canonical JSON
```

### Modified Files

```
src/arcllm/
├── config.py                   # VaultConfig model, SecurityConfig model
├── config.toml                 # [vault] section, [modules.security] section
├── registry.py                 # VaultResolver integration + security= kwarg
├── types.py                    # Optional: metadata field on LLMResponse (or use existing raw)
├── providers/anthropic.toml    # vault_path field
├── providers/openai.toml       # vault_path field
└── __init__.py                 # Export SecurityModule, VaultResolver
```

### New Test Files

```
tests/
├── test_vault.py               # VaultResolver unit tests
├── test_pii.py                 # PII detection and redaction tests
├── test_signing.py             # Request signing tests
└── test_security.py            # SecurityModule integration tests
```

## Component Design

### 1. VaultBackend Protocol (`vault.py`)

```
Protocol: VaultBackend
  Methods:
    get_secret(path: str) -> str | None
      Returns secret value or None if not found.

    is_available() -> bool
      Returns True if backend is reachable.
```

**Design notes**:
- Protocol (not ABC) — structural subtyping, no inheritance required
- Concrete backends live in extras packages, not in core
- importlib loads backend class from config string

### 2. VaultResolver (`vault.py`)

```
Class: VaultResolver
  Constructor:
    backend: VaultBackend | None
    cache_ttl_seconds: int (default 300)

  Methods:
    resolve_api_key(api_key_env: str, vault_path: str | None) -> str
      1. If vault_path and backend: try backend.get_secret(vault_path)
         - On success: cache with TTL, return
         - On failure: fall through to env var
      2. Read os.environ[api_key_env]
      3. Raise ArcLLMConfigError if neither source has the key

  Internal:
    _cache: dict[str, tuple[str, float]]  # path -> (value, expiry_timestamp)
    _get_cached(path: str) -> str | None
    _set_cached(path: str, value: str) -> None
```

**Design notes**:
- Cache uses `time.monotonic()` for TTL expiry (immune to clock changes)
- Thread-safe under async (single event loop)
- `resolve_api_key()` called from `registry.py` during `load_model()`

### 3. PiiDetector Protocol (`_pii.py`)

```
Protocol: PiiDetector
  Methods:
    detect(text: str) -> list[PiiMatch]

Dataclass: PiiMatch
  Fields:
    pii_type: str        # "SSN", "EMAIL", "PHONE", "CREDIT_CARD", "IPV4", etc.
    start: int           # Start index in text
    end: int             # End index in text
    matched_text: str    # The actual matched text (for replacement)
```

### 4. RegexPiiDetector (`_pii.py`)

```
Class: RegexPiiDetector
  Constructor:
    custom_patterns: list[dict[str, str]] | None
      Each dict: {"name": "EMPLOYEE_ID", "pattern": "EMP-\\d{6}"}

  Built-in patterns:
    SSN:         \b\d{3}-\d{2}-\d{4}\b
    CREDIT_CARD: \b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b
    EMAIL:       \b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b
    PHONE:       \b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b
    IPV4:        \b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b

  Methods:
    detect(text: str) -> list[PiiMatch]
      Run all patterns (built-in + custom) against text.
      Return non-overlapping matches sorted by start position.

  Class-level:
    _compiled_patterns: cached compiled regex objects (compiled once)
```

**Design notes**:
- Patterns compiled once at class level, reused across instances
- Custom patterns merged with built-in at construction
- Non-overlapping: longer matches take priority

### 5. Redaction Function (`_pii.py`)

```
Function: redact_text(text: str, matches: list[PiiMatch]) -> str
  Replace each match with [PII:{type}] placeholder.
  Process matches in reverse order (by position) to preserve indices.

  Example: "Call 555-123-4567" -> "Call [PII:PHONE]"
```

### 6. Request Signer Protocol (`_signing.py`)

```
Protocol: RequestSigner
  Methods:
    sign(payload: bytes) -> str
      Returns hex-encoded signature string.

Class: HmacSigner
  Constructor:
    key: bytes (from signing_key_env environment variable)
  Methods:
    sign(payload: bytes) -> str
      hmac.new(key, payload, hashlib.sha256).hexdigest()

Class: EcdsaSigner (loaded via importlib when algorithm="ecdsa-p256")
  Constructor:
    private_key_pem: bytes (from signing_key_env environment variable)
  Methods:
    sign(payload: bytes) -> str
      ECDSA-P256 signature, hex-encoded
```

### 7. Canonical Serialization (`_signing.py`)

```
Function: canonical_payload(messages: list[Message], tools: list[Tool] | None, model: str) -> bytes
  1. Serialize messages to list of dicts (pydantic model_dump())
  2. Serialize tools to list of dicts (or empty list)
  3. Build dict: {"messages": [...], "tools": [...], "model": "..."}
  4. json.dumps(dict, sort_keys=True, separators=(',', ':'))
  5. Return .encode("utf-8")
```

**Design notes**:
- `sort_keys=True` ensures deterministic key ordering
- `separators=(',', ':')` removes whitespace for compact, deterministic output
- Same messages + tools + model always produce identical bytes

### 8. SecurityModule (`modules/security.py`)

```
Class: SecurityModule(BaseModule)
  Constructor:
    config: dict[str, Any]
    inner: LLMProvider

    Extracts from config:
      pii_enabled: bool (default True)
      pii_detector: str (default "regex")
      pii_custom_patterns: list[dict] (default [])
      signing_enabled: bool (default True)
      signing_algorithm: str (default "hmac-sha256")
      signing_key_env: str (default "ARCLLM_SIGNING_KEY")

    Lazily constructs:
      _pii_detector: PiiDetector | None
      _signer: RequestSigner | None

  Methods:
    invoke(messages, tools, **kwargs) -> LLMResponse:
      with self._span("security"):
        # Phase 1: PII Redaction (outbound)
        if pii_enabled:
          with self._span("security.pii_redact_outbound"):
            redacted_messages = redact_messages(messages, detector)

        # Phase 2: Call inner with redacted messages
        response = await self._inner.invoke(redacted_messages, tools, **kwargs)

        # Phase 3: PII Redaction (inbound)
        if pii_enabled:
          with self._span("security.pii_redact_inbound"):
            response = redact_response(response, detector)

        # Phase 4: Signing
        if signing_enabled:
          with self._span("security.sign"):
            payload = canonical_payload(redacted_messages, tools, self.model_name)
            signature = signer.sign(payload)
            response = attach_signature(response, signature, algorithm)

        return response
```

### 9. Config Models (`config.py` additions)

```
Class: VaultConfig(BaseModel)
  Fields:
    backend: str = ""           # empty = disabled
    cache_ttl_seconds: int = 300
    url: str = ""               # HashiCorp Vault URL
    region: str = ""            # AWS region

Class: SecurityModuleConfig(ModuleConfig)
  Fields:
    pii_enabled: bool = True
    pii_detector: str = "regex"
    pii_custom_patterns: list[dict[str, str]] = []
    signing_enabled: bool = True
    signing_algorithm: str = "hmac-sha256"
    signing_key_env: str = "ARCLLM_SIGNING_KEY"
```

### 10. Provider TOML Addition

Each provider TOML gets an optional `vault_path` field:

```toml
# providers/anthropic.toml
[provider]
name = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"
vault_path = ""                    # empty = use api_key_env only
```

### 11. Registry Integration (`registry.py`)

```python
# In load_model():
# After loading provider config, before constructing adapter:
if vault is configured globally:
    resolver = VaultResolver(backend, cache_ttl)
    api_key = resolver.resolve_api_key(config.provider.api_key_env, config.provider.vault_path)
    # Pass resolved key to adapter (new parameter or config override)

# After constructing adapter, add security module to stack:
# ... RateLimit → Fallback → Retry → Security → Audit → Telemetry → Otel
```

## ADRs

### ADR-1: VaultResolver Separate from Module Stack

**Context**: Vault resolves API keys at construction time, not per-invoke. PII/signing are per-invoke.

**Decision**: VaultResolver is a standalone utility in `vault.py`, called from `registry.py` before adapter construction. SecurityModule is a standard BaseModule wrapper in the invoke chain.

**Rationale**: Different lifecycles. Vault runs once (with TTL refresh). Security runs on every call. Mixing them in one module would conflate construction-time and runtime concerns.

**Alternatives rejected**: Single SecurityModule handling both (conflates lifecycles); vault as adapter concern (wrong abstraction level).

### ADR-2: Regex-First PII Detection

**Context**: PII detection runs on every invoke(). 10K concurrent agents means millions of detection calls.

**Decision**: Ship RegexPiiDetector as built-in default. Zero additional dependencies. <1ms per call on typical messages. Covers standard PII patterns (SSN, CC, email, phone, IP).

**Rationale**: Regex is deterministic, fast, and testable. ML-based detectors (Presidio) are more accurate but add heavy dependencies (spaCy + models). Pluggable protocol allows upgrade path.

**Alternatives rejected**: Presidio as default (too heavy for library); no built-in detector (bad DX).

### ADR-3: HMAC Default, ECDSA Optional

**Context**: Request signing for NIST 800-53 AU-10 (Non-repudiation). Need to balance simplicity with federal requirements.

**Decision**: HMAC-SHA256 as default (stdlib `hmac` module). ECDSA P-256 available via `arcllm[signing]` extra (requires `cryptography` package).

**Rationale**: HMAC covers single-trust-domain deployments with zero dependencies. ECDSA provides true non-repudiation for multi-agent federal environments. Config-driven switch: `signing_algorithm = "ecdsa-p256"`.

**Alternatives rejected**: ECDSA only (too heavy as default); no signing (doesn't meet AU-10).

### ADR-4: Signature Scope

**Context**: What data to sign for audit integrity.

**Decision**: Sign canonical JSON of messages + tools + model name. Post-redaction (sign what was actually sent, not original PII).

**Rationale**: Covers all data affecting LLM behavior. Post-redaction means signatures don't contain PII-derived data. Canonical JSON (sorted keys, no whitespace) ensures deterministic signatures.

**Alternatives rejected**: Sign full provider payload (ties to adapter serialization); sign messages only (misses tool injection); sign pre-redaction (signature leaks PII info).

### ADR-5: Signature Storage

**Context**: Where to attach the signature for downstream consumption.

**Decision**: Add optional `metadata: dict[str, Any] | None` field to LLMResponse. Signature stored as `metadata["request_signature"]` and `metadata["signing_algorithm"]`.

**Rationale**: LLMResponse already has `raw` for provider data. Metadata is for ArcLLM-layer data (signatures, trace IDs, etc.). Dict allows future metadata without type changes.

**Alternatives rejected**: New field per metadata item (rigid); stuff into `raw` (mixes provider and ArcLLM data).

## Edge Cases

| Case | Handling |
|------|----------|
| Vault backend not installed | ArcLLMConfigError: "vault_backend='aws' but arcllm[vault-aws] not installed" |
| Vault unreachable | Log warning, fall back to env var |
| Vault key not found | Fall back to env var |
| Env var also missing | ArcLLMConfigError (same as today) |
| TTL cache expired | Next invoke() triggers fresh vault lookup |
| PII in ContentBlock (not just text) | Scan text content of TextBlock and ToolResultBlock. Skip ImageBlock binary. Scan ToolUseBlock arguments as JSON string. |
| PII spans multiple blocks | Each block scanned independently (no cross-block PII detection) |
| Empty messages list | No PII scan needed, signing covers empty list |
| Signing key env var missing | ArcLLMConfigError: "ARCLLM_SIGNING_KEY not set" (when signing_enabled=True) |
| ECDSA requested but cryptography not installed | ArcLLMConfigError: "signing_algorithm='ecdsa-p256' requires arcllm[signing]" |
| Overlapping PII matches | Longer match takes priority (sorted by length descending) |
| Custom PII pattern invalid regex | ArcLLMConfigError at SecurityModule construction |
| PII in tool names/descriptions | Not scanned (tool definitions are developer-authored, not user data) |
| Content is str (not list[ContentBlock]) | Scan the string directly |

## Test Strategy

### Unit Tests (`test_pii.py`) — ~20 tests
- Each PII pattern type: SSN, CC, email, phone, IP
- No false positives on similar-looking non-PII
- Custom pattern addition
- Redaction output format
- Overlapping match handling
- Empty/None input handling

### Unit Tests (`test_signing.py`) — ~10 tests
- HMAC deterministic signature
- Canonical JSON determinism (key ordering, whitespace)
- Different inputs → different signatures
- Missing signing key → error
- ECDSA path (mocked cryptography)

### Unit Tests (`test_vault.py`) — ~15 tests
- Vault hit → return key
- Vault miss → env var fallback
- Vault unreachable → env var fallback
- TTL cache hit (within TTL)
- TTL cache expired (beyond TTL)
- No vault configured → env var only
- Backend not installed → clear error
- Invalid backend config → error

### Integration Tests (`test_security.py`) — ~15 tests
- Full SecurityModule invoke with PII in messages
- Full SecurityModule invoke with PII in response
- Signing + PII redaction combined
- SecurityModule with OTel spans
- load_model() with security=True
- VaultResolver in registry flow
- Config resolution (security kwarg variations)
- Stack ordering verification (audit sees redacted data)
