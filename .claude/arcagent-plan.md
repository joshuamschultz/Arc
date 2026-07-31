# ArcAgent: Core Agent Architecture Plan
## Built on ArcLLM + ArcRun | BlackArc Systems

---

## 1. Executive Summary

ArcAgent is the core autonomous agent layer built on top of ArcLLM (unified LLM call layer) and ArcRun (runtime agentic loop). It serves as the foundational agent that can be extended into specialized agents (coding-agent, design-agent, etc.) while meeting federal security requirements (FedRAMP, NIST, CMMC) and scaling to 10,000+ concurrent agents.

**Design Philosophy:** Simple core, modular everything, secure by default, scale to swarm.

**What we're NOT building:** Another OpenClaw. OpenClaw is a personal assistant with 52+ modules, 45+ dependencies, and a monolithic gateway. We're building an enterprise-grade agent nucleus that's inspectable, auditable, and deployable in a SCIF if needed.

---

## 2. Landscape Analysis

### 2.1 OpenAI Frontier (Concept Inspiration)

Frontier is OpenAI's enterprise agent platform, launched Feb 2026. Key architectural concepts worth borrowing:

- **Shared Business Context**: Agents connect to enterprise data warehouses, CRMs, and internal apps through a semantic layer. Every agent references the same "institutional knowledge." **→ arcTeam territory, not ArcAgent.** This is infrastructure that sits ABOVE the agent layer — shared context across a fleet. ArcAgent needs to be able to CONSUME shared context, but the semantic layer, data connectors, and institutional knowledge graph belong to arcTeam (the orchestration/fleet management layer). Note for when we get there: Frontier uses a "semantic layer" pattern that normalizes access to CRM, ERP, data warehouses, and internal apps so agents don't need per-tool integrations.
- **Agent Identity & IAM**: Each agent gets its own identity with scoped permissions — like an employee badge. Enterprise IAM applies across humans and AI coworkers. Structured onboarding similar to hiring an employee: assign role, grant access to systems, define guardrails. **→ ArcAgent core.** See Section 3.6.
- **Evaluation & Optimization Loops**: Three-tier system: (1) built-in feedback capturing task performance, (2) memory-based learning from past interactions, (3) FDE feedback loop from deployments back to model improvements. Agents build memories, track metrics (completion rates, accuracy, response time, business impact), and continuously improve. Not just "run and forget." **→ ArcAgent core.** Combines with our policy.md self-learning system. See Section 3.7.
- **Multi-vendor agent support**: Frontier manages agents from OpenAI, Google, Anthropic, Microsoft, and custom-built. Creates vendor lock-in at the ORCHESTRATION layer, not the model layer. Unified execution environment provides consistent runtime across vendors. **→ Apex territory.** This is the core value prop of Apex — model-agnostic orchestration. ArcAgent gets this for free via ArcLLM (provider-agnostic calls). The fleet-level multi-vendor management (routing, cost optimization, capability matching) belongs to Apex.
- **Open standards**: No proprietary formats. Existing data stays where it lives. W3C DIDs, OpenTelemetry, standard protocols. **→ Already in our DNA.** DID for identity, OpenTelemetry for observability, NATS for messaging. Keep pushing this.

**What to take:** Agent identity as a first-class citizen (DID + PKI + Vault). The three-tier evaluation loop (built-in feedback + memory learning + external feedback). The "employee onboarding" metaphor for agent provisioning. The IAM model maps perfectly to our federal requirements.

**What to skip:** Frontier is a hosted platform play. We're building self-hosted infrastructure that runs on-prem in DOE labs. Their "shared business context" is their moat — ours needs to be self-hosted, pluggable, and work air-gapped. Also: Frontier's security story is weak ("has not yet demonstrated robust agent-specific security capabilities at enterprise scale" — analyst consensus). That's our differentiation.

**What belongs where:**

| Concept | Layer | Why |
|---|---|---|
| Agent Identity & IAM | **ArcAgent** (core) | Every agent needs its own identity. Can't be optional. |
| Evaluation & Optimization | **ArcAgent** (core + module) | Self-learning is agent-level. Fleet-level eval is arcTeam. |
| Shared Business Context | **arcTeam** (future) | Cross-agent knowledge is orchestration, not agent core. |
| Multi-vendor Support | **Apex** (future) | Model routing is platform-level. ArcAgent uses ArcLLM. |
| Open Standards | **All layers** | Non-negotiable everywhere. |

### 2.2 OpenClaw (Architecture Inspiration)

117K+ GitHub stars, fastest-growing OSS AI project. The architecture is genuinely clever in places, but it's a security disaster for enterprise use.

**What's good (steal these patterns):**

- **Agent Loop Pipeline**: Channel Adapter → Gateway → Lane Queue → Agent Runner → Agentic Loop. The serialized execution per session via "Lane Queues" prevents race conditions — critical at scale.
- **Memory as Markdown files**: MEMORY.md as source of truth + daily logs. Human-readable, git-diffable, auditable. The hybrid search (BM25 + vector, 70/30 weighted) is production-tested.
- **Skills system**: SKILL.md files with YAML frontmatter + natural language instructions. Skills are loaded lazily (not all injected into every prompt), discovered at runtime, and can be hot-reloaded.
- **Context Window Guard**: Monitors token count, triggers compaction before overflow, and — critically — flushes memory to disk BEFORE compacting. Prevents information loss.
- **JSONL Transcripts**: Every message, tool call, and execution result logged as structured events. Full audit trail.
- **Semantic Snapshots for web**: Parses accessibility trees instead of screenshots. Reduces tokens 10x and increases accuracy.

**What's broken (avoid these):**

- **Single process, shared memory**: Everything runs in one Node process. Vulnerability in one area exposes everything.
- **Plaintext credentials**: Stores API keys, OAuth tokens, passwords in plaintext files at `~/.openclaw/credentials/`. This alone disqualifies it from any serious deployment.
- **No authentication by default**: Gateway listens on all interfaces. 40,000+ instances found exposed on the internet within weeks.
- **Skills supply chain**: 20% of ClawHub skills found to contain vulnerabilities. Malicious skills deploying infostealers and reverse shells. No code signing, no provenance verification.
- **The "Lethal Trifecta"**: Access to private data + ability to communicate externally + ability to access untrusted content. Any agent with all three is a walking breach.
- **Prompt injection surface**: Anyone who can message the agent effectively inherits the agent's permissions. No defense in depth.

