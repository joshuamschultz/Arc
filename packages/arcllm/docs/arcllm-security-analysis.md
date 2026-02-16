# ArcLLM Security Analysis: OWASP + NIST Mapping

**Date**: 2026-02-09
**Version**: 1.0
**Scope**: Full OWASP Agentic AI (T1-T15), OWASP LLM Top 10 (2025), NIST SP 800-53, NIST AI RMF mapping against ArcLLM's architecture and surface area.

---

## The Layer Boundary (Critical Context)

ArcLLM sits between **agents** and **LLM providers**. This determines what it can and cannot mitigate:

```
Agent Code  ←── agent's responsibility
    |
  ArcLLM    ←── OUR surface area (this analysis)
    |
Provider API ←── provider's responsibility
```

**ArcLLM controls:** Request/response normalization, provider HTTP communication, module middleware (retry, fallback, rate limit, telemetry, audit), config loading, error handling.

**ArcLLM does NOT control:** Agent decision logic, tool execution, memory systems, multi-agent communication, prompt construction, what agents do with responses.

Many OWASP threats are **agent-layer concerns** where ArcLLM can provide **hooks and guardrails** but cannot enforce alone.

---

## Part 1: OWASP Agentic AI Threats (T1-T15)

### Applicable — ArcLLM Can Directly Mitigate

| Threat | What It Is | ArcLLM Relevance | Already Built | Needs Building |
|--------|-----------|-------------------|---------------|----------------|
| **T4: Resource Overload** | Agent causes excessive API calls/token consumption | Direct — ArcLLM controls all LLM calls | Rate limiting (token bucket), telemetry (token/cost tracking) | **Budget module** (Step 12) — hard spend caps. **Token limit per invoke** — reject messages exceeding configured max |
| **T8: Repudiation & Untraceability** | Cannot prove what LLM was asked/answered | Direct — ArcLLM wraps every invoke() | Audit module (PII-safe metadata), telemetry (per-call timing/cost) | **Request ID propagation** — trace ID per invoke() (baked into BaseModule). **OpenTelemetry export** (Step 13) — SIEM integration. **Audit storage** — structured output to file/stream (not just logging) |
| **T9: Identity Spoofing** | Attacker impersonates legitimate agent/provider | Partial — ArcLLM resolves API keys and communicates with providers | HTTPS enforcement, API keys from env only | **Request signing** (Step 14) — HMAC on outbound requests. **Provider certificate pinning** — optional TLS cert validation |
| **T11: Unexpected RCE** | Code execution through LLM responses | Partial — ArcLLM parses provider responses | Tool call parsing with `ArcLLMParseError` on failure, no `eval()` or dynamic code execution | **Output content type validation** — verify response structure before returning. Already safe by design (Pydantic parsing, no eval) |

### Applicable — ArcLLM Can Provide Guardrails (Agent Must Cooperate)

| Threat | What It Is | What ArcLLM Can Do | Needs Building |
|--------|-----------|-------------------|----------------|
| **T2: Tool Misuse** | LLM manipulates tool calls to perform unauthorized actions | ArcLLM normalizes tool calls — can validate before passing to agent | **Tool call validator module** (Step 17) — allowlist tool names, validate argument schemas against Tool definitions, reject unexpected tool calls |
| **T5: Cascading Hallucinations** | LLM fabricates data that propagates through system | ArcLLM sees every response — can flag anomalies | **Response metadata hooks** — expose stop_reason, token counts, and model confidence signals to agents for their own validation |
| **T6: Intent Breaking / Goal Manipulation** | Injected instructions override agent's intended behavior | ArcLLM carries messages — can inspect content | **Content scanning module** (Step 18) — detect common injection patterns in responses (e.g., "ignore previous instructions"). Optional, configurable |
| **T10: Overwhelming HITL** | System floods human reviewers | ArcLLM rate limits and logs | Rate limiter, telemetry already built. **Alert thresholds** — configurable alerts when call volume or cost exceeds norms (budget module) |

### Not Applicable at ArcLLM Layer (Agent/Application Responsibility)

