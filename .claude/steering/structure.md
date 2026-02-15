# Project Structure

> This document provides stable architectural context that informs all feature specifications.
> Feature-specific details go in `.claude/specs/{feature}/` documents.

## Validation Checklist

- [x] Directory structure documented
- [x] Architecture pattern defined
- [x] Implementation boundaries set
- [x] Pattern references linked
- [x] Naming conventions documented
- [x] No [NEEDS CLARIFICATION] markers

---

## Directory Layout

> The canonical project structure. Features should follow this organization.

### Repository Structure

```
arcagent/
├── arcagent/                    # Python package (core)
│   ├── __init__.py
│   ├── core/                    # The 7 core components (<3,000 LOC total)
│   │   ├── identity.py          # DID, keypair, auth, clearance (<500 LOC)
│   │   ├── config.py            # TOML parser, Pydantic validation, env overrides (<300 LOC)
│   │   ├── telemetry.py         # OpenTelemetry traces, metrics, audit (<400 LOC)
│   │   ├── agent.py             # Orchestrator (wires components, invokes ArcRun) (<600 LOC)
│   │   ├── tool_registry.py     # Register, discover, permission-gate tools (<500 LOC)
│   │   ├── context_manager.py   # System prompt, history, token budget, compaction (<600 LOC)
│   │   └── module_bus.py        # Event-driven extension point (<400 LOC)
│   │
│   ├── modules/                 # Built-in modules (ship with core)
│   │   ├── memory/              # Default markdown-memory module
│   │   │   ├── __init__.py
│   │   │   ├── markdown_memory.py
│   │   │   ├── entity_extractor.py
│   │   │   ├── hybrid_search.py
│   │   │   └── MODULE.yaml
│   │   ├── policies/            # Policy modules (base + plugins)
│   │   │   ├── base.py           # PolicyModule protocol — all policies implement this
│   │   │   ├── federal.py        # Plugin: NIST 800-53/FedRAMP controls
│   │   │   └── lethal_trifecta.py # Plugin: Breaks lethal trifecta with approval gates
│   │   └── channels/            # Built-in channel modules
│   │       ├── cli_channel.py
│   │       └── api_channel.py
│   │
│   ├── adapters/                # Compatibility adapters
│   │   └── openclaw_adapter.py  # Import OpenClaw SKILL.md format
│   │
│   ├── schemas/                 # Pydantic schemas (API contracts)
│   │   ├── agent.py             # AgentCreate, AgentResponse, DIDDocument
│   │   ├── config.py            # Config validation schemas
│   │   ├── module.py            # Module manifest schemas
│   │   ├── message.py           # InboundMessage, OutboundMessage
│   │   └── auth.py              # ChallengeResponse, TokenPayload
│   │
│   └── cli/                     # CLI commands
│       ├── __init__.py
│       ├── init.py              # arcagent init
│       ├── run.py               # arcagent run
│       └── status.py            # arcagent status
│
├── tests/
│   ├── unit/                    # Unit tests (mirrors arcagent/)
│   │   ├── core/
│   │   │   ├── test_identity.py
│   │   │   ├── test_config.py
│   │   │   ├── test_context_manager.py
│   │   │   └── test_module_bus.py
│   │   ├── modules/
│   │   └── schemas/
│   ├── integration/             # Integration tests
│   │   ├── test_identity_vault.py
│   │   ├── test_module_lifecycle.py
│   │   └── test_context_compaction.py
│   ├── e2e/                     # End-to-end tests
│   │   └── test_agent_loop.py
│   ├── security/                # Security-specific tests
│   │   ├── test_auth_bypass.py
│   │   └── test_module_signing.py
│   └── performance/             # Benchmarks
│       ├── bench_cold_start.py
│       └── bench_compaction.py
│
├── docs/                        # Documentation
│   ├── architecture/            # Architecture Decision Records
│   ├── modules/                 # Module development guide
│   └── deployment/              # Deployment guides (federal, enterprise, dev)
│
├── .claude/
│   ├── steering/                # Project context (this file)
│   ├── specs/                   # Feature specifications
│   └── settings.local.json      # Claude Code settings
│
├── arcagent-plan.md             # Master architecture plan
├── arcagent-design-v3.md        # Detailed design document
├── arcagent.toml.example        # Example agent config
├── pyproject.toml               # Python project config (ruff, mypy, pytest)
└── README.md                    # Project README
```

### Agent Workspace Structure (Per-Agent Runtime)