### 2.3 NanoClaw (Simplicity Inspiration)

Built as a reaction to OpenClaw's complexity. ~100 commits, handful of files. "Understand the codebase in 8 minutes."

**Key takeaways:**

- Agents run in actual Linux containers with filesystem isolation (not application-level permission checks)
- Built on Anthropic's Agents SDK — leverages existing, tested runtime
- Separate containers per agent context (personal vs business = separate isolation boundaries)
- PostgreSQL for stateful storage (not flat files)
- Security posture: secure by default, not secure by configuration

### 2.4 Nanobot (Minimal Core Inspiration)

Ultra-lightweight Clawdbot from HKU. Just 24 commits. Pure Python.

**Key takeaway:** You can get 80% of the agent value from a minimal core: bridge (channel adapter) + workspace (context/memory) + nanobot (agent loop). Everything else is extension.

### 2.5 Security Analysis (Sophos, Coder/Blink, CrowdStrike, Bitdefender)

The security community has dissected OpenClaw-style agents thoroughly. The consensus:

- **Architecture matters more than patches.** OpenClaw's "security was bolted on after the fact." Blink's approach: "start from a position where nothing is exposed."
- **Container isolation is mandatory.** Not application-level sandboxing. Real OS-level isolation where each agent can't see into another's filesystem.
- **Network zero-trust.** Agent should not exist on the public internet. Tailscale/WireGuard-style private mesh, or VPN-only access.
- **Credential management requires a vault.** Never plaintext. Never in the agent's filesystem. Vault with short-lived tokens, rotated automatically.
- **Skills/tools need provenance.** Code signing, supply chain verification, VirusTotal-style scanning before installation.
- **The "Lethal Trifecta" must be broken.** An agent should NEVER simultaneously have: (1) access to private data, (2) ability to communicate externally, AND (3) ability to ingest untrusted content — without a human-in-the-loop gate between them.

---

## 3. ArcAgent Architecture

### 3.1 Design Principles (Codified)

```
SIMPLE:    Minimal core. Every feature is a module. Ship less, extend more.
SECURE:    Secure by default, not by configuration. Zero-trust everything.
SCALABLE:  10,000 agents. Shared-nothing per agent. Coordinate via message bus.
AUDITABLE: Every action is an event. Every event is logged. Every log is searchable.
MODULAR:   One config file. Features toggle on/off. Marketplace-ready extensibility.
```

### 3.2 Core Architecture (The Nucleus)

```
┌──────────────────────────────────────────────────────────────┐
│                     ArcAgent Core                            │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │  Identity    │  │  Config     │  │  Telemetry          │  │
│  │  (who am I?) │  │  (one file) │  │  (OpenTelemetry)    │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
│         │                │                     │             │
│  ┌──────┴────────────────┴─────────────────────┴──────────┐  │
│  │                  Agent Loop (from ArcRun)               │  │
│  │  receive → plan → execute_tool → observe → decide →     │  │
│  │  [loop or respond]                                      │  │
│  └──────┬────────────────┬─────────────────────┬──────────┘  │
│         │                │                     │             │
│  ┌──────┴──────┐  ┌──────┴──────┐  ┌──────────┴──────────┐  │
│  │  LLM Layer  │  │  Tool       │  │  Context             │  │
│  │  (ArcLLM)   │  │  Registry   │  │  Manager             │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │                   Module Bus                            │  │
│  │  (memory, skills, channels, hooks — all plug in here)  │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

**The core has exactly 7 components:**

| Component | Purpose | Lines of Code Target |
|---|---|---|
| **Identity** | DID (W3C), Ed25519 keypair, Vault-backed keys, challenge-response auth, clearance level. See 3.6. | <500 |
| **Config** | Single YAML/TOML file. Everything toggleable. Env var overrides. | <300 |
| **Telemetry** | OpenTelemetry traces, metrics, audit events. Plugs into existing SIEM/observability. | <400 |
| **Agent Loop** | The think-act-observe cycle. Delegates to ArcRun. | <600 |
| **LLM Layer** | Model calls via ArcLLM. Provider-agnostic. Failover built in. | Inherited |
| **Tool Registry** | Register, discover, permission-gate, and invoke tools. | <500 |
| **Context Manager** | System prompt assembly, token monitoring (4 thresholds), semantic compaction with pre-flush. See 3.8. | <600 |
| **Module Bus** | Event-driven extension point. Modules subscribe to lifecycle events. | <400 |

**Total core target: <3,000 lines.** If you can't understand the core in 30 minutes, it's too complex.

### 3.3 The Module Bus (Extensibility Layer)

Everything beyond the 7 core components plugs in via the Module Bus. Modules subscribe to lifecycle events and can extend agent behavior without modifying core code.

```
Lifecycle Events:
  agent:init          → Module can inject config, register tools
  agent:pre_plan      → Module can inject context (memory recall, skill loading)
  agent:post_plan     → Module can review/modify the plan before execution
  agent:pre_tool      → Module can gate tool execution (approval, policy check)
  agent:post_tool     → Module can process tool results (logging, learning)
  agent:pre_respond   → Module can review/modify response before delivery
  agent:post_respond  → Module can trigger follow-up actions
  agent:compact       → Module can flush state before context compaction
  agent:error         → Module can handle/recover from errors
  agent:shutdown      → Module can persist state, clean up
```

**Module Types (the marketplace categories):**

| Type | What It Does | Examples |
|---|---|---|
| **Memory** | Persist and recall information across sessions | markdown-memory, vector-memory, graph-memory, redis-memory |
| **Skills** | Teach the agent how to do specific tasks | coding-skill, research-skill, email-skill, jira-skill |
| **Tools** | Give the agent hands (executable capabilities) | shell-tool, browser-tool, file-tool, api-tool |
| **Channels** | Connect the agent to communication interfaces | slack-channel, teams-channel, api-channel, cli-channel |
| **Hooks** | React to lifecycle events for custom behavior | approval-hook, cost-gate-hook, classification-hook |
| **Policies** | Enforce security/compliance rules | federal-policy, hipaa-policy, export-control-policy |
| **Evaluators** | Score agent performance and trigger improvement | accuracy-eval, cost-eval, latency-eval |

### 3.4 Module Manifest (SKILL.md / MODULE.yaml)

Every module ships with a manifest. OpenClaw uses SKILL.md with YAML frontmatter — good pattern, but we formalize it further for enterprise needs.

```yaml
# MODULE.yaml
apiVersion: arcagent/v1
kind: Module
metadata:
  name: markdown-memory
  version: 1.2.0
  type: memory
  author: blackarc
  license: Apache-2.0
  signature: sha256:abc123...  # Code signing required
  classification: UNCLASSIFIED  # | CUI | SECRET | TS

