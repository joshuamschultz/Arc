# Spec 015 — Content Guardrails (Injection + PII/Secret Enrichment + Output Guardrails)

## Metadata

| Field | Value |
|-------|-------|
| Spec ID | 015 |
| Step | 15 |
| Feature | Content Guardrails (Injection Detection + PII/Secret Enrichment + Output Guardrails) |
| Status | COMPLETE |
| Created | 2026-07-05 |
| Author | Josh + Claude |

## Documents

| Document | Purpose |
|----------|---------|
| [PRD.md](PRD.md) | Problem, goals, requirements, NIST/OWASP mapping, user stories |
| [SDD.md](SDD.md) | Design, components, stack placement, ADRs, edge cases, boundaries |
| [PLAN.md](PLAN.md) | Phased TDD tasks with checkboxes and acceptance criteria |

## Decisions Log

| ID | Decision | Rationale |
|----|----------|-----------|
| D-419 | InjectionModule ships OFF by default, opt-in per call | Injection scanning is heuristic and can false-positive on legitimate prompts. arcllm is transport-only; flagging user intent is a policy concern the agent owns. Opt-in keeps the default path zero-cost and zero-surprise. |
| D-420 | Pattern-corpus tier is the zero-dep default; semantic tier is an optional `arcllm[injection-semantic]` extra | Curated attack-pattern regex/substring corpus catches the common attacks with no new deps. Embedding-cosine semantic detection is heavier (model + numpy) and belongs behind an extra, mirroring the `otel`/`signing` extras pattern. |
| D-421 | InjectionModule flags/blocks only — it never interprets, rewrites, or executes scanned content | Respects the arcllm transport-layer boundary. Interpreting "is this really an attack" is semantic judgement that lives in arcagent/arcrun. This is *why* the module is opt-in and off by default. |
| D-422 | Injection scans INBOUND user + tool-result content before it reaches the provider | Tool results are the ASI06 context-poisoning vector; user turns are the LLM01 vector. Both are untrusted-adjacent and must be scanned pre-provider, while the text is still original (pre-redaction). |
| D-423 | Secret scanner folded into the default detector as a togglable `SECRETS` category, redacting to `[SECRET:TYPE]` | Secrets are a leak class, not a separate subsystem. Reusing the existing detect→redact path (D-094 tag format) keeps one code path. Toggleable so callers who route secrets deliberately can opt out. |
| D-424 | Add checksum VALIDATORS (Luhn / mod-97 / ABA) that gate entity matches before they count | Raw 16-digit / IBAN / routing regexes false-positive on order numbers and IDs. A cheap arithmetic validator on the captured digits cuts noise without adding deps. |
| D-425 | Add gov/CUI entity types: US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN, IPV6 | Federal deployments handle CUI beyond SSN/CC/email. These are the entities NIST SI-10 / SC-28 output filtering must catch on DOE machines. |
| D-426 | Entity categories are individually toggleable via a `pii_entities` allow/deny config | Feature toggles via config, not code branches (CLAUDE.md). A lab may need MRN off and DOD_ID on; config expresses that without forking the detector. |
| D-427 | Implement the allowlisted `pii_detector_class="module:Class"` loader that spec 012 D-093/FR-13 specced but never built, mirroring `vault.py` exactly | `security.py` today hard-rejects any detector != "regex" (`_VALID_DETECTORS`, line 58). Reusing `vault.py`'s allowlisted-prefix importlib loader makes bring-your-own spaCy/Presidio real with zero new core deps and the same supply-chain guard (ASI04). |
| D-428 | New GuardrailsModule validates the RESPONSE per call via a `guardrails={...}` kwarg | Structural output validation (schema conformance, allow/deny regex, length cap, banned stop-list) is a transport concern once the response bytes are in hand. Per-call kwarg mirrors routing's `classification` mechanism. |
| D-429 | Semantic guardrails (grounding, factual correctness, toxicity judgement) are explicitly OUT OF SCOPE | Those require model reasoning and agent context — they belong in arcagent/arcrun. arcllm validates *structure*, not *meaning*. Crisp boundary keeps the nucleus a transport layer. |
| D-430 | Stack placement: Injection above Security (sees original text); Guardrails high, just inside Audit (validates the final resolved response) | Injection must see pre-redaction text to match encoded attacks. Guardrails must run after Retry/Fallback resolve, so it validates exactly what the caller receives, and inside Audit so the audit trail records the guardrail verdict. |
| D-431 | Two new exceptions: `ArcLLMInjectionError`, `ArcLLMGuardrailError`, both subclassing `ArcLLMError` | Distinct exception types let callers catch block-mode failures precisely (retry, alert, human gate) without string-matching. |
| D-432 | Config layout: new `[modules.injection]`, `[modules.guardrails]`; extend `[modules.security]` with `pii_entities` + `pii_detector_class` | Each module owns its TOML section (existing convention). Security enrichment lives under the existing security section — no behavior split across files. |
| D-433 | New optional extra `arcllm[injection-semantic]` (embeddings backend) | Semantic tier's numpy/model deps must never load when injection is pattern-only or disabled. Zero-dep-when-disabled is a hard rule. |
| D-434 | Both new modules use `enforcement="block"\|"warn"`: warn = flag in trace + OTel attr + continue; block = raise | Same enforcement vocabulary as RoutingModule. Warn lets federal ops run in observe-only before flipping to block. |

