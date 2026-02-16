# Product Context

> This document provides stable product context that informs all feature specifications.
> Feature-specific details go in `.claude/specs/{feature}/` documents.

## Validation Checklist

- [x] Vision statement defined
- [x] All personas documented
- [x] User journey framework established
- [x] Success metrics framework defined
- [x] Business constraints listed
- [x] Domain glossary populated
- [x] No [NEEDS CLARIFICATION] markers

---

## Vision & Mission

### Vision

Build the enterprise-grade autonomous agent nucleus that's inspectable, auditable, and deployable in a SCIF.

### Mission

ArcAgent is the core autonomous agent layer built on ArcLLM (unified LLM calls) and ArcRun (runtime agentic loop). It provides a minimal, secure, modular foundation for specialized agents that can scale to 10,000+ concurrent instances while meeting federal security requirements (FedRAMP, NIST, CMMC).

### Value Proposition

- **Federal-first**: Security and compliance in the foundation, not bolted on. Every agent has a cryptographic identity, every action has an audit trail, every credential is vault-managed.
- **Simple enough to inspect**: <3,000 lines of core code. A security auditor can review it in a day.
- **Powerful enough to scale**: 10,000 agents via the orchestrator layer (Apex/arcTeam).
- **Self-scheduling**: Agents create and manage their own schedules (cron, interval, one-shot). The prompt lives with the schedule entry — no separate heartbeat file to maintain.
- **OpenClaw-compatible without OpenClaw-dependent**: Consume the massive community ecosystem through a sandboxed adapter.

### Product Hierarchy

```
Apex (Platform)
├── ArcLLM        — Unified LLM call layer (provider-agnostic)
├── ArcRun        — Runtime agentic loop (think-act-observe)
├── ArcAgent      — Core autonomous agent (THIS PROJECT)
│   └── Specialized agents extend ArcAgent via modules
└── arcTeam       — Fleet coordination layer (orchestration, scheduling, shared context)
```

ArcAgent is the individual agent. arcTeam coordinates fleets of ArcAgents. Both live under the Apex umbrella. ArcLLM and ArcRun are foundations that ArcAgent builds on.

---

## User Personas

### Primary Persona: Federal Systems Integrator

