# Spec 012 — Security Layer (Vault + PII + Signing)

## Metadata

| Field | Value |
|-------|-------|
| Spec ID | 012 |
| Step | 14 |
| Feature | Security Layer (Vault Integration, PII Redaction, Request Signing) |
| Status | DRAFT |
| Created | 2026-02-11 |
| Author | Josh + Claude |

## Documents

| Document | Purpose |
|----------|---------|
| [PRD.md](PRD.md) | Problem, goals, requirements, user stories |
| [SDD.md](SDD.md) | Design, components, ADRs, edge cases |
| [PLAN.md](PLAN.md) | Phased tasks with checkboxes and acceptance criteria |

## Decisions Log

| ID | Decision | Rationale |
|----|----------|-----------|
| D-089 | Two modules: VaultResolver (construction-time) + SecurityModule (per-invoke) | Vault resolves keys before adapter creation; PII/signing wrap each invoke call. Different lifecycles. |
| D-090 | Protocol + extras: arcllm[vault-aws], arcllm[vault-hashi] | Zero vault deps in core. Users install only what they need. Importlib loading via config. |
| D-091 | Vault first, env var fallback | Graceful degradation. Supports gradual migration to vault without breaking env-var-based deployments. |
| D-092 | TTL-based key caching (default 300s) | Balances performance (don't hit vault every call) with rotation support (keys refresh without restart). |
| D-093 | Regex default + pluggable PII detector | Works out of box (SSN, CC, email, phone). Override via config for Presidio/spaCy/custom. Custom patterns addable via config. |
| D-094 | Type-tagged PII redaction: [PII:SSN], [PII:EMAIL] | Standard in federal systems. Preserves type info for debugging while removing actual PII. |
| D-095 | HMAC-SHA256 default + ECDSA P-256 optional | HMAC = stdlib, zero deps. ECDSA = arcllm[signing] extra for federal non-repudiation (NIST AU-10). |
| D-096 | Sign messages + tools + model name | Covers everything affecting LLM behavior. Canonical JSON for deterministic serialization. |
| D-097 | Single SecurityModule with two phases | PII redaction + signing in one module. Clean invoke() flow, single kwarg on load_model(). |
| D-098 | Global [vault] config + provider vault_path | Vault connection is global. Key location is per-provider. Clean separation. |
| D-099 | Stack: Audit → Security → Retry | Audit sees redacted data. Each retry sends redacted+signed request. |

## Cross-References

- Prior decisions: D-076 (trace context in BaseModule), D-077 (PII both directions), D-075 (tool validator as Step 17)
- PRD: Section "Modules (opt-in)" — Security module row
- Related specs: 010-audit-trail (audit sees redacted data), 011-otel-export (traces include security spans)

## Learnings

(To be filled during implementation)