```
workspace/                       # Created at runtime, per-agent
├── identity.md                  # WHO — read-only to agent (admin-controlled)
├── context.md                   # WHAT — agent working memory (agent R/W)
├── policy.md                    # HOW — learned behaviors (agent R/W)
│
├── notes/                       # Daily logs (agent append-only)
│   ├── 2026-02-14.md
│   └── 2026-02-15.md
│
├── entities/                    # Extracted knowledge (async, agent-maintained)
│   ├── index.json
│   └── {entity-name}/
│       ├── facts.jsonl
│       └── summary.md
│
├── skills/                      # Knowledge files (loaded on demand)
│   ├── {skill-name}/
│   │   └── SKILL.md
│   └── _agent-created/          # Skills the agent built itself
│
├── library/                     # Agent-created reusable artifacts
│   ├── scripts/                 # Utility scripts the agent wrote
│   ├── templates/               # Reusable document/report templates
│   ├── prompts/                 # Saved prompt patterns that worked
│   ├── data/                    # Reference data the agent collected (tables, lookups, extracts)
│   └── snippets/                # Reusable code/text fragments
│
├── sessions/                    # Active session transcripts (JSONL)
├── archive/                     # Compacted old sessions
├── schedules.json               # Cron/interval/once schedule entries
└── config.json                  # Agent-level runtime config
```

---

## Architecture Pattern

### Overall Architecture: Modular Nucleus

ArcAgent uses a "nucleus" pattern: a tiny core (<3K LOC) surrounded by pluggable modules connected via an event-driven Module Bus.

```
┌──────────────────────────────────────────────────────────────┐
│                     ArcAgent Core (Nucleus)                    │
│                                                                │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐   │
│  │  Identity    │  │  Config     │  │  Telemetry          │   │
│  │  (DID+PKI)  │  │  (TOML)     │  │  (OpenTelemetry)    │   │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘   │
│         │                │                     │               │
│  ┌──────┴────────────────┴─────────────────────┴──────────┐   │
│  │                  Agent Loop (ArcRun)                     │   │
│  │  receive → plan → execute_tool → observe → decide       │   │
│  └──────┬────────────────┬─────────────────────┬──────────┘   │
│         │                │                     │               │
│  ┌──────┴──────┐  ┌──────┴──────┐  ┌──────────┴──────────┐   │
│  │  LLM Layer  │  │  Tool       │  │  Context             │   │
│  │  (ArcLLM)   │  │  Registry   │  │  Manager             │   │
│  └─────────────┘  └─────────────┘  └─────────────────────┘   │
│                                                                │
│  ┌────────────────────────────────────────────────────────┐   │
│  │                   Module Bus                            │   │
│  │  Events: init, pre_plan, post_plan, pre_tool,          │   │
│  │  post_tool, pre_respond, post_respond, compact, error   │   │
│  └────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
         │                    │                    │
    ┌────┴────┐         ┌────┴────┐          ┌────┴────┐
    │ Memory  │         │ Skills  │          │ Channel │
    │ Modules │         │ Modules │          │ Modules │
    └─────────┘         └─────────┘          └─────────┘
    ┌─────────┐         ┌─────────┐
    │ Policy  │         │  Hook   │
    │ Modules │         │ Modules │
    └─────────┘         └─────────┘

External (not agent-internal):
    ┌──────────────────────────────────────────┐
    │ Evaluators consume OTel telemetry        │
    │ (arcTeam or standalone services)         │
    └──────────────────────────────────────────┘
```

### Layer Responsibilities

| Layer | Responsibility | Components |
|-------|---------------|------------|
| **Core** | Agent identity, config, lifecycle, telemetry | 7 core components |
| **Runtime** | Think-act-observe loop, tool execution | ArcRun (inherited) |
| **LLM** | Provider-agnostic model calls, failover | ArcLLM (inherited) |
| **Modules** | Extensions via Module Bus subscription | Memory, skills, tools, channels, hooks, policies |
| **External** | Evaluation via OTel telemetry consumption | Evaluators (arcTeam or standalone services) |
| **Workspace** | Per-agent persistent storage | identity.md, context.md, policy.md, notes/, entities/ |

### Data Flow