## Cross-References

- **Completes prior work**: Spec 012 D-093 / FR-13 ("Custom PII detector class loadable via pii_detector_class") was specced but never implemented — `modules/security.py:33` (`_VALID_DETECTORS = {"regex"}`) and `:58` still hard-reject any non-regex detector. D-427 lands it, mirroring the loader that spec 012 *did* ship in `vault.py` (`VaultResolver.from_config`, allowlisted-prefix importlib).
- **Prior decisions extended**: D-094 (type-tagged `[PII:TYPE]` redaction — reused for `[SECRET:TYPE]`), D-090 (extras pattern — reused for `injection-semantic`), D-099 (Audit → Security → Retry stacking — Injection and Guardrails slot into it).
- **CLAUDE.md threat table**: LLM01 (Prompt Injection), LLM02 (Sensitive Info Disclosure), LLM05 (Improper Output Handling), LLM06, ASI04 (Agentic Supply Chain — detector loader allowlist), ASI06 (Context/Memory Poisoning via tool results).
- **Related specs**: 010-audit-trail (audit records guardrail/injection verdicts), 011-otel-export (spans include content-guardrail attributes), 014-* routing (per-call kwarg + enforcement pattern reused).

## Learnings

- **`InjectionModuleConfig`/`GuardrailsModuleConfig` as literal Pydantic subclasses don't exist anywhere in the codebase** — `config.py`'s `GlobalConfig.modules` is `dict[str, ModuleConfig]` uniformly; every module (audit, retry, circuit_breaker, security, ...) uses the single generic `ModuleConfig` (`extra="allow"`) and validates its own keys via `validate_config_keys` at construction. Introducing per-module typed config classes would have been a parallel, unused mechanism. Extended the existing generic pattern instead — consistent with every other module in the file.
- **`LLMResponse.content` is `str | None`, never `list[ContentBlock]`** — that shape belongs to `Message.content` (the *request* side). The SDD's GuardrailsModule edge-case table ("Response content is list[ContentBlock]") described an input shape that cannot occur in real code. `_extract_text` handles `str | None` only; the corresponding test was rewritten to a valid content shape.
- **Length-capping alone does not defeat genuine catastrophic regex backtracking** — empirically verified `(a+)+$` against as few as 25-30 repeated characters already takes 1-36+ seconds in Python's `re`. A length cap (`_MAX_SCAN_LENGTH = 4000`) bounds cost *proportional to attacker-controlled response size* (the actual LLM10 unbounded-consumption concern — an attacker cannot grow compute merely by growing the response past the cap), but does NOT eliminate blowup from a pathological pattern matched entirely within the capped window. Documented this honestly in the module docstring; the ReDoS test places the pathological substring beyond the cap so it demonstrates the real, bounded property without hanging the test suite.
- **Semantic injection tier uses a deterministic hashing-trick bag-of-words embedding, not a trained model** — kept the `injection-semantic` extra to `numpy` only (no model download, no network call), per SDD's explicit "strongly prefer a local model over a network embedding call." This catches near-verbatim paraphrasing of the curated corpus but is not a substitute for a real sentence-embedding model; documented as a floor-level implementation.
- **Checksum vectors verified empirically before writing tests** — Luhn (Visa test number `4111111111111111`), IBAN mod-97 (`GB82WEST12345698765432`, `DE89370400440532013000`), and ABA MICR 3-7-1 (`021000021`, Chase NY) all confirmed against known-answer references before being hardcoded into `test_pii_enrichment.py`.
- **Ambiguous-Unicode test literals (RUF001) required programmatic construction, not `noqa`** — the fullwidth-homoglyph evasion test builds its payload via codepoint shifting (`chr(ord(c) + 0xFEE0)`) rather than embedding an ambiguous-looking string literal, keeping `ruff check` clean without suppression comments.
