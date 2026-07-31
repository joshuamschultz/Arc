# Product Context — Arc

> Stable product context that informs all feature specifications.
> Feature-specific details go in `.claude/specs/{feature}/`.

## Validation Checklist

- [x] Vision statement defined
- [x] Personas documented
- [x] User journey framework established
- [x] Success metrics framework defined (security/compliance variant — not SaaS)
- [x] Business constraints listed
- [x] Domain glossary populated
- [x] No `[NEEDS CLARIFICATION]` markers

---

## Vision

A world where every autonomous agent action — every prompt, tool call, byte read or written — is **attributable, authorized, and audit-evident**, so AI can be deployed in environments that today refuse to run it.

## Mission

Arc is a security-first agentic framework that makes the *defensible deployment* of AI agents the default, not the upgrade path. Every LLM call, every tool dispatch, every artifact load passes through Identity → Sign → Authorize → Audit. These four pillars are universal across all tiers; what varies is stringency, not enforcement.

### Value Proposition

Most agent frameworks optimize for "look how fast I can wire up a chatbot." Arc optimizes for **"show me, line by line, what this agent did, who told it to do it, and prove nothing was tampered with."**

That posture matters when the deployment target is a DOE lab, a SCIF, a hospital, a bank, or a regulated research environment — places that today say "no" to LLM agents because no existing framework makes the audit trail provable.

References:
- `README.md` (Vision/architecture pitch, lines 34–115)
- `CLAUDE.md` (Build standards: Simplicity / Security / Scalability)

---

## User Personas

### Primary Persona: Federal/Regulated Security Architect

- **Role**: ISSO, security engineer, compliance lead at a federal agency, national lab, or regulated enterprise (banking, healthcare, defense industrial base).
- **Technical level**: Strong — reads STIGs, runs SCAP scans, writes ATO packages, reviews FedRAMP control implementations.
- **Goals**:
  - Adopt LLM agents inside a boundary that requires NIST 800-53 / FedRAMP / CMMC defensibility.
  - Produce evidence that satisfies an auditor without manual reconstruction.
  - Keep agent capabilities scoped, signed, and revocable.
- **Pain points**:
  - Existing frameworks treat audit, identity, and policy as afterthoughts (or paid add-ons).
  - LLM-vendor SDKs are opaque; can't be defended in an authorization package.
  - "Just use OpenAI" fails the moment data classification or air-gap requirements appear.
- **Behaviors**: Lives in policy docs and control catalogs. Will read source. Will demand reproducible builds, signed artifacts, and tamper-evident logs.

### Secondary Persona: Agent Developer (Internal)

- **Role**: Engineer at the operating org building a domain agent on top of Arc (e.g., SCAP analyst, SOC triage, knowledge-base curator).
- **Technical level**: Comfortable Python developer; familiar with async, type hints, Pydantic.
- **Goals**:
  - Author skills, tools, and extensions without re-implementing identity, audit, or policy.
  - Iterate fast in personal tier; promote to enterprise/federal without rewriting.
  - Get clear errors when policy denies a tool call or signature verification fails.
- **Pain points**:
  - Frameworks that mix LLM, loop, and agent concerns (Arc enforces strict separation).
  - Capability discovery scattered across config files, decorators, and registries.
  - Signing/verification ceremony that blocks daily work.

### Tertiary Persona: Auditor / Reviewer

- **Role**: External auditor, IG, or compliance reviewer evaluating an Arc deployment.
- **Goals**:
  - Trace any agent decision back to: who, what, when, with what authority, against what policy.
  - Verify tamper-evident logs without trusting the deployment.
  - Match observed behavior to stated controls (NIST 800-53 IA / AU / AC families).
- **Touchpoints**: `JsonlSink` audit trails, `SignedChainSink` hash chain, OpenTelemetry spans, evidence-pack PDFs (e.g., SCAP demo output).

---

## User Journey Framework

> Adapted for an open-source security framework — not a SaaS funnel.

### Standard Journey Stages

1. **Awareness** — Discovers Arc through README, NLIT/security-conference demos, peer recommendation, or compliance-driven search ("FedRAMP-ready agent framework").
2. **Evaluation** — Reads README + CLAUDE.md, scans `packages/arctrust/` and `packages/arcagent/`, checks ADRs and threat-surface mapping, runs the local demo.
3. **Adoption** — `arc init` (tier selection) → `arc agent create` → first agent runs in personal tier.
4. **Promotion** — Same agent code re-runs at enterprise or federal tier; stringency dials up (FIPS crypto, signed allowlists, hard turn caps).
5. **Operation** — Agent serves traffic; audit trails accumulate; policy violations surface in `arcui`; skills get signed and versioned via `arcskill`.
6. **Authorization** — Evidence packs feed an ATO / audit; signed chain + OTel spans satisfy controls.