spec:
  description: |
    File-based memory using Markdown. Human-readable, git-diffable.
    Hybrid search with BM25 + vector (configurable weights).
  
  requires:
    arcagent: ">=1.0.0"
    tools: [file-read, file-write]
    permissions: [workspace.read, workspace.write]
  
  config:
    search_weights:
      vector: 0.7
      keyword: 0.3
    storage_path: "${WORKSPACE}/memory/"
    embedding_provider: local  # local | openai | custom
    max_file_size_mb: 10
  
  events:
    subscribes: [agent:pre_plan, agent:compact, agent:shutdown]
    emits: [memory:recalled, memory:stored, memory:compacted]
  
  # OpenClaw compatibility (optional)
  compat:
    openclaw_skill: true  # Can load OpenClaw SKILL.md format
    skill_path: ./SKILL.md

  # Federal compliance
  compliance:
    data_at_rest_encryption: true
    audit_events: [memory:stored, memory:recalled, memory:compacted]
    data_retention_policy: configurable
```

### 3.5 OpenClaw Compatibility Layer

Since the OpenClaw community has 175K+ GitHub stars and thousands of skills, we should be able to consume their skills without depending on them.

```
┌─────────────────────┐     ┌──────────────────────┐
│  OpenClaw SKILL.md  │────▶│  ArcAgent Adapter     │
│  (YAML frontmatter  │     │  - Parse SKILL.md     │
│   + markdown body)  │     │  - Map to MODULE.yaml │
│                     │     │  - Sandbox execution  │
└─────────────────────┘     │  - Audit all calls    │
                            └──────────┬───────────┘
                                       │
                            ┌──────────▼───────────┐
                            │  ArcAgent Module Bus  │
                            └──────────────────────┘