```
Message In (from channel module)
    │
    ▼
Context Manager: Assemble system prompt
    │  (identity.md + policy.md + context.md + skills)
    │  Check token budget, prune if needed
    ▼
Agent Loop (ArcRun): receive → plan
    │  Module Bus: agent:pre_plan (memory recall, skill loading)
    ▼
Agent Loop: execute_tool
    │  Module Bus: agent:pre_tool (approval, policy check)
    │  Tool Registry: permission gate → invoke tool
    │  Module Bus: agent:post_tool (logging, learning)
    ▼
Agent Loop: observe → decide
    │  Loop or respond?
    ▼
Agent Loop: respond
    │  Module Bus: agent:pre_respond (output classification, PII check)
    │  Module Bus: agent:post_respond (evaluation, learning)
    ▼
Channel Module: Send response
    │
    ▼
Self-Evaluation (async): Update policy.md (agent-internal)
Entity Extraction (async): Update entities/
    │
    ▼ (OTel export)
External Evaluators: Consume telemetry, score agent performance (arcTeam/standalone)
```

---

## Implementation Boundaries

### Must Preserve

| Item | Location | Why |
|------|----------|-----|
| Core 7 components | `arcagent/core/` | Architecture foundation. <3K LOC constraint. |
| Identity/auth flow | `arcagent/core/identity.py` | Security-critical. Challenge-response, DID, signing. |
| Module Bus events | `arcagent/core/module_bus.py` | Module contract. Changing events breaks all modules. |
| Config schema | `arcagent/schemas/config.py` | Agent config contract. Breaking changes break all deployments. |
| Message protocol | NATS message format | Inter-agent contract. Changing format requires fleet-wide upgrade. |
| Module manifest | `MODULE.yaml` format | Module contract. Breaking changes break marketplace. |

### Can Modify

| Item | Location | Constraints |
|------|----------|-------------|
| Built-in modules | `arcagent/modules/` | Keep interfaces stable, implementations flexible |
| CLI commands | `arcagent/cli/` | User-facing, maintain backwards compat on flags |
| Schemas | `arcagent/schemas/` | Additive changes OK, removal requires deprecation |
| Adapters | `arcagent/adapters/` | Compatibility layer, can evolve independently |

### Must Not Touch

| Item | Location | Reason |
|------|----------|--------|
| ArcLLM internals | `../arcllm/` | Separate project. Use via interface only. |
| ArcRun internals | `../arcrun/` | Separate project. Use via interface only. |
| Agent workspace at runtime | `workspace/identity.md` | Admin-controlled. Agent cannot modify its own identity. |
| Audit logs | OTel export | Immutable. Tamper-evident. Never delete or modify. |

---

## Module Organization

### Core Components

```
arcagent/core/
├── identity.py          # DID, keypair, auth, clearance
├── config.py            # TOML parser, Pydantic validation, env vars
├── telemetry.py         # OTel traces, metrics, audit
├── agent.py             # Orchestrator (wires components, invokes ArcRun)
├── tool_registry.py     # Tool registration, discovery, permission gating
├── context_manager.py   # System prompt, history, compaction
└── module_bus.py        # Event system, module lifecycle
```

### Module Structure

```
arcagent/modules/{module-type}/{module-name}/
├── __init__.py          # Module entry point
├── {module-name}.py     # Implementation
├── MODULE.yaml          # Module manifest (required)
└── tests/               # Module-specific tests (optional)
```

---

## Naming Conventions

### Files

| Type | Convention | Example |
|------|------------|---------|
| Python modules | snake_case | `identity.py` |
| Python packages | snake_case | `arcagent/core/` |
| Config files | kebab-case | `arcagent.toml` |
| Module manifests | SCREAMING | `MODULE.yaml` |
| Workspace files | lowercase.ext | `identity.md`, `policy.md` |
| Tests | test_ prefix | `test_identity.py` |
| Benchmarks | bench_ prefix | `bench_cold_start.py` |

### Code

| Type | Convention | Example |
|------|------------|---------|
| Classes | PascalCase | `IdentityService` |
| Functions | snake_case | `generate_keypair` |
| Constants | SCREAMING_SNAKE | `MAX_LOOP_ITERATIONS` |
| Type aliases | PascalCase | `AgentConfig` |
| Enums | PascalCase + SCREAMING values | `AgentType.EXECUTOR` |
| Dataclasses | PascalCase | `InboundMessage` |
| Protocols | PascalCase with Protocol suffix | `MemoryProvider` |

### Database

