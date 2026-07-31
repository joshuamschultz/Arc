# PLAN — Security Layer (Step 14)

**Status**: COMPLETE
**Spec**: 012-security-layer
**Estimated tasks**: 12
**Estimated new tests**: ~60
**Actual new tests**: 72 (26 PII + 13 signing + 14 vault + 19 security)
**Total tests**: 451 passed, 1 skipped

---

## Phase 1: Foundation Types and Config (Tasks 1-3)

### T14.1 — Add VaultConfig and SecurityModuleConfig to config.py
- [x] Add `VaultConfig` pydantic model: backend, cache_ttl_seconds, url, region
- [x] Add `SecurityModuleConfig` extending ModuleConfig: pii_enabled, pii_detector, pii_custom_patterns, signing_enabled, signing_algorithm, signing_key_env
- [x] Add `vault_path: str = ""` field to ProviderSettings model
- [x] Add `vault: VaultConfig` field to GlobalConfig model
- [x] Add `metadata: dict[str, Any] | None = None` field to LLMResponse in types.py
- [x] Update `__init__.py` exports

**Acceptance**:
- [x] Existing tests still pass (no breaking changes to config parsing)
- [x] New config fields have sensible defaults
- [x] LLMResponse.metadata is None by default (backward compatible)

### T14.2 — Update config.toml and provider TOMLs
- [x] Add `[vault]` section to config.toml with defaults (backend="", cache_ttl_seconds=300, url="", region="")
- [x] Add `[modules.security]` section to config.toml with defaults
- [x] Add `vault_path = ""` to providers/anthropic.toml
- [x] Add `vault_path = ""` to providers/openai.toml

**Acceptance**:
- [x] Config loads without errors
- [x] Existing config tests pass

### T14.3 — Add arcllm[signing] extras to pyproject.toml
- [x] Add `signing` extras group: `cryptography >= 42.0`
- [x] Verify existing extras (otel) still work

**Acceptance**:
- [x] `pip install -e ".[dev]"` succeeds
- [x] pyproject.toml has signing extras

---

## Phase 2: PII Detection and Redaction (Tasks 4-5)

### T14.4 — Write test_pii.py (TDD RED)
- [x] Test RegexPiiDetector detects SSN patterns
- [x] Test RegexPiiDetector detects credit card patterns
- [x] Test RegexPiiDetector detects email patterns
- [x] Test RegexPiiDetector detects phone patterns
- [x] Test RegexPiiDetector detects IPv4 patterns
- [x] Test no false positives on non-PII text
- [x] Test custom pattern addition via config
- [x] Test redact_text() produces [PII:TYPE] placeholders
- [x] Test overlapping matches (longer wins)
- [x] Test empty/None input handling
- [x] Test PiiMatch dataclass fields
- [x] Test PiiDetector protocol conformance

**Acceptance**:
- [x] All PII tests written — 26 tests
- [x] Tests cover each pattern type with positive and negative cases

### T14.5 — Implement _pii.py (TDD GREEN)
- [x] Create PiiMatch dataclass
- [x] Create PiiDetector protocol
- [x] Implement RegexPiiDetector with built-in patterns
- [x] Implement custom pattern support
- [x] Implement redact_text() function
- [x] Compile patterns once at class level

**Acceptance**:
- [x] All test_pii.py tests pass (26/26)
- [x] Patterns compiled once at module level (_BUILTIN_PATTERNS)
- [x] Coverage: 100%

---

## Phase 3: Request Signing (Tasks 6-7)

### T14.6 — Write test_signing.py (TDD RED)
- [x] Test HmacSigner produces deterministic signature
- [x] Test same input → same signature
- [x] Test different input → different signature
- [x] Test canonical_payload() with sort_keys determinism
- [x] Test canonical_payload() with None tools
- [x] Test missing signing key → ArcLLMConfigError
- [x] Test ECDSA path raises clear error when cryptography not installed
- [x] Test signature is hex string

**Acceptance**:
- [x] All signing tests written — 13 tests

### T14.7 — Implement _signing.py (TDD GREEN)
- [x] Create RequestSigner protocol
- [x] Implement HmacSigner using hmac + hashlib (stdlib)
- [x] Implement canonical_payload() with json.dumps(sort_keys=True)
- [x] Implement EcdsaSigner stub that raises if cryptography not installed
- [x] Implement create_signer() factory function (reads algorithm config)