```

**Rules:**
- OpenClaw skills run in a sandboxed adapter, never with direct system access
- All tool calls from adapted skills go through our Tool Registry (permission-gated)
- Skills are scanned for known vulnerabilities before loading (like Bitdefender's approach)
- Adapted skills have a lower trust level by default (can be promoted after audit)

### 3.6 Agent Identity & IAM (Deep Dive)

Identity is the one component that touches everything: authentication, authorization, audit, inter-agent messaging, and federal compliance. We already built this for enterprise-mesh (Apex) — now we adapt the patterns for ArcAgent's lighter-weight, self-hosted context.

#### What enterprise-mesh (Apex) Already Has

The Apex identity system (in `apex-core/`) implements:

1. **W3C DIDs (Decentralized Identifiers)**: Format `did:apex:{org}:{type}/{id}`. Standard-compliant, self-describing, org-scoped.
2. **Ed25519 Keypairs**: Generated at registration. Public key stored in DB, private key stored in Vault. Fast, small, quantum-resistant-adjacent.
3. **Challenge-Response Auth**: Agent proves identity by signing a random challenge with its private key. No passwords. No shared secrets.
4. **JWT Sessions**: After auth, agent gets a short-lived JWT with `sub` (DID), `agent_id`, `org_id`, expiry. Standard bearer token flow.
5. **Vault-Backed Key Storage**: Private keys never touch the filesystem. HashiCorp Vault with metadata (agent name, type).
6. **Namespace-Based Access Grants**: `domain:path:permission` with wildcard support (`**`). Grants resolve from both direct agent grants and team membership.
7. **Agent Directory**: Status tracking (online/busy/offline/suspended), skills with proficiency scores, heartbeat-based presence detection.

#### What ArcAgent Needs (Adapted)

ArcAgent is simpler than Apex (single agent, not fleet), but identity still matters for:
- **Signing tool calls and messages** (provenance — who did what)
- **Authenticating to external services** (MCP servers, APIs, arcTeam)
- **Audit trail integrity** (tamper-evident logs require signed events)
- **Inter-agent auth** (when agents talk to each other via NATS)
- **Federal compliance** (NIST 800-53 IA controls require strong identity)

#### Identity Architecture for ArcAgent

```
┌─────────────────────────────────────────────────────────────┐
│                    Agent Identity                            │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────────────────────┐ │
│  │  DID             │  │  Keypair (Ed25519)               │ │
│  │  did:arc:{org}:  │  │  - Public key: in config/DB     │ │
│  │  {type}/{id}     │  │  - Private key: Vault or file   │ │
│  └────────┬─────────┘  └────────────┬─────────────────────┘ │
│           │                          │                       │
│  ┌────────┴──────────────────────────┴─────────────────────┐ │
│  │              DID Document (W3C Spec)                     │ │
│  │  - verificationMethod: Ed25519VerificationKey2020       │ │
│  │  - authentication: [did#key-1]                          │ │
│  │  - Portable. Can be verified by any party.              │ │
│  └──────────────────────┬──────────────────────────────────┘ │
│                          │                                    │
│  ┌──────────────────────┴──────────────────────────────────┐ │
│  │              Authentication Flows                        │ │
│  │                                                          │ │
│  │  Local:    Config file → private key → sign messages    │ │
│  │  arcTeam:  Challenge-response → JWT session             │ │
│  │  Agent↔Agent: Signed NATS messages (DID in envelope)   │ │
│  └──────────────────────────────────────────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │              Access Control                              │ │
│  │                                                          │ │
│  │  Tool-level:   allowlist in config                      │ │
│  │  Resource:     domain:path:permission grants             │ │
│  │  Classification: UNCLASSIFIED | CUI | SECRET | TS       │ │
│  │  Clearance:    Agent clearance >= data classification   │ │
│  └──────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

#### Registration Flow

```
1. Admin runs: arcagent init --name "Procurement Agent" --org blackarc --type executor
2. System generates Ed25519 keypair
3. Creates DID: did:arc:blackarc:executor/a1b2c3d4
4. Stores private key in Vault (or ~/.arcagent/keys/ for dev mode)
5. Creates DID Document
6. Writes identity to arcagent.yaml
7. Agent is ready to authenticate
```

#### Two Modes for Key Storage

| Mode | Storage | Use Case |
|---|---|---|
| **Vault** (production) | HashiCorp Vault, AWS Secrets Manager | Federal, enterprise, multi-agent |
| **File** (development) | `~/.arcagent/keys/{did}.key` (chmod 600) | Local dev, single agent, air-gapped |

The Identity component abstracts this — same API regardless of backend.

#### Message Signing (Inter-Agent)

Every NATS message includes:

```json
{
  "from": "did:arc:blackarc:executor/a1b2c3d4",
  "signature": "base64-ed25519-signature-of-payload",
  "payload": { ... },
  "trace_id": "otel-trace-id"
}
```

Receiving agent verifies signature against sender's public key (looked up from registry or cached). No trust without verification.

### 3.7 Evaluation & Optimization Framework

ArcAgent has TWO evaluation systems that work at different levels:

#### Level 1: Self-Evaluation (Agent-Level) — policy.md

Already designed in arcagent-design-v3.md Section 7. The agent evaluates its own work and maintains ranked behavioral notes in `policy.md`. This is the "learning from experience" loop.

```
AGENT DOES WORK → SELF-EVALUATION (async) → POLICY UPDATE
```

- Triggers: task completion, user feedback, session end, every N turns
- Scoring: 1-10 with promotion/modification/removal/decay
- Safety: agent can modify policy.md but NOT identity.md or security constraints

#### Level 2: External Evaluation (Module-Level) — Evaluator Modules

Pluggable evaluator modules that score agent performance from the outside. These DON'T modify the agent — they REPORT to observability/arcTeam.

```yaml
# In arcagent.yaml
modules:
  evaluators:
    - name: accuracy-eval
      config:
        method: llm-as-judge          # Use another LLM to grade outputs
        judge_model: claude-haiku-4-5-20251001  # Cheap model for grading
        sample_rate: 0.2              # Evaluate 20% of responses
        dimensions:
          - correctness
          - helpfulness
          - safety
    - name: cost-eval
      config:
        alert_threshold: 10.00       # Alert if daily cost exceeds $10
        track_per_tool: true          # Cost breakdown by tool
    - name: latency-eval
      config:
        p95_threshold_ms: 5000       # Alert if p95 > 5s
        track_per_model: true
    - name: task-outcome-eval
      config:
        track_completion_rate: true
        track_success_rate: true      # Requires explicit success/fail signal
        track_avg_duration: true
        export_to: opentelemetry      # Metrics available in Grafana
```

#### How the Two Levels Connect

```
┌──────────────────────────────────────────────────────────┐
│                    Agent Loop                             │
│                                                          │
│  ┌──────────┐     ┌──────────────┐     ┌──────────────┐ │
│  │ Execute   │────▶│ Self-Eval    │────▶│ policy.md    │ │
│  │ Task      │     │ (internal)   │     │ (updated)    │ │
│  └─────┬────┘     └──────────────┘     └──────────────┘ │
│        │                                                  │
│        │  agent:post_respond event                        │
│        ▼                                                  │
│  ┌──────────────────────────────────────────────────────┐ │
│  │ Evaluator Modules (external, via Module Bus)         │ │
│  │                                                      │ │
│  │  accuracy-eval:  "Was the response correct?"         │ │
│  │  cost-eval:      "How much did this cost?"           │ │
│  │  latency-eval:   "How long did this take?"           │ │
│  │  task-outcome:   "Did the task succeed?"             │ │
│  │                                                      │ │
│  │  → OpenTelemetry metrics                             │ │
│  │  → Audit log events                                  │ │
│  │  → arcTeam reporting (when connected)                │ │
│  └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

**Key insight from Frontier:** Their evaluation is "similar to employee performance reviews." We implement this as:
- **Self-review** = policy.md (agent grades itself)
- **Manager review** = evaluator modules (external scoring)
- **360 review** = arcTeam aggregation across fleet (future)

#### What Frontier Does That We Should Note (But Not Build Yet)

- **Forward Deployed Engineer feedback loop**: Field feedback → model improvements. This is arcTeam/Apex territory (fleet-level learning).
- **Memory distillation**: Consolidating session notes into conflict-free global memories. We handle this via context.md compaction (Section 3.8).
- **Model selection optimization**: Choosing the best model for a task based on evaluation history. This is ArcLLM territory (provider selection based on cost/quality/latency).

### 3.8 Context Management & Compaction

The context window is finite. Every agent needs to manage it. This is a core component, not a module.

#### Landscape: How Others Do It

| System | Detection | Compaction Strategy | Semantic Preservation |
|---|---|---|---|
| **OpenClaw** | 80-90% threshold + manual `/compact` | Selective: summarize older messages, keep recent | LLM summarization |
| **MemGPT/Letta** | Automatic (undocumented trigger) | Sliding window: keep 70% recent, summarize rest | LLM summarization |
| **Claude API** | Token count > 150K (configurable, min 50K) | Full summary → compaction block → drop prior messages | LLM summarization |
| **NanoClaw** | Not applicable (container-per-session, short-lived) | No compaction (relies on container restart) | N/A |

**Key finding:** All production systems use LLM-based semantic summarization, not truncation. Truncation loses meaning. Bullets lose nuance. Good compaction preserves intent, state, decisions, and next steps.

#### ArcAgent Context Manager

```
┌──────────────────────────────────────────────────────────┐
│                  Context Manager                          │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  System Prompt Assembly                              │ │
│  │  identity.md + policy.md + context.md + skill docs  │ │
│  │  Token budget: tracked per-component                │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  Conversation History                                │ │
│  │  [message_1] [message_2] ... [message_N]            │ │
│  │  Token count: tracked incrementally                 │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  Token Monitor                                       │ │
│  │                                                      │ │
│  │  total = system_prompt + history + tool_results      │ │
│  │  ratio = total / max_tokens                         │ │
│  │                                                      │ │
│  │  < 70%:  Green.  Normal operation.                  │ │
│  │  70-85%: Yellow. Prune old tool results (in-memory) │ │
│  │  85-95%: Red.    Trigger semantic compaction.        │ │
│  │  > 95%:  Critical. Emergency compact + flush.       │ │
│  └─────────────────────────────────────────────────────┘ │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │  Compaction Engine                                   │ │
│  │                                                      │ │
│  │  Strategy: Semantic Sliding Window                  │ │
│  │                                                      │ │
│  │  1. FLUSH: Write context.md with current state      │ │
│  │     (OpenClaw pattern — never lose info)            │ │
│  │                                                      │ │
│  │  2. SUMMARIZE: LLM call to summarize older messages │ │
│  │     - NOT bullets. Narrative summary.               │ │
│  │     - Preserves: decisions, state, blockers,        │ │
│  │       next steps, key facts, user preferences       │ │
│  │     - Uses cheaper model (configurable)             │ │
│  │                                                      │ │
│  │  3. REPLACE: Swap old messages with summary block   │ │
│  │     Keep recent N messages intact (configurable)    │ │
│  │                                                      │ │
│  │  4. LOG: Compaction event to audit trail             │ │
│  │     (what was summarized, token savings, model used) │ │
│  └─────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

#### Compaction Config

```yaml
# In arcagent.yaml
context:
  max_tokens: 100000
  compaction:
    trigger_ratio: 0.85           # Compact at 85% of max
    pre_compact_flush: true       # Flush to context.md BEFORE compacting
    keep_recent_messages: 10      # Always keep last 10 messages intact
    strategy: semantic            # semantic | sliding_window | truncate
    summarizer:
      model: claude-haiku-4-5-20251001  # Cheap model for summarization
      prompt: |
        Summarize this conversation segment. Preserve:
        - Current state and progress
        - Decisions made and their rationale
        - Active blockers and pending items
        - Key facts and user preferences
        - Next steps and planned actions
        Write as a coherent narrative, not bullets.
      max_summary_tokens: 2000
    prune_tool_results_at: 0.70   # Prune verbose tool results at 70%
```

#### Why Narrative, Not Bullets

Bullet-point summaries lose:
- **Reasoning chains**: "We tried X because of Y, it failed because Z, so we pivoted to W"
- **Temporal relationships**: "After the database migration, we noticed..."
- **Implicit context**: "The user prefers concise responses" (lost if you bullet-ize decisions)
- **Decision rationale**: "We chose PostgreSQL over MongoDB because of ACID requirements"

Narrative summaries preserve meaning. The extra tokens are worth it — a 2000-token narrative carries more signal than 500 tokens of bullets.

### 3.9 Channel Architecture (No Gateway)

#### The Question: Do We Need a Gateway?

OpenClaw has a full gateway: Channel Adapter → Gateway → Lane Queue → Agent Runner → Agentic Loop. The Gateway is a central control plane that routes messages, manages sessions, and coordinates channels. It's 400K+ lines.

NanoClaw skips the gateway entirely: WhatsApp → SQLite → Polling loop → Container. 500 lines.

Nanobot uses a lightweight MessageBus: Channel Adapters → MessageBus (pub-sub) → AgentLoop. ~4K lines.

#### Answer: No Gateway. Module Bus IS the Router.

ArcAgent's Module Bus (Section 3.3) already serves the gateway's core function — routing events between components. Channels are just another module type. Adding a dedicated gateway creates complexity without benefit for our architecture.

```
                              ArcAgent
┌──────────────────────────────────────────────────────────┐
│                                                          │
│  Channel Modules              Module Bus                 │
│  ┌──────────────┐     ┌──────────────────────────────┐  │
│  │ slack-channel │────▶│                              │  │
│  └──────────────┘     │  agent:message_received      │  │
│  ┌──────────────┐     │  agent:message_sending       │  │
│  │ api-channel   │────▶│  channel:inbound            │  │
│  └──────────────┘     │  channel:outbound            │  │
│  ┌──────────────┐     │                              │──▶ Agent Loop
│  │ cli-channel   │────▶│  (+ all other lifecycle     │  │
│  └──────────────┘     │   events from Section 3.3)  │  │
│  ┌──────────────┐     │                              │  │
│  │ teams-channel │────▶│                              │  │
│  └──────────────┘     └──────────────────────────────┘  │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

#### What OpenClaw's Gateway Actually Does (And Where We Handle It)

| Gateway Function | ArcAgent Equivalent |
|---|---|
| Route messages to agents | Module Bus event routing |
| Session management | Context Manager (per-session state) |
| Lane Queue (serial execution) | Agent Loop (one agent = one loop, no race condition) |
| Channel lifecycle | Module Bus `agent:init` / `agent:shutdown` events |
| Access control | Identity component (Section 3.6) |
| Message normalization | Channel module responsibility (each normalizes to standard format) |

#### Message Normalization (Nanobot Pattern)

Every channel module normalizes platform-specific messages to a standard `InboundMessage`:

```python
@dataclass
class InboundMessage:
    channel: str              # "slack" | "teams" | "api" | "cli"
    sender_id: str            # Platform-specific user ID
    conversation_id: str      # Session/thread identifier
    content: str              # Message text
    media: list[str] = field(default_factory=list)  # File paths
    metadata: dict = field(default_factory=dict)     # Platform extras
    classification: str = "UNCLASSIFIED"             # Data classification
    trace_id: str = ""        # OpenTelemetry trace
```

#### Why Not a Gateway

1. **ArcAgent is one agent.** OpenClaw's gateway exists because it routes messages to MANY agents (multi-agent routing). ArcAgent is a single agent — there's nothing to route to.
2. **No race conditions.** OpenClaw's Lane Queue prevents concurrent access to session files. ArcAgent's agent loop is inherently serial per session.
3. **Channels are modules.** They plug into the Module Bus like everything else. No special infrastructure needed.
4. **When we need multi-agent routing, that's arcTeam.** The orchestrator layer (Section 5) handles fleet-level message routing. The individual agent doesn't need a gateway.

#### Nanobot's "Bridge" Concept

Nanobot calls its channel layer a "bridge" — it bridges the gap between platform-specific protocols and the agent's normalized interface. Our Channel modules serve the same purpose. The MessageBus pattern (async queues for inbound/outbound) is elegant and we should use it:

```python
class ChannelModule(Protocol):
    """Every channel implements this interface."""
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, message: OutboundMessage) -> None: ...

    # Inbound messages published to Module Bus as channel:inbound events
