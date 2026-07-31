# SDD — Content Guardrails (Step 15)

## Design Overview

Step 15 adds two new per-invoke modules and enriches the existing security surface, all within the arcllm transport boundary:

1. **InjectionModule** (`modules/injection.py`) — Opt-in, OFF by default. Scans **inbound** user + tool-result content *before* it reaches the provider for prompt-injection attacks. Default tier is a curated zero-dep attack-pattern corpus; an optional semantic tier (embedding cosine similarity) lives behind `arcllm[injection-semantic]`. Flags (warn) or blocks (raise) only — it never interprets or executes content.

2. **PII / Secret enrichment** (`_pii.py` + `modules/security.py` + config) — Adds checksum validators (Luhn / mod-97 / ABA), gov/CUI entity types, a togglable `SECRETS` category, per-category `pii_entities` toggles, and — completing spec 012 D-093/FR-13 — an allowlisted `pii_detector_class` loader mirroring `vault.py`. Removes the current `_VALID_DETECTORS = {"regex"}` hard-reject.

3. **GuardrailsModule** (`modules/guardrails.py`) — Opt-in per call. Validates the **final resolved response** (after Retry/Fallback) for JSON-schema conformance, regex allow/deny, max-length, and banned-content stop-list. Structural only — semantic judgement stays in arcagent/arcrun.

### Architecture Fit

Injection must see the **original** inbound text, so it sits *above* Security (which redacts). Guardrails must validate the **final** response the caller receives, so it sits *high* in the stack, just inside Audit (so the audit trail records the verdict) but outside every module that could still change the response (Retry, Fallback, Security).

```
Agent calls load_model("anthropic", injection=True, guardrails={...})
  │
  └── Module stack (outermost first):
      Otel
       └─ Queue
           └─ Telemetry
               └─ Audit ............... records injection + guardrail verdicts
                   └─ Guardrails ...... validates FINAL response (post-Retry/Fallback)   [NEW]
                       └─ Injection ... scans inbound user + tool-result (ORIGINAL text)  [NEW]
                           └─ Security . redacts PII/secrets + signs
                               └─ CircuitBreaker
                                   └─ Retry
                                       └─ Fallback
                                           └─ RateLimit
                                               └─ [Routing | Adapter]
```

Per-invoke flow inside the two new modules:

```
GuardrailsModule.invoke(messages, tools, **kwargs):
  with _span("guardrails"):
    response = await inner.invoke(messages, tools, **kwargs)   # Retry/Fallback/Security/Injection resolve here
    violations = validate(response, expected_response_format=kwargs)
    set span attributes (verdict, violation count)
    if violations:
      if enforcement == "block": raise ArcLLMGuardrailError(violations)
      else: flag in trace + attach to response.metadata; return response
    return response

InjectionModule.invoke(messages, tools, **kwargs):
  with _span("injection"):
    findings = scan_inbound(messages)          # user text + ToolResultBlock text
    set span attributes (verdict, pattern hits)
    if findings:
      if enforcement == "block": raise ArcLLMInjectionError(findings)
      else: log + record findings in trace
    return await inner.invoke(messages, tools, **kwargs)   # messages passed through byte-identical
```

## Directory Map

### New Files

```
src/arcllm/
├── modules/
│   ├── injection.py        # InjectionModule + pattern corpus + optional semantic tier hook
│   └── guardrails.py       # GuardrailsModule + structural validators
└── _secrets.py             # Secret patterns + SecretMatch (kept out of _pii core for clarity)
```

### Modified Files

```
src/arcllm/
├── _pii.py                 # Checksum validators, gov/CUI entities, IPV6, SECRETS category, pii_entities toggle
├── modules/security.py     # Remove _VALID_DETECTORS hard-reject; add allowlisted pii_detector_class loader
├── exceptions.py           # ArcLLMInjectionError, ArcLLMGuardrailError
├── config.py               # InjectionModuleConfig, GuardrailsModuleConfig; security keys pii_entities, pii_detector_class
├── config.toml             # [modules.injection], [modules.guardrails]; extend [modules.security]
├── registry.py             # injection/guardrails kwargs, MODULE_NAMES, stack placement
├── pyproject.toml          # injection-semantic extra
└── __init__.py             # Export InjectionModule, GuardrailsModule, new exceptions
```

### New Test Files

```
tests/
├── test_injection.py       # Pattern corpus + tool-result scan + enforcement + zero-dep + semantic-error
├── test_pii_enrichment.py  # Checksums, gov/CUI entities, IPV6, SECRETS, pii_entities toggles
├── test_pii_loader.py      # Allowlisted pii_detector_class loading + rejection paths
└── test_guardrails.py      # Schema/regex/length/stop-list + enforcement + span attributes
```

## Component Design

### 1. InjectionModule (`modules/injection.py`)

```
Class: InjectionModule(BaseModule)
  Constructor(config: dict, inner: LLMProvider):
    validate_config_keys(config, _VALID_CONFIG_KEYS, "InjectionModule")
    enforcement: "block" | "warn"   (default "block"; but module is OFF unless enabled)
    tier: "pattern" | "semantic"    (default "pattern")
    scan_tool_results: bool         (default True)   # ASI06
    scan_user: bool                 (default True)   # LLM01
    _detector = _PatternInjectionDetector()  or  _SemanticInjectionDetector() (lazy, extra-gated)

  invoke(messages, tools, **kwargs) -> LLMResponse:
    with _span("injection"):
      findings = self._scan(messages)            # list[InjectionFinding]
      span.set_attribute("arcllm.injection.hits", len(findings))
      span.set_attribute("arcllm.injection.enforcement", self._enforcement)
      if findings:
        if enforcement == "block": raise ArcLLMInjectionError(findings)
        logger.warning("arcllm.injection.flagged", extra={...})
      return await self._inner.invoke(messages, tools, **kwargs)   # never mutate content

Dataclass: InjectionFinding
  Fields: category: str, pattern_id: str, snippet: str, source: str  # "user" | "tool_result"
```

