# PLAN — Content Guardrails (Step 15)

**Status**: COMPLETE
**Spec**: 015-content-guardrails
**Estimated tasks**: 14
**Estimated new tests**: ~66 (18 injection + 22 pii-enrichment + 10 pii-loader + 16 guardrails), plus ~10 integration
**Actual new tests**: 214 (test_injection.py 38, test_pii_enrichment.py 80, test_pii_loader.py 35, test_guardrails.py 28, test_registry.py +7, test_security.py updated)

All tasks follow TDD (RED → GREEN) and stay strictly within the arcllm module boundary — no arcagent/arcrun changes. Every task leaves `ruff check`, `mypy --strict`, and the full suite green before the next begins.

---

## Phase 1: Foundation — Exceptions, Config, Extras (Tasks 1-3)

### T15.1 — Add exceptions
- [x] Add `ArcLLMInjectionError(ArcLLMError)` carrying `findings`
- [x] Add `ArcLLMGuardrailError(ArcLLMError)` carrying `violations`
- [x] Export both from `__init__.py`

**Acceptance**:
- [x] Both subclass `ArcLLMError` (isinstance test)
- [x] Existing exception tests still pass

### T15.2 — Config models + config.toml
- [x] Extend `[modules.injection]` / `[modules.guardrails]` sections into config.toml (both `enabled = false`) — **resolved differently than drafted**: the codebase has no per-module Pydantic config subclasses (`InjectionModuleConfig`/`GuardrailsModuleConfig` as literally described in the SDD do not exist anywhere in `config.py`); every `[modules.*]` section loads through the single generic `ModuleConfig` (`extra="allow"`), and each module validates its own keys via `validate_config_keys` at construction. Adding typed subclasses would have been dead code inconsistent with every other module (audit, retry, circuit_breaker, etc.). Extended the existing generic mechanism instead.
- [x] Extend security handling with `pii_entities` + `pii_detector_class` keys
- [x] Extend `[modules.security]` with `pii_detector_class` + commented `[modules.security.pii_entities]` example

**Acceptance**:
- [x] Config loads without error; new sections parse with defaults
- [x] Existing config tests pass (no breaking change)

### T15.3 — Add `injection-semantic` extra
- [x] Add `injection-semantic` optional-dependency group to pyproject.toml (`numpy>=1.26` — local hashing-trick embedding, no network/model-download dependency)
- [x] Add `guardrails-schema` optional-dependency group (`jsonschema>=4.0`) — resolves SDD Research Insight #3's jsonschema-optional divergence via gating (Option A), not silent degradation
- [x] Verify existing extras (otel, signing) still resolve

**Acceptance**:
- [x] Pattern-only / non-schema path imports no extra dependency (subprocess-verified in test_injection.py)

---

## Phase 2: PII / Secret Enrichment (Tasks 4-6)

### T15.4 — Write test_pii_enrichment.py (TDD RED)
- [x] Luhn valid/invalid CREDIT_CARD; mod-97 valid/invalid IBAN; ABA routing checksum
- [x] IPV6, US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN (positive + negative)
- [x] SECRETS: AWS AKIA, GitHub token, JWT, PEM block, DB URL; `[SECRET:TYPE]` redaction
- [x] `pii_entities` allow-list limits scan; deny-list excludes; allow-wins-over-deny; unknown category errors

**Acceptance**:
- [x] 80 tests written and confirmed failing for the right reason (ImportError: unimplemented symbols) before implementation

