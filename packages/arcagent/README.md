```
╭──────────────────────────────────────────────────────╮
│                                                      │
│   ▄▀█ █▀█ █▀▀ ▄▀█ █▀▀ █▀▀ █▄ █ ▀█▀                │
│   █▀█ █▀▄ █▄▄ █▀█ █▄█ ██▄ █ ▀█  █                 │
│                                                      │
│   Autonomous Agent Nucleus                           │
│   Inspectable · Auditable · Deployable in a SCIF     │
│                                                      │
├──────────────────────────────────────────────────────┤
│  Ed25519 Identity · Sandboxed Tools · Event Bus      │
╰──────────────────────────────────────────────────────╯
```

**ArcAgent is the core autonomous agent layer for the Arc platform.** Built on [ArcLLM](../arcllm/) (provider-agnostic LLM calls) and [ArcRun](../arcrun/) (runtime agentic loop), it provides a minimal, secure, modular foundation for specialized agents that can scale to 10,000+ concurrent instances while meeting federal security requirements.

---

## Why ArcAgent

Most agent frameworks make a fundamental tradeoff: **simplicity or security**. OpenAI's Agents SDK is hosted-only. OpenClaw has 40,000+ exposed instances with plaintext credentials. NanoClaw caps at a single machine. None of them can pass a FedRAMP audit.

ArcAgent refuses that tradeoff.

| | ArcAgent | OpenClaw | NanoClaw | OpenAI Agents |
|---|---|---|---|---|
| **Core size** | <3,500 LOC | ~40K LOC | ~500 LOC | Hosted |
| **Self-hostable** | Yes | Yes | Yes | No |
| **Air-gap ready** | Yes | No | Partial | No |
| **Cryptographic identity** | Ed25519 DID | None | None | None |
| **Audit trail** | Hash-chained OpenTelemetry | None | None | Partial |
| **Credential isolation** | Vault-backed | Plaintext files | None | Managed |
| **Module system** | Event-driven bus | 52+ coupled modules | None | Plugins |
| **Memory** | Hybrid search + biological | Basic RAG | None | Managed |
| **Concurrent agents** | 10,000+ (via ArcTeam) | Limited | Single machine | Managed |

## Architecture

```
Arc Platform
├── ArcLLM        — Unified LLM call layer (11 providers, budget, routing)
├── ArcRun        — Runtime agentic loop (hash-chained events, container sandbox)
├── ArcAgent      — Core autonomous agent (THIS PROJECT)
│   └── Specialized agents extend ArcAgent via modules
└── ArcTeam       — Fleet coordination layer (messaging, tasks, knowledge, files)
```

### Core Components (~3,200 LOC)

| Component | Responsibility |
|---|---|
| **Identity** | Ed25519 keypairs, W3C DID format (`did:arc:{org}:{type}/{id}`), challenge-response auth |
| **Config** | TOML-based, Pydantic-validated, environment variable overrides |
| **Telemetry** | OpenTelemetry traces + metrics, structured audit events on every action |
| **Context Manager** | Token-budgeted context window with tiered compaction (mask -> summarize -> truncate) |
| **Tool Registry** | Native Python tools with schema validation, policy enforcement, timeout guards |
| **Module Bus** | Event-driven extension system — modules subscribe to lifecycle events |
| **Session Manager** | JSONL session persistence with retention policies |
| **Skill Registry** | Markdown-defined skills discovered from workspace |
| **Extensions** | Hot-loadable Python extensions from workspace directories |
| **Settings Manager** | Runtime-mutable settings with validation and change events |

### Module System

Modules extend ArcAgent without touching core code:

| Module | Status | Description |
|---|---|---|
| **Memory** | Complete | Markdown-based with hybrid search (BM25 + vector), entity extraction, policy engine |
| **Biological Memory** | Complete | Long-term identity-aware memory — personality, episodes, working memory, consolidation |
| **Skills** | Complete | Markdown skill files with argument schemas, discovered at startup |
| **Tools** | Complete | Native Python tools with 4-transport architecture (Native, MCP, HTTP, Process) |
| **Extensions** | Complete | Hot-loadable Python from workspace directories |