```

The Module Bus acts as Nanobot's MessageBus — decoupling channels from the agent loop. No bridge needed as a separate component.

---

## 4. Security Architecture

### 4.1 Threat Model (Informed by OpenClaw Failures)

| Threat | OpenClaw Reality | ArcAgent Mitigation |
|---|---|---|
| Exposed gateway | 40,000+ instances on public internet | No public listener. Mesh network only (Tailscale/WireGuard). |
| Plaintext credentials | `~/.openclaw/credentials/` in plain text | Vault-backed (HashiCorp Vault / AWS Secrets Manager). Short-lived tokens only. |
| Malicious skills | 20% of ClawHub skills contained vulnerabilities | Code signing required. Provenance chain. Pre-install scanning. Sandbox execution. |
| Prompt injection | Message sender inherits agent permissions | Input classification module. Untrusted input flagged. Tool calls gated by policy. |
| Supply chain attack | Typosquatting, compromised accounts on ClawHub | Module registry with verified publishers. GPG-signed packages. SBOM required. |
| Data exfiltration | Agent can read private data AND send messages | Break the Lethal Trifecta: data access and external comms require separate approval flows. |
| Container escape | Docker sandbox escape via PATH manipulation (CVE-2026-24763) | gVisor/Firecracker microVMs for isolation. No Docker-in-Docker. |
| Session hijacking | WebSocket origin validation missing (CVE-2026-25253) | mTLS on all internal comms. Origin validation. Session tokens rotate per request. |

### 4.2 Security Layers

```
Layer 0: NETWORK          → Mesh VPN only. No public endpoints. mTLS everywhere.
Layer 1: IDENTITY          → Agent identity via PKI. Every agent has a cert.
Layer 2: AUTHENTICATION    → Vault-issued short-lived tokens. MFA for human operators.
Layer 3: AUTHORIZATION     → RBAC + ABAC. Tool-level permissions. Clearance levels.
Layer 4: ISOLATION          → Each agent in its own container/microVM. No shared memory.
Layer 5: INPUT VALIDATION  → All inbound messages classified (trusted/untrusted/hostile).
Layer 6: TOOL POLICY        → Allowlist-only. Dangerous patterns blocked at parse level.
Layer 7: OUTPUT CONTROL     → Response classification. PII/CUI detection before emission.
Layer 8: AUDIT              → Every event to immutable log. SIEM integration. Tamper-evident.
Layer 9: MODULE SUPPLY CHAIN → Signed modules. Provenance. Vulnerability scanning.
```

### 4.3 The Single Config File

```yaml
# arcagent.yaml — ONE file to rule them all
apiVersion: arcagent/v1