| Type | Convention | Example |
|------|------------|---------|
| Tables | snake_case, plural | `agents`, `access_grants` |
| Columns | snake_case | `created_at`, `public_key` |
| Indexes | ix_{table}_{columns} | `ix_agents_org_status` |
| Foreign Keys | FK on column name | `organization_id` |
| Constraints | uq_{table}_{columns} | `uq_access_grants_full` |

### DIDs and Identifiers

| Type | Format | Example |
|------|--------|---------|
| Agent DID | `did:arc:{org}:{type}/{id}` | `did:arc:blackarc:executor/a1b2c3d4` |
| Module ID | `{type}/{name}@{version}` | `memory/markdown-memory@1.2.0` |
| Event name | `{scope}:{action}` | `agent:pre_plan`, `memory:recalled` |
| Config path | dot-separated | `modules.memory.config.search_weights` |

---

## Pattern References

### Core Patterns

| Pattern | Location | When to Use |
|---------|----------|-------------|
| Identity & Auth | `arcagent-plan.md` Section 3.6 | Any identity-related feature |
| Module Bus Events | `arcagent-plan.md` Section 3.3 | Adding module lifecycle hooks |
| Context Compaction | `arcagent-plan.md` Section 3.8 | Context management features |
| Channel Architecture | `arcagent-plan.md` Section 3.9 | Adding new channel modules |
| Evaluation Framework | `arcagent-plan.md` Section 3.7 | External evaluation via OTel (arcTeam/standalone) |

### Design Patterns

| Pattern | Location | When to Use |
|---------|----------|-------------|
| Memory System | `arcagent-design-v3.md` Section 2 | Memory module development |
| Session Files | `arcagent-design-v3.md` Section 3 | Workspace structure |
| Self-Learning (Policy) | `arcagent-design-v3.md` Section 7 | Policy system features |
| Self-Scheduling | `arcagent-design-v3.md` Section 4 | Agent-managed schedules (cron/interval/once), ScheduleEntry with prompt |
| Skills vs Tools | `arcagent-design-v3.md` Section 5 | Skill/tool development |

### Reference Implementations

| Reference | Location | What to Learn |
|-----------|----------|---------------|
| Identity Service | `../enterprise-mesh/apex-core/apex_core/services/identity.py` | DID creation, keypair gen, challenge-response |
| Access Service | `../enterprise-mesh/apex-core/apex_core/services/access.py` | Grant resolution, wildcard matching, caching |
| Agent Model | `../enterprise-mesh/apex-core/apex_core/models/agent.py` | DID, status, skills, presence tracking |
| Access Model | `../enterprise-mesh/apex-core/apex_core/models/access.py` | Namespace grants, permissions |

---

## Key Directories Purpose

| Directory | Purpose | Owner |
|-----------|---------|-------|
| `arcagent/core/` | The 7 core components. <3K LOC. | Core team (security-reviewed) |
| `arcagent/modules/` | Built-in modules shipped with core | Module team |
| `arcagent/schemas/` | Pydantic schemas (API contracts) | Core team |
| `arcagent/cli/` | CLI commands | Core team |
| `arcagent/adapters/` | Compatibility adapters (OpenClaw) | Community / module team |
| `tests/` | All test code | Everyone |
| `docs/` | Architecture, modules, deployment | Everyone |
| `workspace/` | Runtime per-agent storage | Agent (read/write at runtime) |

---

## Directory Map Template

> Use this format in SDD documents to show file changes.

```
arcagent/
├── core/
│   └── new_component.py         # NEW: Description
├── modules/
│   └── {module-type}/
│       └── {module-name}/
│           ├── __init__.py      # NEW: Module entry
│           ├── implementation.py # NEW: Module logic
│           └── MODULE.yaml      # NEW: Module manifest
├── schemas/
│   └── new_schema.py            # NEW: Pydantic schemas
└── tests/
    └── unit/
        └── core/
            └── test_new.py      # NEW: Unit tests
```

Legend:
- `# NEW:` - New file to create
- `# MODIFY:` - Existing file to change
- `# DELETE:` - File to remove (rare)

---

## Open Questions (Architecture)

- [ ] Should modules be loadable as separate Python packages (pip install) or only as local directories?
- [ ] Should the Module Bus use asyncio events or a proper pub-sub library internally?
- [ ] How should the OpenClaw adapter handle skills that require tools ArcAgent doesn't have?

---

## References

- Architecture plan: `arcagent-plan.md`
- Design document: `arcagent-design-v3.md`
- Enterprise mesh reference: `../enterprise-mesh/apex-core/`