### Key Touchpoints

| Stage | Touchpoint | Success Criterion |
|-------|------------|-------------------|
| Awareness | README + architecture diagram | Reader can explain Four Pillars in one sentence |
| Evaluation | Local demo (`make install` + `arc agent chat`) | First agent reply within 5 minutes of clone |
| Adoption | `arc init` wizard | Tier picked, identity provisioned, first DID minted |
| Operation | `arcui` live dashboard | Every tool call shows up with caller DID + policy verdict |
| Authorization | Evidence pack output (e.g., SCAP PDF) | Auditor accepts without manual reconstruction |

---

## Business Constraints

### Compliance & Regulatory

| Requirement | Description | Impact |
|-------------|-------------|--------|
| **NIST 800-53** | Federal control catalog (esp. IA, AU, AC families) | Identity, audit, access-control are mandatory and must be evidenced |
| **FedRAMP** | Federal Risk and Authorization Management | Drives crypto choices (FIPS-validated), audit retention, change management |
| **CMMC** | Cybersecurity Maturity Model Certification | Drives supply-chain (signed deps), incident logging, least privilege |
| **Classification awareness** | Data may be classified or CUI | No PII/CUI in prompts, logs, error messages without explicit handling |

### Architectural Mandates (Federal-First)

| Mandate | Rule | Enforced By |
|---------|------|-------------|
| Identity required | `ArcAgent.__init__` rejects construction without DID | `arcagent.core.agent` |
| Sign required | Loaded artifacts (skills, extensions) verified before use | `arcskill`, `arctrust.keypair` |
| Authorize required | Every tool dispatch passes through policy pipeline | `arctrust.policy.PolicyPipeline` |
| Audit required | Every operation emits `AuditEvent` via single emission point | `arctrust.audit.emit` |
| No vendor SDKs | All providers reach LLMs via direct HTTPS | `arcllm/providers/` (httpx only) |
| Lethal-trifecta gate | Private data + external comms + untrusted input never coexist without human approval | Policy + audit |

### Code-Level Constraints (from `CLAUDE.md`)

- Local-only repo — no migration helpers, deprecation shims, or "vestigial" stubs.
- One line beats five — smallest correct change wins.
- Leave it correct — fix any pre-existing lint/type/test errors surfaced during your work.
- Concern separation — LLM calls in `arcllm`, loop in `arcrun`, agent in `arcagent`. Never mix.

### Resources

- Single primary maintainer (sole developer environment).
- Open-source release cadence: package-by-package, version-pinned via `uv` workspace.
- Demo-driven milestones (e.g., NLIT 2026 → SPEC-024).

---

## Success Metrics Framework

> Arc is a framework, not a SaaS — metrics are oriented around adoption, security posture, and operational defensibility, not DAU/MAU/MRR.

### Adoption Metrics

| Metric | Description | Tracking |
|--------|-------------|----------|
| Time-to-first-agent | Clone → first agent reply | Demo timing |
| Tier-promotion rate | Agents that run identically at multiple tiers | Telemetry |
| Capability authoring | Skills/tools authored per agent | Capability registry counts |

### Engagement Metrics

| Metric | Description | Tracking |
|--------|-------------|----------|
| Audit events per turn | Audit emission density | `arctrust.audit` sinks |
| Policy denials | Tool calls blocked at policy | `PolicyPipeline` events |
| Sandbox rejections | Agent-authored code blocked at AST/builtins layer | `arcrun` sandbox metrics |

### Quality Metrics

| Metric | Threshold | Measurement |
|--------|-----------|-------------|
| Audit-trail completeness | 100% of tool calls emit `AuditEvent` | Test invariant |
| Policy-pipeline latency | p95 < 1ms | Benchmark (SPEC-017) |
| Cold start | < 500ms per agent | CLAUDE.md target |
| Agent baseline memory | < 50MB per agent | CLAUDE.md target |
| Critical / high CVEs | 0 | `pip-audit` CI gate |

### Compliance Metrics