**Pattern corpus** (`_PatternInjectionDetector`, zero-dep, module-level compiled):

| category | example trigger |
|----------|-----------------|
| INSTRUCTION_OVERRIDE | "ignore (all )?previous instructions", "disregard the above" |
| SYSTEM_PROMPT_EXFIL | "repeat the system prompt", "print your instructions", "reveal your system message" |
| ROLE_OVERRIDE | "you are now", "act as (an unrestricted|DAN)", "new persona:" |
| DELIMITER_INJECTION | injected `</system>`, `<|im_start|>`, fenced role blocks in user text |
| ENCODED_INSTRUCTION | base64/hex blobs adjacent to "decode and run/execute" |

**Semantic tier** (`_SemanticInjectionDetector`, `arcllm[injection-semantic]`): embeds the scanned span, compares cosine similarity against a known-attack embedding corpus, flags above a threshold. Import of the embeddings backend is lazy and gated exactly like `create_signer`'s ECDSA branch — a clear `ArcLLMConfigError` when the extra is absent.

### Research Insights

Reference tools converge on **layered** injection defense; arcllm's pattern corpus is the correct zero-dep *floor* of that stack, not the whole thing.

1. **The pattern corpus == the "heuristics" layer only — name it as such, don't oversell "block".** Rebuff's design is heuristics → dedicated LLM detector → vector-DB of known signatures → canary tokens; NeMo runs an intent-classification rail; Lakera ships threat-intel harvested from its Gandalf game at <50ms. arcllm ships only the first (heuristics) layer by design. That is the right choice (zero-dep, deterministic), but the AST-validator solutions learning applies directly: **a single static layer will be bypassed** — encoding, novel phrasing, and template abuse all evade fixed patterns, exactly as AST walkers accrued CVEs. This is *why* ADR-421 (flag/block only, byte-identical passthrough, no sanitize) is correct: sanitizing would give false confidence in an incomplete layer. Don't document "block" as protection; document it as one signal. (a) Security: heuristic floor, defense-in-depth completed upstream. (c) Boundary: the LLM/vector/canary layers belong to arcagent/arcrun, not arcllm. Note Rebuff was **archived May 2025** — borrow its taxonomy, never take a runtime dep. [Rebuff status](https://appsecsanta.com/rebuff), [Introl layered defense](https://introl.com/blog/llm-security-prompt-injection-defense-production-guide-2025), [AST-validator-not-enough](.claude/solutions/security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md)

2. **Normalize before matching or the corpus is trivially bypassed (add to `_scan`).** garak's encoding probe family alone spans base64/base32/base16/hex/ROT13/Morse/Braille/ASCII85 **plus Unicode smuggling** (zero-width joiners, homoglyphs); a bare regex for `"ignore previous instructions"` misses `ignore​previous`. Apply **NFKC normalization + zero-width strip** to each scanned span before pattern match (mirrors the scheduler-hardening NFKC learning already used for lineage/classification). Keep the ENCODED_INSTRUCTION adjacency-verb requirement to hold false positives down, but broaden it past base64/hex to ROT13 and add a homoglyph/zero-width flag. The normalized text is scanned; the passthrough to the provider stays byte-identical (ADR-421 preserved). (a) Security: closes the cheapest evasion class. [garak encoding probes](https://github.com/NVIDIA/garak), [ChatInject template abuse](https://arxiv.org/pdf/2509.22830)