## Quick Start

### Prerequisites

- Python 3.12+
- [ArcLLM](../arcllm/) and [ArcRun](../arcrun/) installed
- An LLM API key (Anthropic, OpenAI, etc.)

### Install

```bash
pip install -e .
```

### Create an Agent

```python
import asyncio
from arcagent.core.agent import ArcAgent
from arcagent.core.config import load_config

async def main():
    config = load_config("arcagent.toml")
    agent = ArcAgent(config)
    await agent.startup()

    # Single task
    result = await agent.run("List all files in the workspace")
    print(result.content)

    await agent.shutdown()

asyncio.run(main())
```

### Configuration

Copy and customize the example config:

```bash
cp arcagent.toml.example arcagent.toml
```

```toml
[agent]
name = "my-agent"
org = "my-org"
type = "executor"
workspace = "./workspace"

[llm]
model = "anthropic/claude-sonnet-4-5-20250929"
max_tokens = 4096

[identity]
did = ""                    # Auto-generated if empty
key_dir = "~/.arcagent/keys"

[telemetry]
enabled = true
service_name = "my-agent"
log_level = "INFO"

[context]
max_tokens = 128000
compact_threshold = 0.85    # LLM summarization at 85%
```

Environment variables override config with `ARCAGENT_` prefix:

```bash
export ARCAGENT_LLM__MODEL="openai/gpt-4o"
export ARCAGENT_TELEMETRY__LOG_LEVEL="DEBUG"
```

## Biological Memory

ArcAgent's biological memory module provides long-term identity-aware memory that persists across sessions:

- **Identity Manager** — Tracks agent traits, preferences, and behavioral patterns. Agents develop personality through experience.
- **Working Memory** — Session-scoped scratchpad for in-progress reasoning, intermediate state, and active goals.
- **Consolidator** — Promotes working memory to long-term episodic storage with relevance scoring and deduplication.
- **Retriever** — Context-aware memory retrieval weighted by recency, relevance, and importance.

```python
# Via CLI
arc agent bio_memory status     # Memory overview
arc agent bio_memory identity   # Agent identity traits
arc agent bio_memory episodes   # Long-term episodes
arc agent bio_memory working    # Current working memory
```

All memory writes are sanitized through the shared text sanitizer (NFKC normalization, zero-width character stripping, control character removal) to prevent memory poisoning attacks (OWASP ASI-06).

## Agent Workspace

Each agent operates within a structured workspace:

```
workspace/
├── identity.md         # Agent identity (read-only to agent)
├── context.md          # Working memory (token-budgeted)
├── policy.md           # Self-learning behavioral notes
├── sessions/           # JSONL conversation history
├── notes/              # Daily markdown notes
├── entities/           # Extracted entity files
└── extensions/         # Hot-loadable Python extensions
    └── calculator.py   # Example: custom tool
```

## Built-in Tools

ArcAgent ships with sandboxed filesystem tools, each classified for
parallel-dispatch safety per SPEC-017 R-020:

| Tool | Classification | Description |
|---|---|---|
| `bash` | state_modifying | Execute shell commands (sandboxed, timeout-guarded) |
| `read` | read_only | Read file contents with offset/limit |
| `write` | state_modifying | Write files within workspace |
| `edit` | state_modifying | String-based find-and-replace editing |
| `ls` | read_only | List directory contents |
| `find` | read_only | Search files by glob pattern |
| `grep` | read_only | Search file contents with regex |

All tool calls are:
- Schema-validated before execution
- **Routed through the 5-layer tool policy pipeline** (first-DENY-wins, fail-closed, p95 < 1ms)
- Timeout-guarded (configurable per tool)
- Audit-logged via OpenTelemetry + the `policy.evaluate` event stream
- Hash-chained for tamper-evident trails (via ArcRun)

