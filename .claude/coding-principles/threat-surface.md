## OWASP Threat Surfaces

These are the attack vectors adversaries use against agents in federal environments — design every component against them.

### OWASP Top 10 for LLM Applications (2025)

| Code | Threat | Our Mitigation |
|------|--------|----------------|
| LLM01 | **Prompt Injection** | Input validation, system-prompt isolation, instruction hierarchy. Never trust user-adjacent content as instructions. |
| LLM02 | **Sensitive Information Disclosure** | Output filtering, classification-aware responses, PII/CUI detection before data leaves the agent. |
| LLM03 | **Supply Chain** | Signed modules, SBOM, `pip-audit`, provenance verification on external components. |
| LLM04 | **Data Poisoning** | Validate training/fine-tuning integrity. Checksums on ingested datasets. Isolate data sources. |
| LLM05 | **Improper Output Handling** | Sanitize/validate all LLM outputs before tools, APIs, DBs, or downstream systems. Never execute raw LLM output. |
| LLM06 | **Excessive Agency** | Least-privilege tools. Explicit allowlists. Human-in-the-loop for destructive/irreversible actions. |
| LLM07 | **System Prompt Leakage** | No secrets in system prompts. Treat prompts as exfiltrable. Separate config from instructions. |
| LLM08 | **Vector / Embedding Weaknesses** | Validate embedding sources; access-control vector stores; prevent cross-tenant leakage in shared indices. |
| LLM09 | **Misinformation** | Ground in verified data. Flag confidence. Never present LLM output as authoritative without verification. |
| LLM10 | **Unbounded Consumption** | Token budgets, rate limits, cost ceilings, timeouts, circuit breakers on runaway loops. |

### OWASP Top 10 for Agentic Applications (2026)

| Code | Threat | Our Mitigation |
|------|--------|----------------|
| ASI01 | **Agent Goal Hijack** | Immutable goals in `identity.md` (read-only to agent). Policy boundaries. Kill switches. |
| ASI02 | **Tool Misuse & Exploitation** | Tool allow/deny lists. Parameter validation on every call. Audit all invocations. |
| ASI03 | **Identity & Privilege Abuse** | Per-agent DID. Scoped namespace permissions (`domain:path:permission`). No shared credentials. No privilege inheritance without explicit grant. |
| ASI04 | **Agentic Supply Chain** | Signed runtime-loaded tools/modules. Pre-load vuln scanning. Sandboxed third-party extensions. |
| ASI05 | **Unexpected Code Execution (RCE)** | Never execute agent-generated code without sandboxing. Firecracker microVM. No `eval()`; no dynamic imports from untrusted sources. |
| ASI06 | **Memory & Context Poisoning** | Validate memory writes. Integrity checks on `context.md` and workspace files. Detect anomalous mutations. |
| ASI07 | **Insecure Inter-Agent Communication** | mTLS on NATS. Ed25519 message signing. Replay protection (nonce + timestamp). No plaintext inter-agent traffic. |
| ASI08 | **Cascading Failures** | Circuit breakers. Blast-radius containment. Shared-nothing prevents cascade propagation. |
| ASI09 | **Human-Agent Trust Exploitation** | Agents never impersonate humans. Label AI-generated content. Approval gates on consequential actions. |
| ASI10 | **Rogue Agents** | Telemetry monitoring. Policy-violation alerts. Identity-service revocation. Anomaly detection. |

### Checklist when writing code

1. **Can this be injected?** — Validate inputs. Sanitize outputs.
2. **Can this be abused?** — Least privilege. Explicit allowlists. No implicit trust.
3. **Can this leak?** — No secrets in prompts, logs, or errors. Classification-aware data flow.
4. **Can this cascade?** — Isolate failure domains. Circuit breakers. Timeouts.
5. **Can this be audited?** — Every action is an event. Every event is logged. Every log is searchable.

---

### Abuse Cases for Security Tests

For each feature, write abuse cases alongside success cases: direct artifact tampering, prompt replacement, log scraping, forged/replayed triggers, stolen handles, confused-deputy calls, stale-process behavior and partial-commit/crash windows. Include malicious or compromised operators, agents, plugins, providers, models, databases, message buses, caches, backups, observability exporters, install/update channels and recovery tooling. Cover rollback/downgrade attacks, TOCTOU swaps after verification, symlink/path substitution, dependency confusion, forged DIDs, cross-agent/tenant reads, classification laundering, nonce reuse, clock skew, queue redelivery, stale leases, poisoned indexes/caches, resource exhaustion, audit truncation, backup/export leakage, and attempts to turn health, debug, migration or break-glass paths into authorization bypasses.

Run the dedicated cross-package battery with `uv run python scripts/run_adversarial_tests.py`. A security-sensitive change is not complete until its abuse case is added to that battery.