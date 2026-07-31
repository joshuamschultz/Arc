# Spec 016 — Full Trace Capture & Replay

## Metadata

| Field | Value |
|-------|-------|
| Spec ID | 016 |
| Step | 18 |
| Feature | Full Trace Capture & Replay (raw-by-default, envelope encryption, classification, retention, replay reconstruction, lineage) |
| Status | DRAFT |
| Created | 2026-07-05 |
| Author | Josh + Claude |

## Documents

| Document | Purpose |
|----------|---------|
| [PRD.md](PRD.md) | Problem, goals, requirements, success criteria, compliance mapping |
| [SDD.md](SDD.md) | Design, schema changes, encryption/retention/replay design, ADRs, edge cases, boundaries |
| [PLAN.md](PLAN.md) | Phased tasks with checkboxes and acceptance criteria |

## Decisions Log

| ID | Decision | Rationale |
|----|----------|-----------|
| D-435 | **Flip the default**: `store_raw_bodies` defaults to `True`. Every LLM call captures the full request (messages, model, provider, tools, options) and full response content. | Metadata-only traces cannot answer "what was actually sent/received" during an incident. Full forensic replay is a hard requirement (ASI10 rogue-agent analysis, LLM05/LLM09 post-hoc review). Deliberately overrides the historical metadata-only default in `telemetry.py` (see Cross-References). Compensating controls (encryption, classification, retention, chmod) keep it federal-safe. |
| D-436 | **Raw storage shape: inline, reuse the existing `request_body` / `response_body` fields** on `TraceRecord`. Do NOT add parallel `request_raw` / `response_raw` fields and do NOT split into a linked raw record keyed by `trace_id`. | `TraceRecord` already carries nullable `request_body` / `response_body`. Adding new fields would duplicate them (violates "no legacy/dup, smallest correct change"). Inline keeps one record = one atomically hashed unit — no join on read, no second integrity surface. A linked record would need its own hash chain. Tradeoff (line bloat) is handled by size caps + encryption-at-rest. |
| D-437 | **Hash chain covers the raw bodies and the encryption envelope.** No new integrity machinery. | `TraceRecord.compute_hash()` already excludes only `record_hash`; every other field — including `request_body`, `response_body`, and the new `encryption` envelope — is inside the JCS digest. Raw capture is therefore tamper-evident for free (NIST AU-10 non-repudiation). |
| D-438 | **Federal tier: envelope encryption at rest** — AES-256-GCM content key, data key wrapped by a KMS/vault-resolved key. Plaintext bodies never touch disk when encryption is on. | Raw prompts/responses are a high-value exfiltration target (LLM02) and can leak system prompts (LLM07). Envelope encryption (SC-28 protection at rest, AU-9 protect audit info) is the primary compensating control that makes raw-by-default acceptable in a SCIF. |
| D-439 | **Add a `classification` tag field per record** (default from config, e.g. `unclassified`). | Classification-aware data handling (SI-12). Lets retention and access control act per record, and lets query/export filter by sensitivity. Aligns with the existing `[modules.routing]` classification concept. |
| D-440 | **Retention policy: `max_age_days` + `max_bytes`** drive rotation + purge of aged/oversized trace files. | Raw-by-default grows unbounded; NIST AU-11 (retention) and SI-12 require defined lifecycle. Purge removes whole rotated files past policy; never rewrites live chain lines. |
| D-441 | **Right-to-erasure DROPPED.** No erase-by-`trace_id`/`subject` path, no crypto-shred. | Right-to-erasure removed — it is a GDPR/CCPA consumer-privacy feature that conflicts with federal audit immutability and retention (AU-9/AU-10/AU-11, Federal Records Act), is not an enterprise requirement, and was the sole source of the impossible per-record crypto-shred design. Retention purge (D-440) covers lifecycle; encryption (D-438) covers confidentiality. |
| D-442 | **Replay READ path (`load_for_replay`) lives in arcllm; replay EXECUTION lives in arcrun/tooling.** arcllm only reconstructs and returns a fully-formed request object; it never re-invokes the model or diffs outputs. | Separation of concerns (arcllm = LLM calls + their records; arcrun = loop/execution). Re-invoking a model inside arcllm would put loop/orchestration logic in the wrong package. |
| D-443 | **Lineage token is persisted VERBATIM, never constructed by arcllm.** arcllm adds an optional `lineage` field and stores exactly what is passed via a `load_model`/`invoke` kwarg. | Lineage (template source, RAG doc sources, variable substitution) is built by arcrun/arcagent, which own prompt assembly. arcllm has no visibility into template/RAG provenance and must not fabricate it. Cross-module contract, documented in SDD Boundaries. |
| D-444 | **Turning raw capture off is an audited event.** Setting `store_raw_bodies=false` emits a `config_change` `TraceRecord` recording the downgrade (who/when/from→to). | Secure/observable-by-default: capture is ON; disabling it is a deliberate, logged security-relevant action (AU-2 audit of the audit configuration). Prevents silent blinding of the forensic trail. |
| D-445 | **New optional extra `arcllm[trace-encryption]`** pulling `cryptography`; envelope helper in new `src/arcllm/_trace_crypto.py`. | Zero crypto deps in the core install. Personal/enterprise plaintext tier needs nothing extra; only federal encryption pulls `cryptography`. Mirrors the `arcllm[signing]` extras pattern from Spec 012. |
| D-446 | **Tier behavior**: personal = plaintext chmod-locked JSONL by default (encryption optional); enterprise = same, encryption recommended; federal = encryption + classification default + retention all required. | Stringency is metadata, not a gate (ADR-019). Every tier still captures, hash-chains, chmod-locks, and audits; federal adds the crypto/retention envelope. |
| D-447 | **Encryption-key resolution reuses the `vault.py` `VaultBackend` / `VaultResolver` pattern** (allowlisted `module:Class`, TTL cache, KMS-wrapped key). No new key-loading machinery. | DRY — vault resolution and its ASI-04/ASI-05 allowlist already exist. The wrapping key is fetched exactly like an API key; credentials never touch the filesystem (CLAUDE.md). |
| D-448 | **GCM AAD binds ciphertext to `trace_id` + `timestamp`.** The record's identity is authenticated additional data on every envelope. | Prevents transplanting a valid ciphertext onto a different record (anti-transplant) and strengthens tamper-evidence beyond the SHA-256 chain — decryption fails if the record identity was altered. |

## Cross-References

- **Line being overridden**: `src/arcllm/modules/telemetry.py` (`self._store_raw_bodies = config.get("store_raw_bodies", False)`, ~L207-215) and its comment *"Metadata-only by default (SPEC-026 FR-4 / C2): durable plaintext prompts/responses are an exfiltration target (LLM02) and leak system prompts (LLM07). Raw capture is an explicit, audited opt-in."* — D-435 reverses this default; D-438/D-439/D-444 are the compensating controls that make the reversal federal-safe.
- **Spec 009 (telemetry-module)**: owns `TraceRecord` emission, `store_raw_bodies`, and the `_raw_bodies()` builder that this spec's default-flip and encryption hook extend.
- **Spec 010 (audit-trail-module)**: `AuditModule` PII-safe-by-default posture; this spec keeps audit metadata-only while telemetry goes raw-by-default (the two are independent surfaces).
- **Spec 012 (security-layer)**: `VaultResolver` / `VaultBackend` (reused by D-447) and the `arcllm[signing]` extras pattern (mirrored by D-445).
- Prior art: `trace_store.py` hash chain (D-437), `trace_query.py` read path (extended by D-442), SPEC-026 FR-4 (the stance being reversed).

## Learnings

(To be filled during implementation)