### Self-modification tools (SPEC-017 R-050, opt-in)

| Tool | Federal tier | Enterprise tier | Personal tier |
|---|---|---|---|
| `create_skill(name, markdown_body)` | allowed | allowed | allowed |
| `improve_skill(name, new_markdown_body)` | allowed | allowed | allowed |
| `create_tool(name, python_source)` | **DENIED** | approval | allowed |
| `create_extension(name, python_source, module_yaml)` | **DENIED** | approval | allowed |
| `list_artifacts(kind)` | allowed | allowed | allowed |
| `reload_artifacts()` | allowed | allowed | allowed |

Dynamic tool creation runs source through the `DynamicToolLoader`: encoding check → 9-category AST validation → sandbox compile with `RESTRICTED_BUILTINS` → registration. Every action emits a structured audit event. See the runbook at `docs/runbooks/spec-017-operations.md`.

## Proactive Scheduling (SPEC-017 Phase 6)

The `modules/proactive/` module replaces the legacy `pulse` + `scheduler`
modules with a single engine. Highlights:

- **Drift-free reschedule** — `next_run = last_actual_run + interval - overhead`. Accumulated scheduling overhead does not bias the timeline.
- **Circuit breaker per schedule** — Resilience4j state machine (CLOSED → OPEN → HALF_OPEN → CLOSED) with exponential backoff.
- **Clock-warp detection** — Wall clock vs monotonic delta divergence beyond 5s emits a `clock_warp` event.
- **Heartbeat isolation** — Heartbeat decisions run with a dedicated `HeartbeatContext` that carries only `now_iso` + `idle_since_seconds`. No session state, no tool results, no conversation history.
- **Leader election** — `LeaderElection` Protocol with `NoOpLeaderElection` (single instance) and `InMemoryElection` (multi-process tests). K8s Lease / Redis Lock implementations satisfy the same Protocol.
- **Timezone + DST** — IANA timezone config, overnight windows (`end < start`), spring-forward skipped, fall-back not double-fired.

## Security Model

ArcAgent is designed for environments where security is non-negotiable.

### Identity & Authentication

Every agent has a cryptographic identity (Ed25519 keypair) with a W3C-format DID. Challenge-response authentication proves identity without exposing keys.

### Zero-Trust Architecture

- **Credentials** never touch the filesystem — Vault-backed, short-lived tokens
- **Modules** are signed and verified before loading
- **Tool access** is policy-controlled with explicit allow/deny lists
- **Inter-agent comms** use mTLS on all channels
- **Audit trail** on every operation — hash-chained, tamper-evident, verifiable
- **Memory writes** sanitized against poisoning (NFKC normalization, zero-width stripping)

### Lethal Trifecta Prevention

An agent must **never** simultaneously have:
1. Private data access
2. External communications
3. Untrusted input

...without explicit human-in-the-loop approval. This is enforced by the policy module.

### Compliance Targets

| Framework | Coverage |
|---|---|
| **FedRAMP** | Vault-backed secrets, hash-chained audit trails, encryption at rest |
| **NIST 800-53** | Identity (IA), Audit (AU) with hash chain (AU-9/AU-10), Access Control (AC) |
| **CMMC** | CUI handling, classification-aware data flow |
| **OWASP LLM Top 10** | Prompt injection (LLM01), PII disclosure (LLM02), excessive agency (LLM06) |
| **OWASP Agentic AI Top 10** | Memory poisoning (ASI06), tool misuse (ASI02), rogue agents (ASI10) |

## Development

### Quality Tools

```bash
ruff check .                    # Lint
ruff format .                   # Format
mypy arcagent/ --strict         # Type check
pytest --cov=arcagent           # Test + coverage
```

### Quality Gates

