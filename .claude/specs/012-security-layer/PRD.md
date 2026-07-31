# PRD — Security Layer (Step 14)

## Problem Statement

ArcLLM currently relies exclusively on environment variables for API key management and provides no protection against PII leaking through LLM calls. In federal production environments with thousands of concurrent agents:

1. **API Key Management**: Env vars don't rotate automatically, are visible to all processes on the host, and provide no audit trail of key access. Federal deployments require centralized secrets management (NIST 800-53 SC-12).

2. **PII Exposure**: Agents may inadvertently send PII (SSNs, emails, phone numbers) to LLM providers. LLM responses may also contain PII. Federal compliance (NIST 800-53 SI-12) requires PII handling controls.

3. **Audit Integrity**: Without request signing, audit logs can be tampered with. NIST 800-53 AU-10 (Non-repudiation) requires cryptographic proof of request authenticity.

## Goals

| # | Goal | Success Metric |
|---|------|----------------|
| G1 | Vault-based API key resolution with automatic rotation support | Keys resolve from vault with TTL cache; zero-downtime rotation |
| G2 | PII detection and redaction in both directions | Known PII patterns (SSN, CC, email, phone) caught before reaching provider |
| G3 | Cryptographic request signing for audit integrity | Every invoke() produces verifiable signature covering request content |
| G4 | Zero impact when disabled | No additional imports, latency, or dependencies when security=False |
| G5 | Pluggable for enterprise needs | Custom PII detectors and vault backends configurable without code changes |

## Success Criteria

- [ ] SC-1: VaultResolver retrieves API keys from mock vault backend and falls back to env var
- [ ] SC-2: TTL-based caching prevents vault calls on every invoke()
- [ ] SC-3: RegexPiiDetector catches SSN, credit card, email, phone, IPv4 patterns
- [ ] SC-4: PII redacted in outbound messages before reaching provider
- [ ] SC-5: PII redacted in inbound responses before reaching agent
- [ ] SC-6: Redaction uses type-tagged placeholders: [PII:SSN], [PII:EMAIL], etc.
- [ ] SC-7: Custom regex patterns addable via pii_custom_patterns config
- [ ] SC-8: Custom PII detector class loadable via pii_detector_class config
- [ ] SC-9: HMAC-SHA256 signing works with stdlib (zero extra deps)
- [ ] SC-10: Signature covers serialized messages + tools + model name
- [ ] SC-11: Signature attached to LLMResponse metadata
- [ ] SC-12: SecurityModule integrates with OTel spans (security.pii_redact, security.sign spans)
- [ ] SC-13: All existing tests pass (379+)
- [ ] SC-14: >=90% coverage on new security code

## Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|-------------|----------|------------|
| FR-1 | VaultResolver resolves API keys from configured vault backend | P0 | Unit test: mock vault returns key |
| FR-2 | VaultResolver falls back to env var when vault key not found | P0 | Unit test: vault miss → env var hit |
| FR-3 | VaultResolver caches keys with configurable TTL | P0 | Unit test: second call within TTL uses cache |
| FR-4 | VaultResolver raises ArcLLMConfigError when backend not installed | P0 | Unit test: import failure → clear error |
| FR-5 | VaultBackend protocol defines get_secret() and is_available() | P0 | Protocol in types or security module |
| FR-6 | RegexPiiDetector detects SSN (XXX-XX-XXXX) | P0 | Unit test with sample SSN |
| FR-7 | RegexPiiDetector detects credit card numbers (16 digits) | P0 | Unit test with sample CC |
| FR-8 | RegexPiiDetector detects email addresses | P0 | Unit test with sample emails |
| FR-9 | RegexPiiDetector detects phone numbers (US formats) | P0 | Unit test with sample phones |
| FR-10 | RegexPiiDetector detects IPv4 addresses | P1 | Unit test with sample IPs |
| FR-11 | PII redaction replaces matches with [PII:TYPE] tags | P0 | Unit test on redacted output |
| FR-12 | Custom regex patterns configurable via pii_custom_patterns | P1 | Unit test with custom pattern |
| FR-13 | Custom PII detector class loadable via pii_detector_class | P1 | Unit test with mock detector |
| FR-14 | SecurityModule redacts outbound messages (to LLM) | P0 | Unit test: messages contain PII, adapter receives redacted |
| FR-15 | SecurityModule redacts inbound responses (from LLM) | P0 | Unit test: response contains PII, agent receives redacted |
| FR-16 | HMAC-SHA256 signing of request payload | P0 | Unit test: signature verifiable with key |
| FR-17 | Canonical JSON serialization for deterministic signing | P0 | Unit test: same input → same signature |
| FR-18 | Signature attached to LLMResponse metadata | P0 | Unit test: response has request_signature field |
| FR-19 | ECDSA P-256 signing when arcllm[signing] installed | P1 | Unit test with mock cryptography |
| FR-20 | SecurityModule creates OTel spans for PII and signing phases | P1 | Unit test: spans created |
| FR-21 | load_model() accepts security= kwarg | P0 | Integration test |
| FR-22 | registry.py integrates VaultResolver for key resolution | P0 | Integration test |

## Non-Functional Requirements

| ID | Requirement | Target | Measurement |
|----|-------------|--------|-------------|
| NFR-1 | PII scanning latency | <5ms for typical message | Benchmark test |
| NFR-2 | Signing latency (HMAC) | <1ms | Benchmark test |
| NFR-3 | Vault cache hit | 0ms overhead | No vault call on cache hit |
| NFR-4 | Zero deps when disabled | No imports when security=False | Lazy import verification |
| NFR-5 | Memory per detector | <1MB for regex patterns | Compiled regex reuse |
| NFR-6 | Thread safety | Vault cache safe under async | asyncio single-thread guarantee |

## User Stories

### US-1: Federal Operations Engineer
As a federal ops engineer deploying 500 agents, I want API keys managed through HashiCorp Vault so that keys rotate automatically without restarting agents, and every key access is auditable.

### US-2: Security Compliance Officer
As a compliance officer, I want proof that no PII reaches LLM providers so that we pass FedRAMP audits. I need the redaction to be logged and the original messages never sent.

### US-3: Agent Developer
As an agent developer, I want `security=True` to just work with sane defaults (regex PII detection, HMAC signing) so I don't need to configure anything for basic protection.

### US-4: Platform Architect
As the platform architect, I want to plug in our internal NLP-based PII detector so that we catch context-dependent PII (names, addresses) beyond what regex patterns handle.

### US-5: Incident Responder
As an incident responder, I want cryptographic signatures on every LLM request so that I can verify audit log integrity during post-incident analysis.

## Out of Scope

- Tool Call Validator (Step 17)
- Content Scanner / prompt injection detection (Step 18)
- Vault backend implementations (shipped as separate extras packages)
- ECDSA key generation / PKI infrastructure
- PII de-identification (reversible tokenization)
- Real-time PII detection model training

## Dependencies

| Dependency | Type | Notes |
|------------|------|-------|
| BaseModule (base.py) | Internal | _tracer, _span() for OTel |
| AuditModule | Internal | Sees redacted data (stacking order) |
| registry.py | Internal | VaultResolver integration for key resolution |
| hmac (stdlib) | External | HMAC-SHA256 signing |
| hashlib (stdlib) | External | SHA-256 hashing |
| json (stdlib) | External | Canonical serialization |
| re (stdlib) | External | Regex PII patterns |
| cryptography (optional) | External | ECDSA signing via arcllm[signing] |