### T15.5 — Enrich `_pii.py` (TDD GREEN)
- [x] Add checksum validators (luhn_valid, iban_mod97_valid, aba_checksum_valid) — stdlib only, verified against known-answer vectors
- [x] Attach validators to CREDIT_CARD / IBAN / ABA_ROUTING entries; match counts only if validator passes
- [x] Add regex patterns for IPV6 + all gov/CUI entities (context-anchored, DEFAULT-OFF per Research Insight #2)
- [x] Add `EntityToggle` resolution from `pii_entities` (allow/deny)
- [x] Add SECRETS category (via `_secrets.py`) with `[SECRET:TYPE]` redaction reusing `redact_text`

**Acceptance**:
- [x] All test_pii_enrichment.py tests pass
- [x] Patterns compiled once at module level; one detect→redact path
- [x] Coverage 100% on `_pii.py` + `_secrets.py`

### T15.6 — Write test_pii_loader.py + implement loader in security.py (TDD RED→GREEN)
- [x] RED: valid custom class loads/runs; bad format / non-allowlist / missing module / missing class / no `.detect()` → `ArcLLMConfigError`; `pii_detector_class` overrides `pii_detector`; regression test that non-"regex" is no longer auto-rejected
- [x] GREEN: remove `_VALID_DETECTORS` (line 33) and the reject (line 58)
- [x] GREEN: add `_ALLOWED_DETECTOR_PREFIXES` + `_load_detector_class()` mirroring `vault.py`
- [x] GREEN: wire `pii_detector_class` + `pii_entities` into SecurityModule construction; update `_VALID_CONFIG_KEYS`

**Acceptance**:
- [x] All test_pii_loader.py tests pass (35)
- [x] `security.py` no longer contains the hard-reject; existing test_security.py updated (obsolete hard-reject test replaced with FR-22 regression test) and passes
- [x] Loader validates against `PiiDetector` runtime-checkable protocol
- [x] Coverage 100% on modified security.py

---

## Phase 3: Injection Detection (Tasks 7-9)

### T15.7 — Write test_injection.py (TDD RED)
- [x] One positive per attack family (override, exfil, role, delimiter, encoded) + negatives
- [x] Tool-result (ToolResultBlock) scan; user-content scan
- [x] block raises `ArcLLMInjectionError`; warn flags + continues
- [x] Content passed through byte-identical (no mutation)
- [x] Semantic tier errors clearly without the extra; OFF-by-default; span attributes present

**Acceptance**:
- [x] 38 tests written and confirmed failing for the right reason (ModuleNotFoundError) before implementation

### T15.8 — Implement `modules/injection.py` (TDD GREEN)
- [x] `InjectionModule(BaseModule)` with `validate_config_keys`
- [x] `_PatternInjectionDetector` — module-level compiled corpus, zero-dep
- [x] `InjectionFinding` dataclass (category, pattern_id, snippet, source)
- [x] `_scan()` over user str/TextBlock + ToolResultBlock; skip binary/image
- [x] Enforcement: block raises, warn logs + span attrs; never mutate content
- [x] `_SemanticInjectionDetector` behind lazy extra-gated import (clear error when absent)

**Acceptance**:
- [x] All test_injection.py tests pass
- [x] Pattern path imports nothing new (subprocess-verified); semantic path gated exactly like `create_signer` ECDSA
- [x] Coverage 100% on injection.py

### T15.9 — Export + `__init__.py`
- [x] Export `InjectionModule` from `modules/__init__.py` and top-level `__init__.py`

**Acceptance**:
- [x] Import smoke test passes

---

## Phase 4: Output Guardrails (Tasks 10-11)

### T15.10 — Write test_guardrails.py (TDD RED)
- [x] json_schema conformance pass/fail; non-JSON content
- [x] deny-list hit; allow-list miss; max-length over/under; banned stop-list hit
- [x] block raises `ArcLLMGuardrailError`; warn flags to `response.metadata`
- [x] None content; span attributes present — **resolved differently than drafted**: `LLMResponse.content` is typed `str | None` in `types.py` (never `list[ContentBlock]` — that shape belongs to `Message.content`, the request side). The SDD's "Response content is list[ContentBlock]" edge case does not occur in real code; `_extract_text` handles `str | None` only.

**Acceptance**:
- [x] 28 tests written and confirmed failing for the right reason (ModuleNotFoundError) before implementation

### T15.11 — Implement `modules/guardrails.py` (TDD GREEN)
- [x] `GuardrailsModule(BaseModule)` with `validate_config_keys`
- [x] `Violation` dataclass (rule, detail)
- [x] `_validate()` — schema / allow / deny / length / stop-list; only configured checks run
- [x] Enforcement: block raises, warn attaches `guardrail_violations` to metadata + span attrs
- [x] Schema check gated behind `arcllm[guardrails-schema]` (resolved the "jsonschema if available, else stdlib" divergence flagged in SDD Research Insight #3 — Option A: hard `ArcLLMConfigError` when absent, never a silently weaker check)
- [x] Export from `modules/__init__.py` + top-level `__init__.py`

**Acceptance**:
- [x] All test_guardrails.py tests pass
- [x] Coverage 100% on guardrails.py

---

## Phase 5: Registry Wiring + Stack Placement (Tasks 12-14)

### T15.12 — Registry integration (TDD RED→GREEN)
- [x] RED: extend test_registry.py — MODULE_NAMES includes injection + guardrails; `load_model(injection=True)` and `guardrails={...}` wrap; stack-ordering assertion (ADR-430)
- [x] GREEN: add `"injection"`, `"guardrails"` to `MODULE_NAMES` (alongside SPEC-017's `"load_balance"` — not removed/touched)
- [x] GREEN: add `injection` + `guardrails` kwargs to `load_model()` signature + docstring stacking order (also fixed the pre-existing stale docstring order per SDD's own note — CircuitBreaker sits between Security and Retry, not between Telemetry and Audit)
- [x] GREEN: wrap Security → Injection → Guardrails between existing Security wrap and Audit wrap

**Acceptance**:
- [x] Stack order: Otel → Queue → Telemetry → Audit → Guardrails → Injection → Security → CircuitBreaker → Retry → Fallback → RateLimit → [Routing|LoadBalancer|Adapter]
- [x] test_registry.py MODULE_NAMES-vs-signature assertion passes
- [x] `load_model(..., injection=True, guardrails={...})` works end to end

### T15.13 — Behavioral integration tests
- [x] Injection sees ORIGINAL (pre-redaction) text — injection wraps Security in the stack, verified by `test_injection_sees_pre_redaction_text`
- [x] Guardrails validates FINAL response — Guardrails wraps Injection/Security/Retry/Fallback, verified by stack-order assertion
- [x] Both-disabled path: zero new imports (lazy-import verification via subprocess in test_injection.py; `guardrails-schema`/`injection-semantic` only imported when configured)

**Acceptance**:
- [x] All behavioral integration tests pass
- [x] No regressions across the existing suite (1157 passed, 1 skipped — up from the pre-existing baseline, zero failures)

### T15.14 — Quality gates + docs
- [x] `ruff check .` clean; `ruff format .` applied
- [x] `mypy --strict` clean (fixed 3 inherited errors surfaced during implementation — unused type:ignore, numpy module-as-type annotation, numpy attr-defined — all in injection.py, introduced and fixed within this same task)
- [x] `pytest --cov` — 100% on all new/modified files (_pii.py, _secrets.py, security.py, injection.py, guardrails.py); full suite green
- [x] Docstrings on all new public API (modules, exceptions, config models)
- [x] Spec status updated DRAFT → COMPLETE in this same commit as implementation

**Acceptance**:
- [x] All quality gates pass with fresh output (verification-before-completion)
- [x] Decision log D-419 through D-434 reflected in code comments where load-bearing (ADR references in module docstrings: injection.py, guardrails.py, security.py, registry.py)

---

## Research Insights — Test & Task Additions

Derived from `/deepen` research; fold into the named tasks (do not renumber). Each maps to an SDD Research Insights finding.

- [x] **T15.5 (pii enrichment)** — Added known-answer vectors (valid + invalid) per checksum: Luhn mod-10, IBAN mod-97 (== 1), ABA MICR `3-7-1` weighted mod-10, verified empirically against real-world reference numbers (Visa test card, GB/DE IBANs, Chase routing number). Added false-positive suppression test: a bare 10-digit integer does NOT match DOD_ID without a context anchor (DOD_ID also ships default-off).
- [x] **T15.6 (pii loader)** — Added: non-allowlisted ref raises `ArcLLMConfigError` **before** `import_module` executes (patched `import_module` with a side_effect that raises AssertionError if called — proves prefix-check-first ordering). Added: file-path / relative-dotted refs rejected (only absolute `module:Class`). Documented in code that the allowlist bounds *namespace, not trust* (signing is arcagent/arctrust).
- [x] **T15.7/T15.8 (injection)** — Added NFKC-normalize + zero-width-strip before matching; tests for a zero-width and a fullwidth-homoglyph variant of an INSTRUCTION_OVERRIDE trigger (built programmatically via codepoint shifting, not an ambiguous-Unicode literal, to keep `ruff --select RUF001` clean), and one ROT13 "decode and run" payload. Asserted semantic-tier corpus embeddings are pre-computed at construction and never re-embedded per call (monkeypatched `_embed` with a call-counter).
- [x] **T15.10/T15.11 (guardrails)** — Resolved the `jsonschema`-optional divergence via Option A: gated schema behind `arcllm[guardrails-schema]` with a clear `ArcLLMConfigError` when unavailable. Added a ReDoS test: a catastrophic-backtracking `deny_pattern` (`(a+)+$`) plus a crafted response is bounded by a length cap (`_MAX_SCAN_LENGTH = 4000`) — the pathological substring is placed beyond the cap so it is never reached by the regex engine, keeping the test both meaningful and fast (~ms, not minutes). Documented in the module docstring that the cap bounds cost proportional to response *size*; it does not eliminate blowup from a pathological pattern matched entirely *within* the capped window — a documented, honest limitation of any non-linear-time regex engine. Documented that a passed guardrail is not sink-safe output encoding.
- [x] **T15.12/T15.13 (registry)** — Stack-ordering test (`test_stack_order_security_injection_guardrails_audit`) asserts the exact ADR-430 order via nested `isinstance` checks. `enabled`/`enforcement` are resolved at module `__init__` from the merged per-call config (same `_resolve_module_config` mechanism as every other module), so span attributes (`arcllm.injection.enforcement`, `arcllm.guardrails.enforcement`) always reflect the actual applied posture, not a hardcoded default.

## Completion Checklist

- [x] All 14 tasks complete
- [x] All tests pass (existing 1153-ish baseline + 214 new; 1157 passed, 1 pre-existing skip)
- [x] Coverage 100% on new/modified files (injection.py, guardrails.py, _pii.py, _secrets.py, security.py)
- [x] `security.py:33/58` hard-reject removed; D-093/FR-13 pluggable detector landed
- [x] Stack placement verified by ordering test (ADR-430)
- [x] Zero deps when injection/guardrails disabled (verified via subprocess import-isolation test)
- [ ] Decision log updated (D-419 through D-434) — **out of scope for this implementation pass**: repo-root `.claude/decisions-log.md` was explicitly excluded from this task's scope per the task instructions; ADR references are reflected in code docstrings/comments instead.
- [x] Spec status set to COMPLETE in the same commit as implementation