3. **Semantic tier is a real per-invoke latency/cost ceiling — off by default is mandatory, and load-time corpus embedding is not enough.** Cosine-similarity detection embeds every scanned inbound span on the hot path. At 1000s of concurrent agents, an **embedding call per invoke** (API or even local model inference) adds latency and an unbounded-consumption cost surface (LLM10). Constraints for the semantic tier: pre-compute and cache the *known-attack corpus* embeddings at construction (never per-call); embed only the inbound span at call time; strongly prefer a local model over a network embedding call (a remote call doubles round-trips and adds a failure/timeout domain — needs its own circuit breaker); and treat the corpus itself as an LLM08 supply-chain artifact (validate provenance, it must be signed/immutable). (b) Scalability: this is the one component in Spec 015 with a per-call compute ceiling — flag it explicitly and keep `tier="pattern"` the default. (c) Boundary: corpus curation/refresh is an offline pipeline, not runtime arcllm (SDD already says this). [OWASP LLM01:2025](https://genai.owasp.org/llmrisk/llm01-prompt-injection/), [LLM08 embedding weaknesses]

4. **Tool-result (indirect) scanning is the highest-value part of this module — but detection is necessarily incomplete, so warn-default + arcrun response is right.** OWASP LLM01:2025 and current in-the-wild reports rank indirect injection via retrieved/tool content (ASI06) as the primary agentic vector; scanning `ToolResultBlock` pre-provider (ADR-422) directly targets it. But no pattern set catches a determined indirect payload, so the module must only *surface* the signal — deciding to abort/quarantine/re-plan is arcrun loop policy. This matches ADR-419's warn-by-default and the boundary section. (c) Boundary: cross-turn lineage ("which tool poisoned this") stays arcagent memory-integrity. [OWASP LLM01:2025](https://genai.owasp.org/llmrisk/llm01-prompt-injection/), [Securance indirect-injection 2026](https://www.securance.com/blog/prompt-injection-the-owasp-1-ai-threat-in-2026/)

### 2. Enriched RegexPiiDetector (`_pii.py`)

```
_BUILTIN_PATTERNS extended (each entry gains an optional validator):
  ("CREDIT_CARD", pattern, validator=luhn_valid)
  ("IBAN",        pattern, validator=iban_mod97_valid)
  ("ABA_ROUTING", pattern, validator=aba_checksum_valid)
  ("IPV6",        pattern, validator=None)
  ("US_PASSPORT", pattern, validator=None)
  ("US_DRIVERS_LICENSE", pattern, validator=None)
  ("DOD_ID",      pattern (10-digit EDIPI), validator=None)
  ("CAC",         pattern, validator=None)
  ("BANK_ACCOUNT",pattern, validator=None)
  ("DOB",         pattern, validator=None)
  ("MRN",         pattern, validator=None)
  ... plus existing SSN, EMAIL, PHONE, IPV4

Validators (stdlib arithmetic, no deps):
  luhn_valid(digits) -> bool          # mod-10
  iban_mod97_valid(s) -> bool         # rearrange + mod 97 == 1
  aba_checksum_valid(digits) -> bool  # 3(d1+d4+d7)+7(d2+d5+d8)+(d3+d6+d9) % 10 == 0

RegexPiiDetector.__init__(custom_patterns=None, entities: EntityToggle | None = None):
  # entities carries the allow/deny decision from pii_entities config.
  # A match only counts if (a) its regex hits, (b) its validator (if any) passes,
  # and (c) its category is enabled by the entity toggle.

detect(text) -> list[PiiMatch]:
  candidate matches filtered by validator, then by entity toggle, then de-overlapped (existing logic)
```

**`pii_entities` toggle** resolves as: if an allow-list is given, only those categories scan; else if a deny-list is given, all-but-denied scan; else all categories scan. Feature toggle via config, not code branches (CLAUDE.md).

### 3. Secret Scanner (`_secrets.py`, folded into detector as `SECRETS`)

```
_SECRET_PATTERNS: list[(secret_type, re.Pattern)]
  ("AWS_ACCESS_KEY", r"\bAKIA[0-9A-Z]{16}\b")
  ("GITHUB_TOKEN",   r"\bgh[posu]_[A-Za-z0-9]{36,}\b")
  ("JWT",            r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
  ("PEM_BLOCK",      r"-----BEGIN [A-Z ]+-----")
  ("DB_URL",         r"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?)://[^\s]+")

Redaction reuses redact_text with a [SECRET:TYPE] tag (SECRETS category emits SECRET: prefix
instead of PII: — one branch on the tag namespace, driven by the category, not a separate path).
```

The `SECRETS` category is a single toggleable entity like any other, so `pii_entities` can enable/disable it. This keeps **one detect→redact code path** (D-423).

### Research Insights (PII / Secret enrichment — sections 2 & 3)

Checksums are the precision win; the no-checksum gov entities are the false-positive risk to manage.

1. **Checksum rules confirmed exact — lock each with a known-answer test.** Luhn = mod-10 (double every second digit right-to-left, sum, total % 10 == 0); IBAN = move first 4 chars to the end, letters→(rank+9), the big integer % 97 must equal **1**; ABA = 9-digit MICR weighted `3(d1+d4+d7)+7(d2+d5+d8)+1(d3+d6+d9)` % 10 == 0 — matches the SDD arithmetic exactly. All are single-transposition-detecting and stdlib-only. (a) Security: cuts CREDIT_CARD/IBAN/ABA false positives (order numbers, IDs) with zero deps — deterministic precision, the ADR-424 rationale holds. (b) Scalability: pure arithmetic, no ceiling. Add valid+invalid KAT vectors per algorithm. [Luhn (Wikipedia)](https://en.wikipedia.org/wiki/Luhn_algorithm), [IBAN mod-97](https://medium.com/@matlabb/iban-structure-and-mod-97-validation-algorithm-719e3d4db5f2)

2. **No-checksum gov/CUI entities (EDIPI, CAC, passport, DL, MRN, DOB, BANK_ACCOUNT) are a real FP source — gate them on context anchors or ship default-off.** A bare 10-digit EDIPI regex also matches phone numbers, order IDs, epoch timestamps; US driver's-license formats vary wildly by state (no single canonical shape); passports vary by country. DoD's own guidance notes the DoD ID number alone is *low* breach risk (it is CUI only in aggregate). Recommendation for the owner: require a proximate keyword anchor (`EDIPI`, `DoD ID`, `passport no`, `DL#`, `MRN`) for these entities, **or** leave them out of the default `pii_entities` allow-set so a deployment opts in per lab. This is the ADR-425/426 "tune per deployment" intent made safe. (a) Security: high recall without drowning redaction in noise; noisy PII filters get disabled, which is worse than tuned ones. [DoD CUI PII FAQ](https://www.dodcui.mil/Frequently-Asked-Questions/Privacy-PII/), [DoD ID as PII](https://www.doncio.navy.mil/chips/ArticleDetails.aspx?ID=4034)

3. **Presidio is the correct mental model for the pluggable detector — and confirms the layering.** Presidio's `EntityRecognizer` base exposes exactly two extension points, `load()` + `analyze()`, which maps 1:1 onto the `PiiDetector.detect()` runtime-checkable protocol. Critically, Presidio *itself* layers regex → checksum/context validation → NER; arcllm's regex+checksum tier is the deterministic floor, and NER (spaCy/transformers) is the heavy upgrade that must ride the pluggable loader — never the built-in default. (b) Scalability: spaCy/transformer NER loads a model and runs per-call inference (hundreds of MB, real latency); at 1000s of agents it cannot be the default path — keep it opt-in via `pii_detector_class`. (c) Boundary: arcllm loads the class; the model artifact is a supply-chain object (ASI04, see loader insights). [Presidio custom recognizers](https://microsoft.github.io/presidio/analyzer/developing_recognizers/), [Presidio registry-from-file](https://microsoft.github.io/presidio/analyzer/recognizer_registry_provider/)

4. **SECRETS is pattern-only (no entropy tier) — correct for a floor, but document the gap.** Reference secret scanners (truffleHog, git-secrets) combine fixed patterns with Shannon-entropy heuristics to catch generic high-entropy tokens. arcllm's `SECRETS` matches only structured prefixes (AKIA, `gh[posu]_`, JWT, PEM, DB-URL). That is a clean single detect→redact path (ADR-423) and the right scope, but a generic 40-char API key with no known prefix won't redact. Document as an accepted limitation, not silent. (c) Boundary: fine to stay in arcllm — it is output filtering (LLM02), not agent policy.

### 4. Pluggable Detector-Class Loader (`modules/security.py`)

Removes lines 33 (`_VALID_DETECTORS = {"regex"}`) and 58 (the reject). Adds a loader that mirrors `vault.py`'s `VaultResolver.from_config` exactly:

```
_ALLOWED_DETECTOR_PREFIXES = ("arcllm.", "arcagent.", "arcpii.")

def _load_detector_class(ref: str, custom_patterns, entities) -> PiiDetector:
  if ":" not in ref: raise ArcLLMConfigError("pii_detector_class must be 'module:Class'")
  module_path, class_name = ref.rsplit(":", 1)
  if not any(module_path.startswith(p) for p in _ALLOWED_DETECTOR_PREFIXES):
    raise ArcLLMConfigError("not in allowlist ...")           # ASI04
  module = importlib.import_module(module_path)  # ImportError -> ArcLLMConfigError
  cls = getattr(module, class_name, None)        # missing -> ArcLLMConfigError
  instance = cls(...)                            # construct
  if not isinstance(instance, PiiDetector):      # runtime_checkable protocol -> .detect present
    raise ArcLLMConfigError("does not implement PiiDetector.detect()")
  return instance
```

SecurityModule construction: if `pii_detector_class` is set, load it; elif `pii_detector == "regex"` (or unset), build `RegexPiiDetector`. No hard-coded `_VALID_DETECTORS` gate remains.

### Research Insights (allowlisted loader — ASI04/ASI05)

Mirroring `vault.py` is right; the subtlety is *when* the guard fires and what "allowlisted" does and does not buy.

1. **Order is load-bearing: check the prefix allowlist BEFORE `import_module`, because import runs top-level code (import-time RCE).** `importlib.import_module` executes the target module's top-level statements the instant it is imported — before any `isinstance(instance, PiiDetector)` check. So the allowlist gate must run *first* (the SDD pseudocode does this correctly: prefix check → import → getattr → isinstance). Add a regression test asserting a non-allowlisted ref raises *without* the module ever being imported (patch `import_module` to fail the test if called). (a) Security: this ordering is the whole defense — reversing it defeats it. [Python importlib guidance](https://docs.python.org/3/library/importlib.html)

2. **Allowlist ≠ trust — it narrows the namespace, it does not sandbox.** The AST-validator learning applies: a prefix allowlist is a single static layer. If an attacker can publish/shadow a package under `arcllm.*`/`arcagent.*`/`arcpii.*` (typo-squat, compromised dep, writable sys.path), an allowlisted import is still full-privilege RCE — the detector class runs in-process with no isolation. The real second layer is the Four Pillars **Sign**: the loaded package/class should be signature-verified before load. That verification lives in arctrust/arcagent, not arcllm, so arcllm must (a) document explicitly that the allowlist bounds *namespace, not trust*, and (b) accept only `module:Class` refs — **never file paths** and never relative/traversal dotted names — so no arbitrary-file import is possible. (c) Boundary: signing enforcement is arcagent/arctrust; arcllm provides the allowlist + protocol check only. [AST-validator-not-enough](.claude/solutions/security-issues/2026-04-18-ast-validator-is-not-enough-defense-in-depth.md), [importlib allowlist practice](https://www.bomberbot.com/python/mastering-dynamic-module-and-class-loading-in-python-a-deep-dive-for-enthusiasts/)

3. **Loader runs once at construction — zero per-call cost.** Unlike the semantic injection tier, `_load_detector_class` fires only during `SecurityModule.__init__`, so a heavy detector (Presidio/spaCy) pays its model-load cost once per agent process, not per invoke. (b) Scalability: no hot-path ceiling; the per-call cost is whatever the loaded `detect()` does — which is why NER detectors are opt-in.

### 5. GuardrailsModule (`modules/guardrails.py`)

```
Class: GuardrailsModule(BaseModule)
  Constructor(config, inner):
    validate_config_keys(config, _VALID_CONFIG_KEYS, "GuardrailsModule")
    enforcement: "block" | "warn"     (default "block")
    json_schema: dict | None
    allow_patterns / deny_patterns: list[str]  (compiled once)
    max_length: int | None
    banned_content: list[str]         (stop-list, case-insensitive)

  invoke(messages, tools, **kwargs) -> LLMResponse:
    with _span("guardrails"):
      response = await self._inner.invoke(messages, tools, **kwargs)
      violations = self._validate(response, kwargs.get("response_format"))
      span.set_attribute("arcllm.guardrails.violations", len(violations))
      if violations:
        if enforcement == "block": raise ArcLLMGuardrailError(violations)
        response = self._flag(response, violations)   # attach to response.metadata["guardrail_violations"]
      return response

  _validate(response, response_format) -> list[Violation]:
    checks (only those configured run):
      - json_schema: when response_format expects json/schema, parse content and validate structurally
      - deny_patterns: any match -> violation
      - allow_patterns: no match -> violation
      - max_length: len(content) > cap -> violation
      - banned_content: any stop-list phrase present -> violation

Dataclass: Violation
  Fields: rule: str, detail: str
```

Per-call configuration via `guardrails={...}` merges over `[modules.guardrails]` defaults exactly like every other module kwarg (`_resolve_module_config`).

### Research Insights (structural output guardrails — LLM05)

The structural/semantic split is industry-correct; two build details carry security weight.

1. **Structure-only is the right line, and the field validates ADR-429.** Native structured-output/constrained decoding guarantees only *shape* — correct field names, types, required-present — never field-level semantics (a `rating` in 1–5, a real email, a non-hallucinated confidence). Semantic validity "always requires an additional layer" that needs the task's world model, which is exactly arcagent/arcrun. arcllm doing schema/regex/length/stop-list only is the correct nucleus boundary. (c) Boundary: semantic guardrails (grounding, toxicity, factuality) stay upstream — locked by ADR-429. [structured-output schema vs semantic](https://collinwilkins.com/articles/structured-output), [AI-security output-validation patterns](https://www.aisecurityinpractice.com/defend-and-harden/llm-output-validation-patterns/)

2. **Guardrails ≠ output encoding — flag, don't imply sanitization for a sink.** LLM05 harms (XSS/SSRF/RCE downstream) come from *unencoded* output reaching a specific sink; a passed deny-regex/length/schema check does **not** make output safe to render in HTML, run in a shell, or feed a DB. arcllm flags/blocks structurally; the *consumer* must still context-encode for its sink. Document this so a caller does not treat "guardrails passed" as "safe to execute." (a) Security: prevents a false-safety read of LLM05. [OWASP LLM05:2025 improper output handling](https://www.aisecurityinpractice.com/defend-and-harden/llm-output-validation-patterns/)

3. **The "jsonschema if available, else stdlib" plan creates a non-deterministic guardrail — OWNER DECISION.** PLAN T15.11 falls back to a weaker stdlib structural check when `jsonschema` is absent, so the same schema can pass without the lib and fail with it (or vice-versa). A guardrail whose verdict depends on which optional deps are installed is a security-relevant inconsistency. Recommend either (A) gate JSON-schema validation behind an extra (`arcllm[guardrails-schema]`) with a clear `ArcLLMConfigError` when unavailable — mirroring the `injection-semantic` pattern — or (B) vendor one minimal validator so behavior is identical everywhere. Do not silently degrade. Add a test asserting identical verdicts across the with/without-lib paths (or that the without path errors, not degrades). (a) Security: deterministic enforcement. [Guardrails-AI JSON-schema validation](https://abacktools.com/blog/guardrails-ai-structured-output-validation-json-schema)

4. **Operator-supplied `deny_patterns`/`allow_patterns` are a ReDoS surface on model output.** Regexes run against attacker-influenceable LLM output; a catastrophic-backtracking pattern plus a crafted response is a DoS (LLM10 unbounded consumption). Compile once (SDD says so) and additionally bound it: cap the scanned content length before regex, and treat operator patterns as trusted-but-fenced. (b) Scalability: otherwise one bad pattern stalls a worker across 1000s of calls; all other checks (len/parse/stop-list) are clean O(n). Add a pathological-pattern test with a timeout assertion.

### 6. Exceptions (`exceptions.py`)

```
class ArcLLMInjectionError(ArcLLMError):
    def __init__(self, findings: list[InjectionFinding]) -> None:
        self.findings = findings
        super().__init__(f"Prompt injection detected: {len(findings)} finding(s)")

class ArcLLMGuardrailError(ArcLLMError):
    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        super().__init__(f"Output guardrail violation: {len(violations)} rule(s)")
```

Both carry structured detail so callers can branch (retry, alert, human gate) without string-matching (D-431).

### 7. Config Models (`config.py`)

```
class InjectionModuleConfig(ModuleConfig):   # extra="allow" inherited
    enforcement: str = "block"
    tier: str = "pattern"                     # "pattern" | "semantic"
    scan_user: bool = True
    scan_tool_results: bool = True

class GuardrailsModuleConfig(ModuleConfig):
    enforcement: str = "block"
    max_length: int = 0                        # 0 = no cap
    banned_content: list[str] = []
    # json_schema / allow_patterns / deny_patterns typically supplied per-call

# Security section gains (validated in SecurityModule, ModuleConfig already extra="allow"):
    pii_entities: dict[str, list[str]] = {}    # {"allow": [...]} or {"deny": [...]}
    pii_detector_class: str = ""               # "module:Class" or "" for built-in
```

Because `ModuleConfig` is `extra="allow"`, TOML round-trips cleanly; the modules do strict `validate_config_keys` at construction so typos fail loud.

### 8. Config TOML (`config.toml`)

```toml
# --- Prompt-injection detection (inbound, OFF by default) ---
[modules.injection]
enabled = false
enforcement = "warn"        # observe before enforce
tier = "pattern"            # "semantic" requires arcllm[injection-semantic]
scan_user = true
scan_tool_results = true

# --- Structural output guardrails (per-call) ---
[modules.guardrails]
enabled = false
enforcement = "block"
max_length = 0              # 0 = uncapped
banned_content = []

# --- Security: extend existing section ---
[modules.security]
# ... existing keys ...
pii_detector_class = ""     # "" = built-in RegexPiiDetector; else "module:Class"
[modules.security.pii_entities]
# allow = ["SSN", "EMAIL", "SECRETS"]   # if present, only these scan
# deny  = ["IPV4"]                        # else all-but-these scan
```

### 9. Registry Integration (`registry.py`)

- Add `"injection"` and `"guardrails"` to `MODULE_NAMES`.
- Add `injection` and `guardrails` kwargs to `load_model()`.
- Wrap in the correct order. The stack builds innermost→outermost, so wrapping order is: Security (existing) → Injection → Guardrails, placed between the existing Security wrap and the Audit wrap:

```
security_config  -> SecurityModule(...)     # existing
injection_config -> InjectionModule(...)    # NEW: wraps security-wrapped result
guardrails_config-> GuardrailsModule(...)   # NEW: wraps injection-wrapped result
audit_config     -> AuditModule(...)        # existing, now wraps guardrails
```

### Research Insights (registry / stack placement)

**Enforcement mode must be resolved at module construction and the audit must record the *actual* verdict+mode — the tier-flow-through-construction lesson applies (AU-2).** `enabled` and `enforcement` ("block"/"warn") for both new modules are process-lifetime, tier-derived posture values read on every call. The solutions archive documents the exact failure this invites: enforcement wired correctly at the factory while a hardcoded default reaches the per-record audit context, producing an audit trail that *systematically misreports posture* even when enforcement is right — a NIST 800-53 AU-2 compliance failure. Set injection/guardrails `enabled`+`enforcement` from the resolved tier at `__init__`, and ensure the span attributes the SDD already emits (`arcllm.injection.hits`, `arcllm.injection.enforcement`, `arcllm.guardrails.violations`) plus the Audit record reflect the mode actually applied. Add a regression test mirroring `test_registry_propagates_tier_to_policy_context`: a federal-tier `load_model` shows injection block-mode + guardrails enabled in the audit record, not the config default. (a) Security: audit truth-by-construction. (b) Scalability: tier change requires agent restart — already operational reality. [tier-must-flow-through-construction](.claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md)

**Order note is correct and worth a comment in code.** ADR-430's placement (Injection above Security to see pre-redaction text; Guardrails just inside Audit to validate the final post-Retry/Fallback response) is right, and the SDD already flags that the live `registry.py` build differs from the stale `load_model` docstring (CircuitBreaker sits between Security and Retry). Lock the real order with the stack-ordering assertion (T15.12) so the docstring drift cannot silently reverse a guard's position.

## ADRs

### ADR-419: InjectionModule OFF by default, opt-in

**Context**: Injection detection is heuristic; false positives block legitimate prompts. arcllm is transport-only.

**Decision**: Ship InjectionModule disabled (`enabled = false`), enabled per call via `injection=True|{...}`. Default enforcement `warn`.

**Rationale**: Zero-cost, zero-surprise default. Deciding whether a flagged prompt is truly hostile is agent-level policy; arcllm only surfaces the signal. Opt-in keeps the common path clean.

**Alternatives rejected**: On-by-default (breaks existing callers, false positives); block-by-default (too aggressive for a heuristic).

### ADR-420: Pattern corpus default, semantic tier optional

**Context**: Embedding-based detection is more robust but adds a model + numpy dependency.

**Decision**: Curated zero-dep pattern corpus is the default tier. Semantic tier lives behind `arcllm[injection-semantic]`, imported lazily.

**Rationale**: Mirrors the `otel`/`signing` extras pattern (D-090). Zero-dep-when-disabled is a hard rule; the pattern tier catches the common attacks with no install cost.

**Alternatives rejected**: Semantic-only (heavy default); no semantic path (ceiling too low for federal).

### ADR-421: Flag/block only — never interpret or execute

**Context**: The lethal-trifecta and transport boundary demand arcllm not act on content meaning.

**Decision**: InjectionModule returns the scanned messages byte-identical; it only flags (warn) or raises (block). No rewriting, no sanitizing, no execution.

**Rationale**: Interpreting intent is semantic judgement owned by arcagent/arcrun. This is precisely why the module is opt-in (ADR-419).

**Alternatives rejected**: Auto-sanitize injected spans (silent mutation hides attacks and crosses the boundary).

### ADR-422: Scan inbound user + tool-result content

**Context**: LLM01 enters via user turns; ASI06 enters via tool results.

**Decision**: Scan both `TextBlock`/str user content and `ToolResultBlock` content before the provider call, while text is original (pre-Security redaction).

**Rationale**: Tool results are the poisoning vector for agentic loops. Scanning pre-redaction preserves encoded-attack signal that redaction would obscure.

**Alternatives rejected**: User-only (misses ASI06); post-redaction (redaction destroys signal).

### ADR-423: Secret scanner as a togglable detector category

**Context**: Secrets are a leak class alongside PII.

**Decision**: Fold secret patterns into the detector as a `SECRETS` category redacting to `[SECRET:TYPE]`, toggleable like any entity.

**Rationale**: One detect→redact path (reuses D-094 tag format). No parallel subsystem to maintain.

**Alternatives rejected**: Separate SecretModule (duplicate scan pass, second code path).

### ADR-424: Checksum validators gate entity matches

**Context**: Raw 16-digit/IBAN/routing regexes false-positive on IDs and order numbers.

**Decision**: Attach a stdlib arithmetic validator (Luhn / mod-97 / ABA) to the relevant entities; a match only counts if the validator passes.

**Rationale**: Cheap, deterministic, zero-dep precision boost. Cuts noise without heavier NLP.

**Alternatives rejected**: No validation (noisy); ML classifier (heavy for a checksum problem).

### ADR-425 / ADR-426: Gov/CUI entities + per-category toggles

**Context**: Federal CUI extends well past SSN/CC/email; different labs need different categories.

**Decision**: Add US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN, IPV6. Make every category individually toggleable via `pii_entities` allow/deny.

**Rationale**: Meets NIST SI-10/SC-28 output filtering for DOE machines. Config toggles, not code branches (CLAUDE.md).

**Alternatives rejected**: Fixed entity set (can't tune per deployment); code flags per entity (branch sprawl).

### ADR-427: Allowlisted `pii_detector_class` loader (completes D-093/FR-13)

**Context**: `security.py` hard-rejects any detector != "regex" (`_VALID_DETECTORS`, line 58). Spec 012 promised pluggable detectors (D-093/FR-13) but only `vault.py` shipped a loader.

**Decision**: Remove the hard-reject; add `pii_detector_class="module:Class"` loaded via allowlisted-prefix importlib, validated against the `PiiDetector` runtime-checkable protocol — mirroring `VaultResolver.from_config` line-for-line.

**Rationale**: Makes bring-your-own spaCy/Presidio real with zero new core deps and the same ASI04 supply-chain guard already trusted for vault backends. Reuse over reinvention.

**Alternatives rejected**: Unrestricted importlib (arbitrary code execution via TOML — ASI04/ASI05); entry-point plugins (heavier, and the vault pattern already exists and is proven).

### ADR-428 / ADR-434: Per-call GuardrailsModule with block/warn enforcement

**Context**: Downstream systems trust raw model output (LLM05). Enforcement strictness varies by deployment.

**Decision**: GuardrailsModule validates the response per call via `guardrails={...}`, with `enforcement="block"|"warn"` (warn flags in trace + metadata; block raises `ArcLLMGuardrailError`).

**Rationale**: Per-call config matches routing's `classification` mechanism. Shared block/warn vocabulary lets ops observe before enforcing.

**Alternatives rejected**: Global-only guardrails (schemas are request-specific); always-block (no observe phase).

### ADR-429: Semantic guardrails out of scope

**Context**: Grounding/correctness/toxicity require model reasoning and agent context.

**Decision**: arcllm guardrails validate **structure** only. Semantic guardrails live in arcagent/arcrun.

**Rationale**: Keeps the nucleus a transport layer. Structural checks are deterministic and testable; semantic checks need the agent's world.

**Alternatives rejected**: LLM-judge inside arcllm (crosses the concern boundary; adds an LLM call inside the transport layer).

### ADR-430: Stack placement

**Context**: Each guard needs specific bytes — Injection the original inbound text, Guardrails the final resolved response.

**Decision**: Injection above Security (sees pre-redaction text). Guardrails just inside Audit (validates post-Retry/Fallback response; audit records the verdict). Final order (matching the actual inside-out build in `registry.py`, where CircuitBreaker sits between Security and Retry — not between Telemetry and Audit as the stale `load_model` docstring claims): Otel → Queue → Telemetry → Audit → Guardrails → Injection → Security → CircuitBreaker → Retry → Fallback → RateLimit → [Routing|Adapter].

**Rationale**: Injection below Security would scan already-redacted text and miss encoded attacks. Guardrails below Retry/Fallback would validate an intermediate attempt, not what the caller receives.

**Alternatives rejected**: Injection below Security (loses signal); Guardrails at the adapter (validates the wrong, non-final response).

### ADR-431 / ADR-432 / ADR-433: Exceptions, config layout, extra

**Decision**: Add `ArcLLMInjectionError` + `ArcLLMGuardrailError` (subclass `ArcLLMError`); give each module its own `[modules.*]` section and extend `[modules.security]` for PII enrichment; add `arcllm[injection-semantic]` extra.

**Rationale**: Structured, catchable errors; one TOML section per module (existing convention); optional deps gated behind an extra (zero-dep-when-disabled).

## Edge Cases

| Case | Handling |
|------|----------|
| Injection tier="semantic" but extra not installed | `ArcLLMConfigError`: "tier='semantic' requires arcllm[injection-semantic]" (mirrors `create_signer` ECDSA branch) |
| Injection finding in warn mode | Log `arcllm.injection.flagged`, set span attrs, attach to trace; continue with original messages |
| Injection on empty / non-text content (ImageBlock) | Skipped — only str / TextBlock / ToolResultBlock text scanned; content passed through untouched |
| Encoded-instruction pattern in legitimate base64 payload | Pattern requires an adjacent decode/execute verb to fire — reduces false positives; still flaggable in warn |
| CREDIT_CARD regex hits but Luhn fails | Not a match — no redaction (order-number false positive suppressed) |
| IBAN mod-97 fails | Not a match |
| `pii_entities` has both allow and deny | allow wins (allow-list is the stricter, explicit intent); documented |
| `pii_entities` allow-list references unknown category | `ArcLLMConfigError` at construction (typo fails loud) |
| `pii_detector_class` not "module:Class" format | `ArcLLMConfigError` (same message shape as vault) |
| `pii_detector_class` module outside allowlist | `ArcLLMConfigError` (ASI04) |
| `pii_detector_class` loads but lacks `.detect()` | `ArcLLMConfigError`: "does not implement PiiDetector.detect()" |
| Both `pii_detector_class` and `pii_detector="regex"` set | `pii_detector_class` wins (explicit override); regex ignored |
| SECRETS category disabled but secret present | Not redacted — operator opted out; documented as their choice |
| Guardrail json_schema set but response is not JSON | Violation "json_parse" (block raises / warn flags) |
| Guardrail expects schema but no response_format kwarg | Schema check still runs on content if json_schema configured; else skipped |
| Guardrail on None / empty response content | No content-based checks fire; length/schema treat empty per rule (empty may violate min-length if configured) |
| Response content is list[ContentBlock] | Guardrail concatenates TextBlock text for regex/length/stop-list; schema check targets the JSON text block |
| Injection + Guardrails + Security all enabled | Ordered per ADR-430; each span nested; audit sees all verdicts |
| Both new modules disabled | Zero imports, zero latency, zero deps (verified) |

### Research Insights (additional edge cases surfaced by research)

Add these rows/tests — each is a concrete evasion or divergence the current table misses:

| Case | Handling |
|------|----------|
| Injection via zero-width / homoglyph split (`ignore​previous`) | Scan span is NFKC-normalized + zero-width-stripped before matching; passthrough stays byte-identical. Test both a zero-width and a homoglyph variant of an INSTRUCTION_OVERRIDE trigger. |
| Injection via ROT13/base32 (not just base64/hex) | ENCODED_INSTRUCTION adjacency-verb rule broadened; at minimum flag in warn. Test one ROT13 "decode and run" payload. |
| `pii_detector_class` ref is a non-allowlisted module | `ArcLLMConfigError` raised **before** `import_module` runs (assert the module is never imported). |
| `pii_detector_class` given as a file path or relative dotted name | Rejected — only `module:Class` absolute refs accepted (no arbitrary-file import). |
| Bare 10-digit number with no EDIPI/DoD-ID anchor | Not matched as DOD_ID (context-anchor / default-off), suppressing phone/order-ID false positives. |
| Guardrail `json_schema` set but `jsonschema` lib absent | Deterministic: either clear `ArcLLMConfigError` (extra not installed) or identical verdict via vendored validator — never a silent weaker check. Test with/without the lib. |
| Guardrail `deny_pattern` with catastrophic backtracking + crafted response | Content length-capped before regex; bounded so one worker cannot stall (ReDoS/LLM10). |
| Semantic injection tier corpus embeddings | Pre-computed at construction, cached; never embedded per-call. Assert corpus is not re-embedded on the hot path. |

## Boundaries — What Stays in arcagent / arcrun

arcllm is the transport layer. These are **explicitly not** in scope for Spec 015 and must not leak into arcllm:

- **Semantic guardrails** — grounding against sources, factual correctness, toxicity/harm/bias judgement. These require model reasoning and the agent's task context. They belong to arcagent (policy) / arcrun (loop). (D-429)
- **Injection *response* strategy** — deciding what to do with a flagged injection (abort the turn, escalate to a human, quarantine the tool result, re-plan). arcllm raises/flags; arcrun's loop and arcagent's policy engine decide.
- **Lineage / provenance construction** — tracking *which* upstream tool or document introduced poisoned content across turns. arcllm scans a single invoke's messages; cross-turn lineage is arcagent memory-integrity (ASI06 mitigation there, not here).
- **Human-in-the-loop gates** — approval workflows on block-mode failures live in arcagent's policy pipeline, triggered by catching `ArcLLMInjectionError` / `ArcLLMGuardrailError`.
- **Corpus curation & model training** — building/refreshing the semantic attack corpus and its embeddings is an offline pipeline, not runtime arcllm code.

## Test Strategy

### Unit Tests (`test_injection.py`) — ~18 tests
- One positive per attack family (override, exfil, role, delimiter, encoded) + negatives
- Tool-result (ToolResultBlock) scanning; user-content scanning
- block raises `ArcLLMInjectionError`; warn flags + continues
- Content passed through byte-identical (no mutation)
- Zero-dep pattern path; semantic tier errors clearly without the extra
- OFF by default (no scan when disabled)
- Span attributes present

### Unit Tests (`test_pii_enrichment.py`) — ~22 tests
- Luhn valid/invalid CC; mod-97 valid/invalid IBAN; ABA valid routing
- IPV6, US_PASSPORT, US_DRIVERS_LICENSE, DOD_ID/EDIPI, CAC, BANK_ACCOUNT, DOB, MRN positives + negatives
- SECRETS: AWS/GitHub/JWT/PEM/DB URL positives; `[SECRET:TYPE]` redaction
- `pii_entities` allow-list limits scan; deny-list excludes; allow-wins-over-deny
- Unknown category in toggle → error

### Unit Tests (`test_pii_loader.py`) — ~10 tests
- Valid `pii_detector_class` loads and runs
- Bad format / non-allowlisted / missing module / missing class / missing `.detect()` → `ArcLLMConfigError`
- `pii_detector_class` overrides `pii_detector="regex"`
- Regression: former `_VALID_DETECTORS` reject path is gone (non-"regex" no longer auto-rejected)

### Unit Tests (`test_guardrails.py`) — ~16 tests
- json_schema conformance pass/fail; non-JSON content
- deny-list hit; allow-list miss; max-length over/under; banned stop-list hit
- block raises `ArcLLMGuardrailError`; warn flags to metadata
- list[ContentBlock] response handling; None content
- Span attributes present

### Integration Tests (extend `test_registry.py` / `test_security.py`) — ~10 tests
- `load_model(..., injection=True)` / `guardrails={...}` wrap correctly
- MODULE_NAMES includes injection + guardrails
- Stack ordering assertion (ADR-430)
- Injection sees original (pre-redaction) text; Guardrails sees final response
- No regressions across existing suite