| Gate | Threshold |
|---|---|
| Line coverage | >= 80% |
| Branch coverage | >= 75% |
| Core coverage | >= 90% |
| Cyclomatic complexity | <= 10 per function |
| Ruff errors | 0 |
| mypy errors | 0 |
| Critical vulnerabilities | 0 |
| Core LOC | < 3,500 |

### Test Structure

```
tests/
├── unit/               # 70% — isolated component tests
│   ├── core/           # Identity, config, telemetry, agent, etc.
│   ├── tools/          # Bash, read, write, edit, ls, find, grep
│   ├── modules/        # Memory, entity extraction, policy, bio_memory
│   └── utils/          # Sanitizer, IO utilities
├── integration/        # 20% — component interaction tests
│   ├── test_bio_memory_integration.py
│   └── test_bio_memory_retrieval.py
└── e2e/                # 10% — full agent lifecycle tests
```

## Project Structure

```
arcagent/
├── core/                   # The nucleus (<3,500 LOC)
│   ├── agent.py            # Orchestrator — wires components, invokes ArcRun
│   ├── identity.py         # Ed25519 DID, keypairs, challenge-response
│   ├── config.py           # TOML config with Pydantic validation
│   ├── telemetry.py        # OpenTelemetry traces, metrics, audit events
│   ├── context_manager.py  # Token-budgeted context with tiered compaction
│   ├── tool_registry.py    # Tool registry with 4-transport architecture
│   ├── module_bus.py       # Event-driven module extension system
│   ├── session_manager.py  # JSONL session persistence
│   ├── skill_registry.py   # Markdown skill discovery
│   ├── extensions.py       # Hot-loadable Python extensions
│   ├── settings_manager.py # Runtime-mutable settings
│   ├── errors.py           # Error hierarchy
│   └── protocols.py        # Protocol interfaces
├── modules/
│   ├── memory/             # Markdown memory with hybrid search
│   │   ├── markdown_memory.py   # Core memory module
│   │   ├── hybrid_search.py     # BM25 + vector search
│   │   ├── entity_extractor.py  # Named entity extraction
│   │   └── policy_engine.py     # Self-learning policy
│   └── bio_memory/         # Biological long-term memory
│       ├── identity_manager.py  # Persistent agent identity
│       ├── working_memory.py    # Session-scoped scratchpad
│       ├── consolidator.py      # Working -> episodic promotion
│       ├── retriever.py         # Context-aware retrieval
│       └── config.py            # Module configuration
├── tools/                  # Built-in sandboxed tools
│   ├── bash.py, read.py, write.py, edit.py
│   ├── ls.py, find.py, grep.py
│   └── _validation.py     # Shared validation logic
└── utils/
    ├── io.py               # Async I/O utilities
    └── sanitizer.py        # Shared text sanitization (ASI-06)
```

## Roadmap

- [x] **Phase 1a** — Core components (identity, config, telemetry, context, tools, modules)
- [x] **Phase 1b** — Agent runtime (ArcRun integration, sessions, skills, extensions, settings)
- [x] **Phase 1c** — Memory module (markdown memory, hybrid search, entity extraction, policy)
- [x] **Phase 2a** — Biological memory (identity, working memory, consolidation, retrieval)
- [x] **Phase 2b** — Shared text sanitizer (centralized ASI-06 defense)
- [ ] **Phase 3** — MCP/HTTP/Process tool transports, CLI integration via arccmd
- [ ] **Phase 4** — ArcTeam fleet coordination, scheduling, shared context
- [ ] **Phase 5** — Firecracker isolation, module marketplace, FedRAMP authorization package

## License

This project is licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

Copyright (c) 2025-2026 BlackArc Systems.

---

<p align="center">
  <strong>Built by <a href="https://blackarcsystems.com">BlackArc Systems</a></strong><br>
  <em>The only agent framework you can deploy in a SCIF.</em>
</p>