agent:
  id: "arc-agent-001"
  name: "Procurement Analyst"
  clearance: CUI          # UNCLASSIFIED | CUI | SECRET | TS
  
identity:
  vault_path: "secret/agents/arc-agent-001"
  cert_path: "/etc/arcagent/certs/"
  token_ttl: 3600         # seconds
  
llm:
  provider: arcllm        # Uses ArcLLM unified layer
  model: claude-sonnet-4-5-20250929
  fallback: [claude-haiku-4-5-20251001, gpt-4o]
  max_tokens: 8192
  temperature: 0.3
  cost_limit_daily: 50.00  # USD

runtime:
  engine: arcrun           # Uses ArcRun runtime
  isolation: microvm       # container | microvm | process
  max_concurrent_tools: 3
  tool_timeout_seconds: 30
  max_loop_iterations: 25
  
context:
  max_tokens: 100000
  compaction_threshold: 0.8  # Compact at 80% of max
  pre_compact_flush: true    # Flush memory before compacting (OpenClaw pattern)
  
network:
  mode: mesh               # mesh | vpn | localhost
  mesh_network: tailscale  # tailscale | wireguard | zerotier
  public_endpoints: false   # NEVER in federal deployments
  mtls: true
  
telemetry:
  enabled: true
  provider: opentelemetry
  export:
    traces: "otlp://collector:4317"
    metrics: "otlp://collector:4317"
    audit: "syslog://siem:514"
  audit:
    log_all_tool_calls: true
    log_all_llm_calls: true
    log_all_memory_ops: true
    tamper_evident: true
    retention_days: 2555     # 7 years for federal

modules:
  memory:
    type: markdown-memory    # or: vector-memory, graph-memory, none
    config:
      search_weights: { vector: 0.7, keyword: 0.3 }
      
  skills:
    - name: research-skill
    - name: email-skill
      config:
        require_approval: [send, delete]
    
  tools:
    policy: allowlist        # allowlist | denylist
    allowed: [file-read, file-write, browser, api-call]
    blocked_patterns: ["rm -rf", "curl | bash", "eval(", "$("]
    elevated:
      enabled: false         # Requires separate approval workflow
      
  channels:
    - type: api-channel      # REST/gRPC endpoint
    - type: slack-channel
      config:
        workspace: blackarc
        
  policies:
    - name: federal-policy
      config:
        classification_gate: true
        pii_detection: true
        export_control: true
    - name: lethal-trifecta-breaker
      config:
        require_approval_for_external_comms: true
        isolate_untrusted_input: true

  evaluators:
    - name: accuracy-eval
    - name: cost-eval
      config:
        alert_threshold: 10.00
```

---

## 5. Scaling to 10,000 Agents

### 5.1 Architecture for Scale

```
┌─────────────────────────────────────────────────────┐
│                 ArcAgent Orchestrator                │
│            (built on ArcRun + LangGraph)             │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │ Registry  │  │ Scheduler│  │ Health Monitor     │  │
│  │ (who      │  │ (when    │  │ (is it alive?      │  │
│  │  exists?) │  │  to run?)│  │  performing well?) │  │
│  └─────┬────┘  └─────┬────┘  └────────┬──────────┘  │
│        └──────────────┼───────────────┘              │
│                       │                              │
│              ┌────────▼────────┐                     │
│              │   Message Bus    │                     │
│              │ (NATS / Redis    │                     │
│              │  Streams)        │                     │
│              └────────┬────────┘                     │
│                       │                              │
└───────────────────────┼──────────────────────────────┘
                        │
          ┌─────────────┼─────────────┐
          │             │             │
    ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼─────┐
    │ Agent Pool│ │ Agent Pool│ │ Agent Pool│
    │ Node 1    │ │ Node 2    │ │ Node N    │
    │           │ │           │ │           │
    │ Agent 1   │ │ Agent 101 │ │ Agent 9901│
    │ Agent 2   │ │ Agent 102 │ │ Agent 9902│
    │ ...       │ │ ...       │ │ ...       │
    │ Agent 100 │ │ Agent 200 │ │ Agent 10K │
    └───────────┘ └───────────┘ └───────────┘