**Acceptance**:
- [x] All test_signing.py tests pass (13/13)
- [x] HMAC signing uses only stdlib
- [x] Canonical serialization is deterministic
- [x] Coverage: 97%

---

## Phase 4: Vault Resolver (Tasks 8-9)

### T14.8 — Write test_vault.py (TDD RED)
- [x] Test VaultResolver with mock backend: vault hit returns key
- [x] Test vault miss → env var fallback
- [x] Test vault unreachable → env var fallback
- [x] Test no vault configured → env var only
- [x] Test TTL cache hit (second call within TTL uses cache)
- [x] Test TTL cache expired (beyond TTL triggers fresh lookup)
- [x] Test backend not installed → ArcLLMConfigError
- [x] Test invalid backend config → ArcLLMConfigError
- [x] Test neither vault nor env var → ArcLLMConfigError
- [x] Test VaultBackend protocol

**Acceptance**:
- [x] All vault tests written — 14 tests

### T14.9 — Implement vault.py (TDD GREEN)
- [x] Create VaultBackend protocol (get_secret, is_available)
- [x] Implement VaultResolver class with TTL cache
- [x] Cache uses time.monotonic() for expiry
- [x] resolve_api_key(): vault first → env var fallback
- [x] Backend loading via importlib from config string
- [x] Clear error messages for missing backends

**Acceptance**:
- [x] All test_vault.py tests pass (14/14)
- [x] Cache correctly expires after TTL
- [x] Fallback behavior works correctly
- [x] Coverage: 92%

---

## Phase 5: SecurityModule Integration (Tasks 10-12)

### T14.10 — Write test_security.py (TDD RED)
- [x] Test SecurityModule redacts PII from outbound messages (text content)
- [x] Test SecurityModule redacts PII from outbound messages (ContentBlock content)
- [x] Test SecurityModule redacts PII from inbound response
- [x] Test SecurityModule signs request and attaches to response.metadata
- [x] Test PII + signing combined in single invoke
- [x] Test PII disabled (signing only)
- [x] Test signing disabled (PII only)
- [x] Test both disabled (passthrough)
- [x] Test OTel spans created for security phases
- [x] Test invalid config raises ArcLLMConfigError
- [x] Test custom PII detector class loading
- [x] Test stack ordering: audit sees redacted data

**Acceptance**:
- [x] All security integration tests written — 19 tests

### T14.11 — Implement SecurityModule in modules/security.py (TDD GREEN)
- [x] SecurityModule extends BaseModule
- [x] Constructor: parse config, lazily build detector and signer
- [x] invoke(): Phase 1 — redact outbound messages
- [x] invoke(): Phase 2 — call inner.invoke() with redacted messages
- [x] invoke(): Phase 3 — redact inbound response
- [x] invoke(): Phase 4 — sign and attach signature
- [x] OTel spans for each phase
- [x] Helper: _redact_messages() — handles str and list[ContentBlock]
- [x] Helper: _redact_response() — handles response content
- [x] Config validation at construction

**Acceptance**:
- [x] All test_security.py tests pass (19/19)
- [x] SecurityModule follows BaseModule patterns from existing modules
- [x] Coverage: 89%

### T14.12 — Update registry.py, __init__.py, modules/__init__.py
- [x] Add `security` kwarg to load_model()
- [x] Integrate VaultResolver: if vault.backend configured, resolve key before adapter construction
- [x] Add SecurityModule to stacking order (after Retry, before Audit)
- [x] Update modules/__init__.py with SecurityModule export
- [x] Update __init__.py with VaultResolver, SecurityModule exports
- [x] Run full test suite: all 379+ existing tests pass + 72 new tests

**Acceptance**:
- [x] load_model("anthropic", security=True) works
- [x] Full stack: Otel → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter
- [x] All existing tests pass (no regressions)
- [x] >=90% coverage on new code (avg 95%)
- [x] Total test count: 451 passed, 1 skipped

---

## Completion Checklist

- [x] All 12 tasks complete
- [x] All tests pass (existing + new): 451 passed, 1 skipped
- [x] Coverage >=90% on new files (_pii: 100%, _signing: 97%, vault: 92%, security: 89%)
- [x] State file updated
- [x] Decision log updated (D-089 through D-099)