| Metric | Description | Tracking |
|--------|-------------|----------|
| OWASP LLM 2025 coverage | Mitigation present per item | CLAUDE.md threat-surface table |
| OWASP Agentic 2026 coverage | Mitigation present per item | CLAUDE.md threat-surface table |
| Signed-artifact ratio | Signed skills/extensions vs total loaded | Capability registry |
| Tamper-evident chain integrity | `SignedChainSink` chain verifies | Chain verification job |

---

## Risk Framework

### Risk Categories

| Category | Description | Mitigation Approach |
|----------|-------------|---------------------|
| Threat-surface drift | A new feature opens an OWASP LLM/Agentic attack vector | Map every feature to the threat-surface table in `CLAUDE.md` |
| Concern bleed | LLM/loop/agent boundaries blurring | Architecture regression tests (TX.1) |
| Audit completeness | A code path that mutates state but skips `audit.emit` | Test invariant; single emission point |
| Supply chain | Untrusted skill/extension loaded unsigned | `arcskill` signature + AST scan + sandboxed dry-run |
| Cascading failure | One agent's failure propagates across the fleet | Shared-nothing per-agent; circuit breakers |

### OWASP Coverage

The full OWASP LLM 2025 (LLM01–LLM10) and Agentic Applications 2026 (ASI01–ASI10) tables in `CLAUDE.md` are the canonical threat surface. Every spec should map at least one threat ID to a mitigation in its SDD.

---

## Domain Glossary

| Term | Definition | Context |
|------|------------|---------|
| **Four Pillars** | Identity, Sign, Authorize, Audit. Universal across all tiers (ADR-019 referenced). | Every feature is gated through these |
| **Tier** | Personal / Enterprise / Federal. Stringency metadata, **not** a feature gate. | Same code, dialed-up enforcement |
| **DID** | Decentralized Identifier; format `did:arc:{org}:{type}/{hash}`. Mandatory at agent construction. | `arctrust.identity` |
| **Policy Pipeline** | 5-layer deny-by-default chain: Global → Provider → Agent → Team → Sandbox. p95 < 1ms. | `arctrust.policy.PolicyPipeline` |
| **Module Bus** | In-process priority-ordered event bus with veto. Priority 10/50/100/200. | `arcagent.core.module_bus` |
| **Capability** | Unified abstraction for tools, skills, hooks, background tasks (SPEC-021). | `arcagent` capability loader |
| **Capability Roots** | 4 scan locations with explicit precedence: builtins → global → per-agent → untrusted. | SPEC-021 loader |
| **Audit Event** | Structured record emitted on every operation; fans out to `JsonlSink`, `SignedChainSink`, `UIBridgeSink`. | `arctrust.audit` |
| **Signed Chain** | Hash-chained tamper-evident JSONL audit trail. | `arctrust.audit.SignedChainSink` |
| **Lethal Trifecta** | Private data + external comms + untrusted input. Never combined without human approval. | Policy + audit |
| **Sandbox** | Defense-in-depth runner for agent-authored code: AST validator → restricted builtins → egress proxy. | ADR-017C, `arcrun` |
| **Tool Dispatch** | `ToolRegistry.dispatch()` — the single entry point for tool execution. | `arcagent.core.tool_registry` |
| **Spawn** | Agent-spawning primitive (delegation). | `arcagent.orchestration` post-2026-04-26 split |
| **Capability** vs **Module** | Capability = pluggable unit (tool/skill/hook/task). Module = official optional package under `arcagent/modules/`. | Both load through Module Bus |
| **NLIT** | Conference / demo target (NLIT 2026 Kansas City). Drives current SPEC-024 work. | `.claude/NLIT2026-Demo-PRD.md` |
| **SCAP** | Security Content Automation Protocol. Compliance-scan format used in NLIT demo. | `demo-extensions/scap/` |

---

## Open Questions (Product-Level)

- [ ] Is there a public-facing roadmap doc or is the spec sequence (`SPEC-NNN`) the canonical roadmap? Currently the spec sequence is canonical.
- [ ] Persona research: are there documented federal-customer interviews to cite, or is the primary persona inferred from the operating context?

---

## References

- `README.md` — public product narrative + architecture diagram
- `CLAUDE.md` — build standards, threat-surface mapping, quality gates (canonical)
- `.claude/NLIT2026-Demo-PRD.md` — current demo target driving Phase 2
- `.claude/adrs/ADR-017A..D-*.md` — accepted architecture decisions
- `.claude/decisions-log.md` — timestamped decision journal
- `.claude/steering/tech.md` — technical context
- `.claude/steering/structure.md` — architecture
- `.claude/steering/roadmap.md` — phases + execution framework