```

### 5.2 Key Scale Decisions

| Decision | Choice | Rationale |
|---|---|---|
| **Agent isolation** | microVM (Firecracker) | ~125ms cold start, <5MB memory overhead, hardware-level isolation |
| **Inter-agent comms** | NATS JetStream | 18M msgs/sec, built-in persistence, subject-based routing |
| **State storage** | PostgreSQL + per-agent SQLite | PG for coordination state, SQLite for agent-local state (like OpenClaw) |
| **Memory index** | Per-agent SQLite with sqlite-vec | No shared vector DB. Each agent owns its index. |
| **Scheduling** | Temporal.io workflows | Durable execution, replay, visibility. Already battle-tested at scale. |
| **Config distribution** | etcd / Consul | Distributed config with watch support for hot-reload |

### 5.3 Agent Communication Protocol

Agents need to coordinate without tight coupling. Simple pub/sub over NATS:

```
# Agent-to-agent message format
{
  "from": "arc-agent-001",
  "to": "arc-agent-042",        # or "*" for broadcast, "team:procurement" for group
  "type": "request | response | event | handoff",
  "subject": "procurement.analysis.complete",
  "payload": { ... },
  "trace_id": "abc-123",        # OpenTelemetry propagation
  "timestamp": "2026-02-14T...",
  "classification": "CUI",      # Data classification travels with the message
  "ttl": 300                    # Message expires after 5 min
}
```

---

## 6. Agent Extension Pattern (Specialized Agents)

ArcAgent core becomes specialized agents by composing modules:

```yaml
# coding-agent.yaml (extends arcagent.yaml)
extends: arcagent-base

agent:
  name: "Coding Agent"
  
modules:
  skills:
    - name: code-analysis-skill
    - name: test-writing-skill
    - name: pr-review-skill
    - name: refactoring-skill
  
  tools:
    allowed: [file-read, file-write, shell-exec, git, browser]
    shell_allowlist: [npm, pip, python, node, go, cargo, make, git]
    
  memory:
    type: graph-memory   # Code agents benefit from relationship-aware memory
    config:
      index_code: true
      track_dependencies: true

---

# design-agent.yaml (extends arcagent.yaml)
extends: arcagent-base

agent:
  name: "Design Agent"
  
modules:
  skills:
    - name: figma-skill
    - name: brand-guidelines-skill
    - name: accessibility-audit-skill
    
  tools:
    allowed: [file-read, file-write, browser, image-gen, figma-api]
    
  memory:
    type: markdown-memory
    config:
      track_design_decisions: true

---

# procurement-agent.yaml (extends arcagent.yaml)  
extends: arcagent-base

agent:
  name: "Procurement Intelligence Agent"
  
modules:
  skills:
    - name: sam-gov-skill
    - name: contract-analysis-skill
    - name: pricing-intelligence-skill
    
  tools:
    allowed: [file-read, browser, api-call, database-query]
    
  policies:
    - name: federal-policy
    - name: far-compliance-policy