- **Role/Title**: DevSecOps Engineer / Systems Architect at CTG Federal, DOE labs, or similar
- **Demographics**: 30-50, deep security expertise, cleared (Secret/TS-SCI), familiar with NIST/FedRAMP
- **Goals**: Deploy AI agents in air-gapped or SCIF environments that pass security audits. Need agents that automate procurement, analysis, and reporting tasks without violating compliance boundaries.
- **Pain Points**: OpenClaw is a security disaster (plaintext creds, no isolation, exposed gateways). Frontier is hosted-only (can't self-host). Building from scratch is too expensive.
- **Behaviors**: Works in hardened Linux environments. Uses Vault, mTLS, OTel. Needs audit trails for everything. Won't deploy anything that can't be inspected.

### Secondary Persona: Enterprise AI Platform Builder

- **Role/Title**: AI/ML Engineer or Platform Engineer at mid-to-large enterprise
- **Demographics**: 28-45, building internal AI platforms, evaluating agent frameworks
- **Goals**: Build scalable agent infrastructure that integrates with existing IAM, observability, and data platforms. Needs multi-model support, module marketplace, and fleet management.
- **Pain Points**: Current frameworks are either too simple (toy demos) or too complex (OpenClaw's 52+ modules). Need something in between: real security, real scale, but understandable codebase.
- **Behaviors**: Evaluates tools on GitHub stars, documentation quality, security posture, and extensibility. Wants to fork and customize, not fight the framework.

### Tertiary Persona: BlackArc Internal Developer

- **Role/Title**: Software Engineer at BlackArc Systems
- **Demographics**: Building agents for BlackArc's government contracts and products
- **Goals**: Ship specialized agents (procurement, coding, design) quickly using ArcAgent as a base. Need the module system to work, identity to be real, and evaluation to help agents improve.
- **Pain Points**: Building everything from scratch for each contract is unsustainable. Need a reusable foundation.
- **Behaviors**: Works across multiple projects. Needs to understand the core in 30 minutes. Extends via modules and config, not core modifications.

---

## User Journey Framework

> This framework applies to all features. Individual features customize stages.

### Standard Journey Stages

1. **Discovery**: Engineer evaluates agent frameworks for enterprise/federal use. Finds ArcAgent via GitHub, referral, or BlackArc engagement.
2. **Evaluation**: Reviews codebase (<3K lines core), runs `arcagent init`, deploys a test agent. Verifies security posture, audit trail, identity system.
3. **Adoption**: Deploys first production agent with real modules (memory, skills, tools). Connects to existing infrastructure (Vault, OTel, NATS).
4. **Extension**: Builds specialized agents via module composition. Creates custom skills, tools, policies. Publishes to internal module marketplace.
5. **Scale**: Deploys arcTeam for fleet management. Runs 100+ agents with coordinated scheduling, shared context, and centralized evaluation.
6. **Advocacy**: Recommends ArcAgent for other contracts/teams. Contributes modules back to marketplace.

### Key Touchpoints

| Stage | Touchpoint | Success Metric |
|-------|------------|----------------|
| Discovery | GitHub README + arcagent-plan.md | Clone within 5 min of reading |
| Evaluation | `arcagent init` + `arcagent run` | First agent running in < 10 min |
| Adoption | arcagent.yaml config | Production agent deployed in < 1 day |
| Extension | MODULE.yaml + Module Bus | Custom module working in < 2 hours |
| Scale | arcTeam orchestrator | 100 agents coordinated successfully |

---

## Success Metrics Framework

> Standard metrics categories. Features reference these with specific targets.

### Adoption Metrics

| Metric | Description | Tracking Method |
|--------|-------------|-----------------|
| Time to First Agent | Time from `arcagent init` to running agent | CLI telemetry |
| Module Adoption | Modules installed per agent | Config analysis |
| Extension Rate | Custom modules created per deployment | Registry data |

### Agent Performance Metrics

| Metric | Description | Tracking Method |
|--------|-------------|-----------------|
| Task Completion Rate | % of tasks successfully completed | OTel metrics (consumed externally) |
| Task Duration | Average time per task type | OTel metrics |
| Cost per Task | LLM + compute cost per completed task | OTel metrics |
| Policy Score | Average score of policy.md bullets | Agent self-evaluation (internal) |

### Security Metrics

| Metric | Description | Tracking Method |
|--------|-------------|-----------------|
| Audit Coverage | % of actions with audit events | Telemetry analysis |
| Auth Success Rate | Challenge-response auth pass rate | Identity service |
| Policy Violations | Attempted actions blocked by policy | Policy module |
| Vulnerability Count | Known vulns in modules/dependencies | SBOM + scanning |

### Scale Metrics

| Metric | Description | Tracking Method |
|--------|-------------|-----------------|
| Concurrent Agents | Number of agents running simultaneously | Orchestrator |
| Message Throughput | NATS messages per second | NATS monitoring |
| Cold Start Time | Time to spin up new agent | Firecracker metrics |
| Fleet Health | % of agents in healthy state | Health monitor |

---

## Business Constraints

### Compliance & Legal

| Requirement | Description | Impact |
|-------------|-------------|--------|
| FedRAMP | Federal Risk and Authorization Management Program | Must support authorization package. Vault-backed secrets, audit trails, encryption at rest. |
| NIST 800-53 | Security and Privacy Controls for Federal Systems | Identity (IA controls), audit (AU controls), access control (AC controls) built into core. |
| CMMC | Cybersecurity Maturity Model Certification | CUI handling, classification-aware data flow, tamper-evident logging. |
| ITAR/EAR | Export control regulations | Classification gates on data and tool outputs. Modules must declare classification level. |

### Business Rules

| Rule | Description | Enforced By |
|------|-------------|-------------|
| Lethal Trifecta Prevention | Agent must NEVER simultaneously have: private data access + external comms + untrusted input without human-in-the-loop | Policy module + approval gates |
| Module Provenance | All modules must be signed and verified before loading | Module Bus + code signing |
| Credential Isolation | Credentials never in agent filesystem, always vault-backed | Identity component + Vault |
| Audit Immutability | Audit logs must be tamper-evident with 7-year retention for federal | Telemetry component |

### Technical Constraints

| Constraint | Description | Rationale |
|------------|-------------|-----------|
| Core < 3,000 LOC | Core components must stay under 3K lines | Inspectability. Security auditor reviews in 1 day. |
| Python core | Primary implementation language | ML/AI ecosystem, team expertise |
| Self-hostable | Must run air-gapped, on-prem, in DOE labs | Federal deployment requirements |
| No public endpoints | Agent never listens on public internet | Mesh VPN only (Tailscale/WireGuard) |

---

## Competitive Landscape

### Primary Competitors

| Competitor | Strengths | Weaknesses | Our Differentiation |
|------------|-----------|------------|---------------------|
| OpenClaw | 175K+ stars, massive community, ClawHub marketplace | Plaintext creds, no isolation, 40K exposed instances, 52+ modules complexity | Federal-first security, <3K LOC core, real isolation |
| NanoClaw | 500 lines, container isolation, simple | Single machine, no scale, no IAM, no fleet mgmt | Scale to 10K, identity system, module marketplace |
| OpenAI Frontier | Enterprise IAM, multi-vendor, evaluation loops | Hosted-only, can't self-host, weak security story, no air-gap | Self-hosted, air-gapped, Firecracker isolation, open source |
| Nanobot (HKU) | 4K lines, clean bridge pattern, multi-channel | No security, no enterprise features, no scale | Enterprise-grade with same simplicity philosophy |

### Market Position

Enterprise/federal self-hosted agent infrastructure. Premium segment (government contracts, DOE, DOD, intelligence community). Not competing with consumer/developer agent tools. Positioned as "the only agent framework you can deploy in a SCIF."

---

## Risk Framework

### Risk Categories

| Category | Description | Mitigation Approach |
|----------|-------------|---------------------|
| Technical | Core complexity exceeds 3K LOC target | Strict component boundaries, module bus for extensions |
| Security | Vulnerability in core identity/auth | Code review, pen testing, formal verification of crypto |
| Adoption | Too complex for non-federal users | Dev mode (file-based keys, no Vault required) |
| Ecosystem | ClawHub skills contain malware | Sandboxed adapter, pre-load scanning, trust levels |
| Scale | NATS/Firecracker perf at 10K agents | Load testing phases (100 -> 1K -> 10K) |
| Compliance | FedRAMP authorization timeline | Start authorization package in Phase 2 |

---

## Domain Glossary

| Term | Definition | Context |
|------|------------|---------|
| **ArcAgent** | Core autonomous agent layer. The individual agent. | This project |
| **arcTeam** | Fleet coordination layer within Apex. Manages groups of ArcAgents. | Orchestration, scheduling, shared context |
| **Apex** | The entire platform ecosystem (ArcLLM + ArcRun + ArcAgent + arcTeam) | Product family name |
| **ArcLLM** | Unified LLM call layer. Provider-agnostic model calls with failover. | Foundation layer |
| **ArcRun** | Runtime agentic loop. Think-act-observe cycle. | Foundation layer |
| **DID** | Decentralized Identifier (W3C spec). Format: `did:arc:{org}:{type}/{id}` | Agent identity |
| **Module Bus** | Event-driven extension point. Modules subscribe to lifecycle events. | Core architecture |
| **Module** | Pluggable extension: memory, skill, tool, channel, hook, policy | Extension system |
| **Workspace** | Agent's filesystem: identity.md, context.md, policy.md, notes/, entities/, etc. | Per-agent storage |
| **policy.md** | Agent-maintained self-learning file. Ranked behavioral notes. | Self-improvement |
| **context.md** | Agent-maintained working memory. Token-budgeted. Replaces OpenClaw's MEMORY.md. | Session continuity |
| **identity.md** | Admin-controlled agent identity. Read-only to agent. | Agent configuration |
| **Lethal Trifecta** | Combination of private data access + external comms + untrusted input. Must be broken with approval gates. | Security model |
| **Evaluator** | External service that scores agent performance by consuming OTel telemetry. Not an agent-internal module. | Quality assurance (arcTeam/external) |
| **ScheduleEntry** | Self-created schedule (cron/interval/once) carrying the prompt with it. Agent manages via tools. | Self-scheduling |
| **Compaction** | Semantic summarization of conversation history to free context window. | Context management |
| **MicroVM** | Firecracker-based hardware-level isolation per agent. | Security isolation |

---

## References

- Architecture plan: `arcagent-plan.md`
- Design document: `arcagent-design-v3.md`
- Enterprise mesh (Apex): `../enterprise-mesh/`
