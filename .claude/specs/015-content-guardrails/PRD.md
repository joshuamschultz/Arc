# PRD — Content Guardrails (Step 15)

## Problem Statement

Spec 012 gave ArcLLM a SecurityModule (PII redaction + request signing) and a VaultResolver, but three gaps remain that federal deployments hit immediately:

1. **No inbound prompt-injection detection.** Nothing scans user turns or tool results for instruction-override attacks ("ignore previous instructions", system-prompt exfiltration, delimiter/role-override, encoded instructions) before they reach the provider. This is OWASP LLM01 and — because tool results are the poisoning vector — ASI06. Today an attacker-controlled tool result flows straight to the model.

2. **The PII detector is shallow and non-pluggable.** `RegexPiiDetector` covers SSN/CC/email/phone/IPv4 with no checksum validation (16-digit order numbers redact as credit cards), no gov/CUI entities (passport, driver's license, DOD ID/EDIPI, CAC, bank account, DOB, MRN, IPv6), and no secret scanning (AWS keys, GitHub tokens, JWTs, PEM blocks, DB URLs leak untouched). Worse, `modules/security.py` **hard-rejects any detector other than `"regex"`** (`_VALID_DETECTORS = {"regex"}`, raises at line 58) — so the bring-your-own spaCy/Presidio path that spec 012 promised (D-093 / FR-13) does not exist. Entity categories cannot be toggled per deployment.

3. **No structural output validation.** When an agent expects JSON conforming to a schema, or a response constrained to an allow-list / under a length cap / free of banned content, ArcLLM returns whatever the provider produced. This is OWASP LLM05 (Improper Output Handling): raw model output is trusted downstream with no structural gate.

Spec 015 closes all three within the arcllm transport boundary — flagging, redacting, and structurally validating content. It does **not** interpret meaning; semantic judgement (grounding, correctness, toxicity) stays in arcagent/arcrun.

## Goals

| # | Goal | Pillar | Success Metric |
|---|------|--------|----------------|
| G1 | Inbound prompt-injection detection on user + tool-result content, opt-in and zero-dep by default | Security | Curated attack corpus flags known injection patterns before the provider; `injection=False` path imports nothing |
| G2 | Enrich the default PII detector: checksums, gov/CUI entities, secret scanning, per-category toggles | Security | Luhn/mod-97/ABA cut false positives; new entities + SECRETS category detected; `pii_entities` allow/deny honored |
| G3 | Make the PII detector genuinely pluggable via an allowlisted class loader (complete D-093/FR-13) | Modularity | `pii_detector_class="module:Class"` loads any `PiiDetector`; non-allowlisted module → clear `ArcLLMConfigError` |
| G4 | Structural output guardrails validating the resolved response per call | Modularity | Schema / regex / length / stop-list violations flagged (warn) or raised (block) via `guardrails={...}` |
| G5 | Zero cost and zero deps when disabled; simple, flat, one-code-path modules | Simplicity | No injection/guardrails imports, latency, or deps when the kwarg is `False`/absent |
| G6 | Correct stack placement so each guard sees the right bytes at scale | Scalability | Injection sees original text; Guardrails validates final post-Retry/Fallback response; both async-safe |

## Success Criteria

- [ ] SC-1: InjectionModule detects "ignore previous instructions" / "disregard the above" / role-override / delimiter-injection / encoded-instruction patterns in user content
- [ ] SC-2: InjectionModule scans tool-result content (ToolResultBlock) as an ASI06 poisoning vector
- [ ] SC-3: `injection="block"` raises `ArcLLMInjectionError`; `injection="warn"` flags in trace + OTel span attributes and continues
- [ ] SC-4: Pattern tier requires zero new dependencies; semantic tier only imports under `arcllm[injection-semantic]`
- [ ] SC-5: InjectionModule never mutates or executes scanned content (flag/block only)
- [ ] SC-6: RegexPiiDetector validates CREDIT_CARD via Luhn, IBAN via mod-97, ABA routing via checksum
- [ ] SC-7: RegexPiiDetector detects US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN, IPV6
- [ ] SC-8: SECRETS category detects AWS AKIA keys, GitHub tokens (ghp_/gho_/ghs_), JWT (eyJ...), PEM blocks, DB connection URLs; redacts to `[SECRET:TYPE]`
- [ ] SC-9: `pii_entities` allow/deny list toggles categories individually without code changes
- [ ] SC-10: `pii_detector_class="module:Class"` loads a custom detector conforming to the PiiDetector protocol
- [ ] SC-11: Non-allowlisted `pii_detector_class` module → `ArcLLMConfigError` (ASI04 supply-chain guard, mirrors vault.py)
- [ ] SC-12: A loaded class lacking `.detect()` → clear `ArcLLMConfigError` at construction
- [ ] SC-13: GuardrailsModule validates JSON-schema conformance when a schema/json response_format is expected
- [ ] SC-14: GuardrailsModule enforces regex allow-list / deny-list, max-length cap, banned-content stop-list
- [ ] SC-15: `guardrails={...}` block mode raises `ArcLLMGuardrailError`; warn mode flags in trace
- [ ] SC-16: Stack order places Injection above Security and Guardrails just inside Audit; verified by an ordering test
- [ ] SC-17: OTel spans emitted for injection scan and guardrail validation with verdict attributes
- [ ] SC-18: All existing tests pass (no regressions); the security.py:33/58 hard-reject is removed
- [ ] SC-19: >=90% coverage on all new/modified content-guardrail code

## Functional Requirements

| ID | Requirement | Priority | Acceptance |
|----|-------------|----------|------------|
| FR-1 | InjectionModule(BaseModule) scans inbound user content before the provider | P0 | Unit: user message with attack pattern flagged |
| FR-2 | InjectionModule scans ToolResultBlock content (ASI06 vector) | P0 | Unit: poisoned tool result flagged |
| FR-3 | Curated pattern corpus covers instruction-override, exfiltration, delimiter/role-override, encoded-instruction attacks | P0 | Unit: one positive test per attack family + negatives |
| FR-4 | Pattern tier is zero-dep and the default | P0 | Import test: pattern-only path pulls no extras |
| FR-5 | Optional semantic tier (embedding cosine similarity to attack corpus) behind `arcllm[injection-semantic]` | P1 | Unit: semantic path errors clearly when extra not installed |
| FR-6 | `injection` accepts True / False / dict per call | P0 | Integration: kwarg variations resolve |
| FR-7 | `enforcement="block"` raises `ArcLLMInjectionError`; `"warn"` flags + continues | P0 | Unit: block raises, warn returns with flag |
| FR-8 | InjectionModule sets OTel span attributes and records detections in the trace | P1 | Unit: span attributes present |
| FR-9 | InjectionModule never mutates/executes content | P0 | Unit: scanned messages passed through byte-identical |
| FR-10 | Default is OFF (opt-in) | P0 | Integration: no injection wrap unless enabled |
| FR-11 | RegexPiiDetector adds Luhn validator for CREDIT_CARD | P0 | Unit: valid CC matches, invalid 16-digit does not |
| FR-12 | RegexPiiDetector adds mod-97 validator for IBAN | P1 | Unit: valid IBAN matches, bad checksum does not |
| FR-13 | RegexPiiDetector adds ABA routing checksum validator | P1 | Unit: valid routing number matches |
| FR-14 | RegexPiiDetector adds IPV6 | P1 | Unit: sample IPv6 detected |
| FR-15 | RegexPiiDetector adds US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN | P0 | Unit: one positive test per entity |
| FR-16 | Entity categories individually toggleable via `pii_entities` allow/deny config | P0 | Unit: denied category not detected; allow-list limits scan |
| FR-17 | SECRETS category detects AWS AKIA, GitHub tokens, JWT, PEM blocks, DB URLs | P0 | Unit: one positive test per secret type |
| FR-18 | Secrets redact to `[SECRET:TYPE]` | P0 | Unit: redacted output tag format |
| FR-19 | `pii_detector_class="module:Class"` loads a custom PiiDetector via allowlisted importlib | P0 | Unit: mock detector class loads and runs |
| FR-20 | Non-allowlisted module prefix → `ArcLLMConfigError` | P0 | Unit: `os:system` style ref rejected |
| FR-21 | Loaded class missing `.detect()` → `ArcLLMConfigError` | P0 | Unit: bad class rejected at construction |
| FR-22 | `security.py` no longer hard-rejects non-"regex" detectors | P0 | Unit: former `_VALID_DETECTORS` reject path gone |
| FR-23 | GuardrailsModule(BaseModule) validates the response content | P0 | Unit: invalid response flagged |
| FR-24 | JSON-schema conformance check when a schema/json response_format is expected | P0 | Unit: non-conforming JSON flagged |
| FR-25 | Regex allow-list / deny-list validation | P0 | Unit: deny hit flagged, allow miss flagged |
| FR-26 | Max-length cap validation | P0 | Unit: over-length flagged |
| FR-27 | Banned-content stop-list validation | P0 | Unit: banned phrase flagged |
| FR-28 | `guardrails` configured per call via kwarg (True/False/dict) | P0 | Integration: kwarg variations resolve |
| FR-29 | `enforcement="block"` raises `ArcLLMGuardrailError`; `"warn"` flags + continues | P0 | Unit: block raises, warn returns with flag |
| FR-30 | GuardrailsModule sets OTel span attributes with the verdict | P1 | Unit: span attributes present |
| FR-31 | `ArcLLMInjectionError` and `ArcLLMGuardrailError` subclass `ArcLLMError` | P0 | Unit: isinstance checks |
| FR-32 | New `[modules.injection]` and `[modules.guardrails]` config sections load | P0 | Config test: loads with defaults |
| FR-33 | `[modules.security]` extended with `pii_entities` + `pii_detector_class` | P0 | Config test: new keys accepted |
| FR-34 | `injection` and `guardrails` kwargs added to `load_model()` and MODULE_NAMES | P0 | Integration: `load_model(..., injection=True)` works |
| FR-35 | Stack order: Otel → Queue → Telemetry → CircuitBreaker → Audit → Guardrails → Injection → Security → Retry → Fallback → RateLimit → [Routing\|Adapter] | P0 | Ordering test asserts wrap sequence |

## Non-Functional Requirements

| ID | Requirement | Target | Measurement |
|----|-------------|--------|-------------|
| NFR-1 | Injection pattern scan latency | <3ms for typical message | Benchmark |
| NFR-2 | PII scan latency with enriched entities + checksums | <8ms for typical message | Benchmark |
| NFR-3 | Guardrail validation latency (non-semantic) | <5ms | Benchmark |
| NFR-4 | Zero deps when injection/guardrails disabled | No imports on disabled path | Lazy-import verification |
| NFR-5 | Compiled patterns reused across instances | Module-level compilation | Pattern reuse assertion |
| NFR-6 | Async safety under concurrent invoke() | No shared mutable state | Concurrency test |

## NIST 800-53 + OWASP LLM/ASI Mapping

| Control / Threat | Framework | Covered By |
|------------------|-----------|------------|
| SI-3 (Malicious Code Protection) | NIST 800-53 | InjectionModule pattern/semantic detection |
| SI-10 (Information Input Validation) | NIST 800-53 | InjectionModule (inbound) + enriched PII + GuardrailsModule (outbound) |
| SI-15 (Information Output Filtering) | NIST 800-53 | GuardrailsModule structural validation |
| SC-28 (Protection of Information at Rest) | NIST 800-53 | PII/secret redaction before content leaves the agent |
| AC-4 (Information Flow Enforcement) | NIST 800-53 | PII/secret redaction + guardrail deny-list gating outbound flow |
| LLM01 (Prompt Injection) | OWASP LLM 2025 | InjectionModule user-content scan |
| LLM02 (Sensitive Information Disclosure) | OWASP LLM 2025 | PII/CUI + SECRETS detection and redaction |
| LLM05 (Improper Output Handling) | OWASP LLM 2025 | GuardrailsModule (schema/regex/length/stop-list) |
| LLM06 (Excessive Agency) | OWASP LLM 2025 | Guardrail deny-list + injection block gates |
| ASI04 (Agentic Supply Chain) | OWASP ASI 2026 | Allowlisted `pii_detector_class` loader (mirrors vault.py) |
| ASI06 (Memory & Context Poisoning) | OWASP ASI 2026 | InjectionModule tool-result scanning |

## User Stories

### US-1: Federal Security Engineer
As a federal security engineer, I want inbound prompt-injection detection on tool results so that a poisoned search result can't hijack my agent's instructions, and I want to run it in `warn` mode first to measure false positives before enforcing `block`.

### US-2: CUI Data Steward
As a data steward on a DOE machine, I want the PII detector to catch passports, DOD IDs, CACs, bank accounts, DOB, and MRNs — not just SSNs — and I want checksum validation so order numbers don't get redacted as credit cards.

### US-3: Platform Architect
As the platform architect, I want to plug in our licensed Presidio detector via `pii_detector_class` without forking arcllm, and I want the loader to reject any module outside our allowlist so a tampered config can't import arbitrary code.

### US-4: Agent Developer
As an agent developer, I want `guardrails={"json_schema": {...}, "max_length": 4000}` so that a response that isn't valid schema-conforming JSON raises before it reaches my downstream parser.

### US-5: Compliance Officer
As a compliance officer, I want secrets (AWS keys, GitHub tokens, JWTs, PEM blocks, DB URLs) redacted to `[SECRET:TYPE]` before any content leaves the agent, and I want the audit trail to record every injection and guardrail verdict.

## Out of Scope

- **Semantic guardrails** — grounding, factual correctness, toxicity/harm judgement (belongs in arcagent/arcrun; D-429)
- Content *rewriting* or *sanitizing* of injection matches (arcllm flags/blocks only; D-421)
- Prompt-injection *remediation* strategy (agent decides what to do with a flag)
- Reversible PII tokenization / de-identification (carried over out-of-scope from spec 012)
- Semantic-tier embedding model *training* or corpus curation tooling
- Vault backend implementations (spec 012, separate extras)

## Dependencies

| Dependency | Type | Notes |
|------------|------|-------|
| BaseModule (modules/base.py) | Internal | `_tracer`, `_span()`, `validate_config_keys` |
| `_pii.py` | Internal | Extended with checksums, entities, secrets, toggles |
| `modules/security.py` | Internal | Detector-class loader added; hard-reject removed |
| `vault.py` | Internal | Reference pattern for allowlisted class loader (D-427) |
| registry.py | Internal | `injection`/`guardrails` kwargs + stack placement |
| exceptions.py | Internal | `ArcLLMInjectionError`, `ArcLLMGuardrailError` |
| re, json, hashlib (stdlib) | External | Patterns, JSON-schema/canonical checks, checksums |
| jsonschema OR stdlib structural check | External | Schema conformance (kept dependency-light) |
| embeddings backend (optional) | External | Semantic injection tier via `arcllm[injection-semantic]` |