| Threat | Why Not ArcLLM's Layer |
|--------|----------------------|
| **T1: Memory Poisoning** | ArcLLM is stateless — agents manage their own message history. ArcLLM never stores or retrieves memory. |
| **T3: Privilege Compromise** | ArcLLM has no privilege system — it uses whatever API key the agent provides via env vars. Agent/platform owns access control. |
| **T7: Misaligned & Deceptive Behaviors** | Model alignment is the provider's concern. ArcLLM passes messages transparently. |
| **T12: Agent Communication Poisoning** | ArcLLM is a single-agent-to-single-provider library. No inter-agent communication. |
| **T13: Rogue Agents** | Agent orchestration is outside ArcLLM scope. |
| **T14: Human Attacks on Multi-Agent Systems** | No multi-agent orchestration in ArcLLM. |
| **T15: Human Manipulation** | Social engineering attacks on humans — outside library scope. |

---

## Part 2: OWASP LLM Top 10 (2025)

| ID | Threat | ArcLLM Relevance | Already Mitigated | Proposed |
|----|--------|-------------------|-------------------|----------|
| **LLM01: Prompt Injection** | Malicious input hijacks LLM behavior | ArcLLM transports messages, doesn't construct them. But can offer opt-in scanning. | None — by design (agents own prompts) | **Content scanner module** (Step 18, opt-in) — regex/pattern detection on messages before sending. NOT a silver bullet, but a defense layer. |
| **LLM02: Sensitive Info Disclosure** | LLM leaks PII/secrets in responses | ArcLLM sees all responses — can scan outbound content | PII-safe audit logging (no content by default), error body truncation (500 chars) | **PII redaction module** (Step 14) — scan both outbound messages AND inbound responses for patterns (SSN, CC#, API keys). Configurable regex patterns. Both directions per D-077. |
| **LLM03: Supply Chain** | Compromised dependencies | ArcLLM has minimal deps (pydantic, httpx) | Minimal dependency surface by design | **Dependency pinning** in pyproject.toml, **SBOM generation** for federal compliance (post-Step 16 docs pass) |
| **LLM04: Data/Model Poisoning** | Training data corruption | Provider concern — ArcLLM doesn't train models | N/A | N/A |
| **LLM05: Improper Output Handling** | Trusting LLM output without validation | ArcLLM parses to typed Pydantic models | Pydantic validation on all responses, `ArcLLMParseError` on malformed data | **Strict response validation mode** — reject responses missing expected fields |
| **LLM06: Excessive Agency** | LLM taking actions beyond intended scope | ArcLLM normalizes tool calls — can validate | Tool call parsing with type safety | **Tool allowlist module** (Step 17) — agents declare allowed tools, ArcLLM rejects any tool_use not in the allowlist |
| **LLM07: System Prompt Leakage** | System prompts extracted by adversarial input | Agent constructs prompts — but ArcLLM could log/flag | None — agent responsibility | **System prompt protection** — optional: hash system messages for audit (detect changes) without logging content |
| **LLM08: Vector/Embedding Weaknesses** | RAG/embedding attacks | ArcLLM doesn't do RAG or embeddings | N/A | N/A |
| **LLM09: Misinformation** | Hallucinated facts | Provider/agent concern | None | N/A — not ArcLLM's layer |
| **LLM10: Unbounded Consumption** | Excessive resource usage (tokens, calls, cost) | **Direct hit** — ArcLLM controls all API calls | Rate limiting, telemetry with cost tracking | **Budget module** (Step 12) — hard limits per period. **Max token enforcement** — reject calls exceeding configured max_tokens |

---

## Part 3: NIST Controls Mapping

### NIST AI Risk Management Framework (AI RMF 1.0)

| Function | Category | ArcLLM Relevance | Coverage |
|----------|----------|-------------------|----------|
| **GOVERN** | Risk governance policies | ArcLLM's config-driven module system lets orgs enforce policies | Config.toml as policy. Module enable/disable. |
| **MAP** | Context and risk identification | Telemetry provides data for risk mapping | Telemetry module, audit module |
| **MEASURE** | Risk analysis and tracking | Token/cost/timing metrics per call | Telemetry built. Budget (Step 12). OTel (Step 13). |
| **MANAGE** | Risk mitigation and monitoring | Rate limiting, budget caps, audit trail | Rate limit built. Budget needed. |

### NIST SP 800-53 Controls (Relevant Subset for LLM Library)

| Control | Title | ArcLLM Mapping | Status |
|---------|-------|----------------|--------|
| **AC-3** | Access Enforcement | Path traversal prevention in config loading, API keys from env only | Built |
| **AC-4** | Information Flow Enforcement | Module middleware chain controls request/response flow | Built (module pattern) |
| **AU-2** | Audit Events | LLM invoke() calls logged with metadata | Built (audit module) |
| **AU-3** | Content of Audit Records | Provider, model, message count, stop reason, tool calls, content length | Built (D-072) |
| **AU-6** | Audit Review, Analysis, Reporting | Structured log output for SIEM ingestion | Partial — needs OTel export (Step 13) |
| **AU-8** | Time Stamps | Telemetry logs timing per call | Built |
| **AU-9** | Protection of Audit Information | Log injection prevention via `_sanitize()` | Built |
| **AU-12** | Audit Generation | Configurable audit at module level | Built |
| **IA-5** | Authenticator Management | API keys from environment only, never in config files | Built |
| **SC-8** | Transmission Confidentiality | HTTPS enforcement (only HTTP for localhost) | Built |
| **SC-13** | Cryptographic Protection | TLS via httpx default | Built. Request signing planned (Step 14). |
| **SC-28** | Protection of Info at Rest | No secrets in config files, no PII in logs by default | Built |
| **SI-4** | System Monitoring | Telemetry + audit per invoke() | Built |
| **SI-10** | Information Input Validation | Pydantic validation on all inputs, strict regex on provider names | Built |
| **SI-11** | Error Handling | Structured exception hierarchy, error body truncation (500 chars) | Built |
| **SA-10** | Developer Configuration Management | Convention-based registry, config-driven modules | Built |

### Controls Needing Work

| Control | Title | Gap | Proposed |
|---------|-------|-----|----------|
| **AU-6** | Audit Review/Reporting | Logs go to Python logging only — not SIEM-ready | **OTel export** (Step 13) — structured spans/events for Datadog/Splunk/etc. |
| **SC-12** | Cryptographic Key Establishment | No key rotation, no vault integration | **Vault integration** (Step 14) — HashiCorp Vault / AWS Secrets Manager for API key retrieval |
| **SC-13** | Cryptographic Protection | No request integrity verification | **Request signing** (Step 14) — HMAC-SHA256 on outbound request body for tamper detection |
| **SI-4(4)** | Automated Security Alerts | No automated alerting on anomalies | **Budget alerts** (Step 12) — threshold-based alerts. **Anomaly detection hooks** — configurable callbacks when metrics exceed norms |
| **CM-7** | Least Functionality | All modules exist in codebase even when disabled | Already mitigated by lazy loading and config toggles. Modules only loaded when enabled. |

---

## Part 4: What's Already Built (Security Inventory)

| Feature | File | OWASP/NIST Mapping |
|---------|------|--------------------|
| HTTPS enforcement | `config.py` `_validate_https()` | SC-8, T9 |
| API keys from env only | `adapters/base.py` `os.environ` | IA-5, SC-28 |
| Path traversal prevention | `config.py` `_validate_provider_name()` | AC-3, SI-10 |
| Log injection prevention | `modules/_logging.py` `_sanitize()` | AU-9 |
| PII-safe audit logging | `modules/audit.py` | AU-2, AU-3, T8 |
| Error body truncation | `exceptions.py` `ArcLLMAPIError` | SI-11 |
| Pydantic input validation | `types.py`, `config.py` | SI-10 |
| Rate limiting (token bucket) | `modules/rate_limit.py` | T4, LLM10 |
| Telemetry (per-call metrics) | `modules/telemetry.py` | AU-8, SI-4, MEASURE |
| Retry with backoff+jitter | `modules/retry.py` | T4 (prevents thundering herd) |
| Provider fallback chain | `modules/fallback.py` | Availability |
| Lazy module loading | `__init__.py` `__getattr__` | CM-7 |
| Strict config validation | All modules | SI-10 |

---

## Part 5: Proposed New Features/Modules

### Tier 1: Build Into Existing Planned Steps

These fit naturally into already-planned steps:

#### Step 12 — Budget Module (next up)
- Hard spend caps per period (daily/monthly)
- Per-agent or per-provider budget isolation
- Configurable alert thresholds (warn at 80%, block at 100%)
- **NIST**: SI-4(4), MANAGE
- **OWASP**: T4 (Resource Overload), LLM10 (Unbounded Consumption)

#### Step 13 — OpenTelemetry Export
- Structured spans per invoke() (trace_id, span_id, duration, status)
- Events for audit records
- Attributes for all telemetry fields
- Export to OTLP (Datadog, Splunk, Jaeger, etc.)
- Request ID from BaseModule propagated as trace context
- **NIST**: AU-6, SI-4
- **OWASP**: T8 (Repudiation), GOVERN

#### Step 14 — Security Layer (already planned, now scoped)
- **Vault integration**: HashiCorp Vault, AWS Secrets Manager for API key retrieval (replacing env-only)
- **Request signing**: HMAC-SHA256 on outbound request body for tamper detection
- **PII redaction**: Configurable regex scanner on BOTH outbound messages AND inbound responses (SSN, CC#, email, API keys). Both directions per D-077.
- **NIST**: SC-12, SC-13, LLM02
- **OWASP**: T9 (Identity Spoofing), LLM02 (Sensitive Info Disclosure)

### Tier 2: New Security Steps (Post Step 16)

Per D-075, these are separate steps to keep concerns clean:

#### Step 17 — Tool Call Validator Module (opt-in)
- Agents declare allowed tool names when calling `load_model()`
- Module rejects any `tool_use` in LLM response that isn't in the allowlist
- Validates tool call arguments against the `Tool.parameters` JSON Schema the agent provided
- **Why**: Defense-in-depth. Even if the LLM hallucinates a tool call, ArcLLM catches it before the agent sees it.
- **OWASP**: T2 (Tool Misuse), LLM06 (Excessive Agency)

#### Step 18 — Content Scanner Module (opt-in)
- Configurable regex patterns to scan messages and responses
- Detect common injection patterns ("ignore previous instructions", "system:", etc.)
- Detect PII patterns in outbound messages (before they reach the LLM)
- Log warnings or raise on match (configurable behavior)
- **Why**: Not foolproof against prompt injection, but adds a pattern-based defense layer. Federal compliance often requires *demonstrating* you have input validation.
- **OWASP**: LLM01 (Prompt Injection), LLM02 (Sensitive Info Disclosure)

### Cross-Cutting: Request ID / Trace Context (BaseModule Enhancement)

Per D-076, baked into BaseModule so all modules inherit automatically:
- Generate unique `request_id` per invoke() call
- Propagate W3C Trace Context headers to providers that support it
- Attach `request_id` to audit + telemetry logs
- OTel module (Step 13) uses it as span context
- **When**: Implement during Step 13 (OTel) since that's when trace context becomes actionable
- **Why**: Correlate a single agent request across retry, fallback, rate-limit, and provider response. Essential for incident response.
- **NIST**: AU-3, AU-6
- **OWASP**: T8

### Tier 3: Documentation / Process (Post Step 16)

Per D-078, these come after all code is complete:

#### Dependency SBOM Generation
- Add `pip-audit` or `cyclonedx-bom` to dev dependencies
- CI script to generate SBOM on each release
- **NIST**: SA-10, LLM03 (Supply Chain)

#### Security Incident Response Runbook
- Based on OWASP GenAI IR Guide
- Specific to ArcLLM: "what to do if API key is compromised", "what to do if unexpected tool calls appear in audit logs"
- Detection criteria mapped to ArcLLM's telemetry/audit fields
- **NIST**: IR-1, IR-4, IR-5, IR-6

#### Threat Model Document
- Formal threat model specific to ArcLLM's architecture
- Data flow diagrams showing trust boundaries
- Attack surfaces per layer
- **NIST**: RA-3, RA-5

---

## Part 6: Summary Matrix — What Applies vs. What Doesn't

### Threats We Handle Well (Already Built)
- **T4** (Resource Overload) — rate limiting + telemetry
- **T8** (Repudiation) — audit logging + telemetry
- **T11** (RCE) — Pydantic parsing, no eval, typed responses
- **LLM05** (Improper Output) — Pydantic validation
- **LLM10** (Unbounded Consumption) — rate limiting + telemetry

### Threats We'll Handle (Planned Steps)
- **T4/LLM10** (Resource) — budget module (Step 12)
- **T8** (Repudiation) — OTel export (Step 13) + trace context in BaseModule
- **T9** (Identity) — vault + signing (Step 14)
- **LLM02** (Sensitive Info) — PII redaction both directions (Step 14)
- **LLM03** (Supply Chain) — SBOM generation (post-Step 16)

### Threats We Can Add Guardrails For (New Modules)
- **T2/LLM06** (Tool Misuse/Excessive Agency) — tool call validator (Step 17)
- **LLM01** (Prompt Injection) — content scanner (Step 18)
- **LLM07** (System Prompt Leakage) — system prompt hashing (optional, Step 18)

### Threats Outside Our Layer (Not Our Problem)
- **T1** (Memory Poisoning) — agents manage state
- **T3** (Privilege Compromise) — platform/orchestrator concern
- **T7** (Misaligned Behavior) — provider/model concern
- **T12-T15** (Multi-agent) — no multi-agent in ArcLLM
- **LLM04** (Data Poisoning) — provider concern
- **LLM08** (Vector/Embedding) — no RAG in ArcLLM
- **LLM09** (Misinformation) — provider/agent concern

---

## Part 7: Updated Build Order

### Phase 2: Module System (Steps 7-9)

| Step | What | Status |
|------|------|--------|
| 7 | Fallback + retry | Complete |
| 8 | Rate limiter | Complete |
| 9 | Router | Skipped (deferred) |

### Phase 3: Observability (Steps 10-13)

| Step | What | Status | Security Mapping |
|------|------|--------|-----------------|
| 10 | Telemetry | Complete | AU-8, SI-4, MEASURE |
| 11 | Audit trail | Complete | AU-2, AU-3, AU-9, AU-12, T8 |
| 12 | Budget manager | **Next** | T4, LLM10, SI-4(4) |
| 13 | OpenTelemetry export + trace context | Not started | AU-6, T8, GOVERN |

### Phase 4: Enterprise (Steps 14-16)

| Step | What | Status | Security Mapping |
|------|------|--------|-----------------|
| 14 | Security layer (vault, signing, PII redaction) | Not started | SC-12, SC-13, T9, LLM02 |
| 15 | Local providers (Ollama, vLLM) | Not started | — |
| 16 | Full integration test | Not started | — |

### Phase 5: Security Hardening (Steps 17-18) — NEW

| Step | What | Status | Security Mapping |
|------|------|--------|-----------------|
| 17 | Tool call validator | Not started | T2, LLM06 |
| 18 | Content scanner | Not started | LLM01, LLM02, LLM07 |

### Phase 6: Compliance Documentation — NEW

| Deliverable | Status | NIST Mapping |
|-------------|--------|-------------|
| SBOM generation | Not started | SA-10, LLM03 |
| Threat model document | Not started | RA-3, RA-5 |
| IR runbook | Not started | IR-1, IR-4, IR-5, IR-6 |

---

## Part 8: Decisions Log (This Analysis)

| ID | Decision | Reason |
|----|----------|--------|
| D-075 | Tool validator + content scanner as Steps 17-18 (not folded into Step 14) | Keep Step 14 focused on vault/signing/PII |
| D-076 | Trace context baked into BaseModule | Cross-cutting concern — all modules inherit automatically |
| D-077 | PII redaction scans both directions (outbound + inbound) | Federal compliance: prevent PII leakage to provider AND through agent system |
| D-078 | Compliance docs (SBOM, threat model, IR runbook) after Step 16 | Complete all code first, document with full context |

---

## Reference Documents

| Document | Purpose |
|----------|---------|
| `.claude/security/Agentic-AI-Threats-and-Mitigations_v1.0a.pdf` | OWASP Agentic AI threat model (T1-T15), 6 mitigation playbooks |
| `.claude/security/OWASP-GenAI-IR-Guide-1.0.pdf` | GenAI incident response lifecycle, detection criteria |
| `.claude/security/OWASP-GenAI-Security-Project-Solutions-Reference-Guide-Q2_Q325.pdf` | Solutions landscape, LLMSecOps lifecycle |
| `CLAUDE.md` | Project architecture, locked decisions, build order |
| `.claude/arcllm-state.json` | Current state, all decisions (D-001 through D-078) |