```

---

## 7. Implementation Roadmap

### Phase 1: Foundation (Weeks 1-4)
- [ ] Define Module Bus event system and module manifest spec
- [ ] Implement core 7 components (<3,000 lines)
- [ ] Wire ArcLLM integration (model calls, failover)
- [ ] Wire ArcRun integration (agent loop, tool execution)
- [ ] Single config file parser with validation
- [ ] Basic telemetry (OpenTelemetry traces + audit log)
- [ ] Container isolation (Docker first, Firecracker later)
- [ ] Basic markdown-memory module
- [ ] CLI: `arcagent init`, `arcagent run`, `arcagent status`

### Phase 2: Security Hardening (Weeks 5-8)
- [ ] Vault integration for credential management
- [ ] mTLS for all internal communications
- [ ] Agent identity via PKI (cert-per-agent)
- [ ] Tool policy engine (allowlist, blocked patterns, approval workflows)
- [ ] Input classification module (trusted/untrusted/hostile)
- [ ] Output classification (PII/CUI detection)
- [ ] Lethal Trifecta breaker (approval gates between data access + external comms)
- [ ] Module signing and provenance verification
- [ ] Federal policy module (FedRAMP, NIST 800-53 controls)

### Phase 3: Scale Infrastructure (Weeks 9-12)
- [ ] NATS JetStream for inter-agent messaging
- [ ] Agent orchestrator (registry, scheduler, health monitor)
- [ ] Firecracker microVM isolation
- [ ] Temporal.io workflow integration
- [ ] Agent pool management (spawn, scale, destroy)
- [ ] Distributed config (etcd)
- [ ] Load testing: 100 → 1,000 → 10,000 agents

### Phase 4: Ecosystem (Weeks 13-16)
- [ ] OpenClaw skill adapter (import community skills with sandboxing)
- [ ] Module marketplace infrastructure
- [ ] Specialized agent templates (coding, design, procurement, etc.)
- [ ] Evaluation framework (accuracy, cost, latency scoring)
- [ ] Self-improvement loop (agent can propose skill/memory improvements)
- [ ] Documentation + onboarding for CTG Federal / BlackArc customers
- [ ] SBOM generation for all deployments

### Phase 5: Advanced Capabilities (Weeks 17+)
- [ ] Multi-agent coordination patterns (swarm, hierarchy, consensus)
- [ ] Agent-to-agent handoff protocols
- [ ] Learning module (agents improve from experience, not just memory)
- [ ] Graph memory module (temporal knowledge graphs à la Graphiti)
- [ ] Voice/multimodal channel modules
- [ ] FedRAMP authorization package preparation

---

## 8. Competitive Positioning

| Capability | OpenClaw | NanoClaw | OpenAI Frontier | **ArcAgent** |
|---|---|---|---|---|
| Open source | ✅ MIT | ✅ MIT | ❌ Hosted | ✅ |
| Federal compliance | ❌ | ❌ | Partial (SOC2) | ✅ FedRAMP/NIST/CMMC |
| Self-hosted | ✅ | ✅ | ❌ | ✅ |
| Container isolation | ❌ (application-level) | ✅ (Apple containers) | Unknown | ✅ (Firecracker microVM) |
| Credential management | ❌ (plaintext) | Basic | Enterprise IAM | ✅ (Vault-backed) |
| Scale (10K agents) | ❌ (single process) | ❌ (single machine) | ✅ | ✅ |
| Audit trail | Partial (JSONL) | Minimal | ✅ | ✅ (SIEM-integrated, tamper-evident) |
| Module marketplace | ✅ (ClawHub, but insecure) | ❌ | Coming | ✅ (signed, verified) |
| Agent-to-agent comms | Basic (multi-agent routing) | ✅ (Agent Swarms) | ✅ | ✅ (NATS pub/sub) |
| Codebase complexity | 52+ modules, 45+ deps | ~10 files | N/A (hosted) | <3,000 lines core |
| Time to understand | Days | 8 minutes | N/A | 30 minutes |

---

## 9. Key Technical Decisions Summary

| Decision | Choice | Why |
|---|---|---|
| Language | Python (core) + TypeScript (UI/channels) | Python for ML/AI ecosystem, TypeScript for web interfaces |
| Config format | YAML with JSON Schema validation | Human-readable, widely supported, schema-enforceable |
| Message bus | NATS JetStream | Best throughput, built-in persistence, lightweight |
| State storage | PostgreSQL (coordination) + SQLite (per-agent) | PG for scale, SQLite for agent-local speed and portability |
| Isolation | Firecracker microVMs | Hardware-level isolation, ~125ms cold start, minimal overhead |
| Secrets | HashiCorp Vault | Industry standard, supports transit encryption, dynamic secrets |
| Observability | OpenTelemetry → Grafana stack | Open standard, works with any SIEM, no vendor lock-in |
| CI/CD | GitHub Actions + ArgoCD | GitOps for agent deployment, version-controlled everything |
| Memory default | Markdown files (OpenClaw pattern) | Auditable, diffable, portable. Vector search layered on top. |

---

## 10. What Makes This Different

1. **Federal-first, not federal-patched.** Security and compliance are in the foundation, not bolted on. Every agent has an identity, every action has an audit trail, every credential is vault-managed.

2. **Simple enough to inspect, powerful enough to scale.** <3,000 lines of core code that a security auditor can review in a day. But it scales to 10,000 agents via the orchestrator layer.

3. **The marketplace play.** The Module Bus creates a platform. Memory providers, skill libraries, policy engines, evaluation frameworks — all pluggable. This is how you build an ecosystem that generates recurring revenue while giving customers exactly what they need.

4. **Built on proven foundations.** ArcLLM handles the model complexity. ArcRun handles the runtime complexity. ArcAgent handles the agent intelligence. Each layer has a clear boundary and can evolve independently.

5. **OpenClaw-compatible without OpenClaw-dependent.** We can consume the massive OpenClaw ecosystem's skills through an adapter, but our native module format is purpose-built for enterprise, security, and scale.

6. **Self-improving agents.** The policy.md self-learning system + evaluator modules create agents that get better with use. Not just "run and forget" — continuous improvement with full audit trail. Frontier calls this "built-in feedback loops." We call it core architecture.

---

## 11. Cross-Product Notes (Future Reference)

These items are NOT part of ArcAgent core but will be needed when building the higher layers. Capturing here so nothing gets lost.

### arcTeam (Fleet Orchestration Layer)

| Item | Description | Source |
|---|---|---|
| **Shared Business Context** | Semantic layer connecting agents to enterprise data (CRM, ERP, data warehouses). Every agent references same "institutional knowledge." Frontier's core differentiator. | OpenAI Frontier |
| **Fleet-Level Evaluation** | Aggregate performance metrics across all agents. Compare agents. Identify best performers. Route tasks to best-fit agents. | Frontier FDE feedback loop |
| **Agent Directory** | Already partially built in enterprise-mesh. Agents register capabilities, domains, skills. Other agents discover and route work. | Apex `directory_service.py` |
| **Schedule Coordination** | View all agent schedules. Override/cancel. Set global limits. Aggregate for capacity planning. | arcagent-design-v3.md Sec 4 |
| **Cross-Agent Memory** | Shared knowledge that spans agents. Entity knowledge accessible fleet-wide with access controls. | Apex `entity_knowledge_service.py` |

### Apex (Platform Layer)

| Item | Description | Source |
|---|---|---|
| **Multi-Vendor Model Support** | Route tasks to best model (OpenAI, Anthropic, Google, custom). Cost/quality/latency optimization. Unified execution environment. | Frontier multi-vendor, ArcLLM provider abstraction |
| **Model Selection Optimization** | Use evaluation history to choose best model for task type. "This agent does code review best with Claude, but data analysis best with GPT." | Frontier eval → model selection |
| **Orchestration-Level Lock-In** | Frontier creates lock-in at orchestration, not model. We should avoid this — open standards, portable configs, no proprietary formats. | Frontier analysis |
| **Enterprise IAM Integration** | SSO/SAML/OAuth for human operators. Unified identity across humans and agents. Agent onboarding flows. | Frontier IAM, Apex auth service |
| **Governance Dashboard** | Centralized view of all agent actions, costs, compliance status, security events. | Frontier admin console concept |

### Open Standards Commitment

| Standard | Where Used | Why |
|---|---|---|
| **W3C DIDs** | Agent identity | Decentralized, verifiable, portable. No vendor lock-in on identity. |
| **OpenTelemetry** | Observability | Works with any SIEM/monitoring. Grafana, Datadog, Splunk, whatever. |
| **NATS** | Inter-agent messaging | Open protocol. No proprietary message format. |
| **JSON Schema** | Config validation | Universal. Any tool can validate. |
| **MCP** | Tool integration | Anthropic's standard. Widely adopted. Language-agnostic. |
| **JSONL** | Audit/transcripts | Line-delimited JSON. Streamable. Parseable by anything. |
| **Ed25519** | Cryptographic identity | Fast, small, secure. Standard key format. |
