```
     ┌─────────────────────────────────────────────────────────────────┐
     │                                                                 │
     │     ╔══╗                 ╔══╗                        ╔╗        │
     │    ╔╝  ╚╗               ╚╗ ╚╗                       ╔╝╚╗       │
     │   ╔╝ ╔╗ ╚╗  ╔═╗╔══╗    ╔╝ ╔╝ ╔══╗╔══╗╔═╗ ╔══╗  ╔═╩╗╔╩═╗     │
     │   ║  ╔╝╔╗║  ║╔╝║╔═╝   ╔╝ ╔╝  ║╔╗║║╔╗║║╔╝ ║╔╗║  ║╔╗║║╔╗║     │
     │   ║  ╚═╝║║  ║║ ║╚═╗  ╔╝ ╔╝   ║╚╝║║╚╝║║║  ║╚╝║  ║║║║║╚╝║     │
     │   ╚═════╝╚╝ ╚╝ ╚══╝  ╚══╝    ╚══╝╚═╗║╚╝  ╚══╝  ╚╝╚╝╚══╝     │
     │                                    ╔═╝║                        │
     │   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  ╚══╝  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓   │
     │                                                                 │
     │   Enterprise-Grade Autonomous Agent Nucleus                     │
     │   Inspectable ∙ Auditable ∙ Deployable in a SCIF               │
     │                                                                 │
     │   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
     │   │ IDENTITY │  │ CONTEXT  │  │  TOOLS   │  │ MODULES  │      │
     │   │ Ed25519  │  │ Token-   │  │ Sandboxed│  │ Event-   │      │
     │   │ DID Auth │  │ Budgeted │  │ Policy   │  │ Driven   │      │
     │   └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘      │
     │        └──────────────┴──────────────┴──────────────┘           │
     │                          ╔═══════╗                              │
     │                          ║ AGENT ║                              │
     │                          ╚═══╤═══╝                              │
     │                     ┌────────┴────────┐                         │
     │                  ╔══╧══╗           ╔══╧══╗                      │
     │                  ║ARCLLM║          ║ARCRUN║                     │
     │                  ╚══════╝          ╚══════╝                     │
     │                                                                 │
     └─────────────────────────────────────────────────────────────────┘
```

---

**arcAgent** is the core autonomous agent layer for the [Apex](https://github.com/blackarcsystems) platform. Built on [ArcLLM](https://github.com/blackarcsystems/arcllm) (provider-agnostic LLM calls) and [ArcRun](https://github.com/blackarcsystems/arcrun) (runtime agentic loop), it provides a minimal, secure, modular foundation for specialized agents that can scale to 10,000+ concurrent instances while meeting federal security requirements.

## Why arcAgent?

Most agent frameworks make a fundamental tradeoff: **simplicity or security**. OpenAI's Agents SDK is hosted-only. OpenClaw has 40,000+ exposed instances with plaintext credentials. NanoClaw caps at a single machine. None of them can pass a FedRAMP audit.

arcAgent refuses that tradeoff.

| | arcAgent | OpenClaw | NanoClaw | OpenAI Agents |
|---|---|---|---|---|
| **Core size** | <3,500 LOC | ~40K LOC | ~500 LOC | Hosted |
| **Self-hostable** | Yes | Yes | Yes | No |
| **Air-gap ready** | Yes | No | Partial | No |
| **Cryptographic identity** | Ed25519 DID | None | None | None |
| **Audit trail** | OpenTelemetry | None | None | Partial |
| **Credential isolation** | Vault-backed | Plaintext files | None | Managed |
| **Module system** | Event-driven bus | 52+ coupled modules | None | Plugins |
| **Concurrent agents** | 10,000+ (via arcTeam) | Limited | Single machine | Managed |

## Architecture

```
Apex (Platform)
├── ArcLLM        — Unified LLM call layer (provider-agnostic)
├── ArcRun        — Runtime agentic loop (think-act-observe)
├── ArcAgent      — Core autonomous agent (THIS PROJECT)
│   └── Specialized agents extend ArcAgent via modules
└── arcTeam       — Fleet coordination layer (orchestration, scheduling)
```

### Core Components (~3,200 LOC)

| Component | Responsibility |
|---|---|
| **Identity** | Ed25519 keypairs, W3C DID format (`did:arc:{org}:{type}/{id}`), challenge-response auth |
| **Config** | TOML-based, Pydantic-validated, environment variable overrides |
| **Telemetry** | OpenTelemetry traces + metrics, structured audit events on every action |
| **Context Manager** | Token-budgeted context window with tiered compaction (mask → summarize → truncate) |
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
| **Memory** | Implemented | Markdown-based with hybrid search (BM25 + vector), entity extraction, policy engine |
| **Skills** | Implemented | Markdown skill files with argument schemas, discovered at startup |
| **Tools** | Implemented | Native Python tools with 4-transport architecture (Native, MCP, HTTP, Process) |
| **Extensions** | Implemented | Hot-loadable Python from workspace directories |

## Quick Start

### Prerequisites

- Python 3.12+
- [ArcLLM](https://github.com/blackarcsystems/arcllm) and [ArcRun](https://github.com/blackarcsystems/arcrun) installed
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

ArcAgent ships with sandboxed filesystem tools:

| Tool | Description |
|---|---|
| `bash` | Execute shell commands (sandboxed, timeout-guarded) |
| `read` | Read file contents with offset/limit |
| `write` | Write files within workspace |
| `edit` | String-based find-and-replace editing |
| `ls` | List directory contents |
| `find` | Search files by glob pattern |
| `grep` | Search file contents with regex |

All tool calls are:
- Schema-validated before execution
- Policy-checked against allow/deny lists
- Timeout-guarded (configurable per tool)
- Audit-logged via OpenTelemetry

## Security Model

ArcAgent is designed for environments where security is non-negotiable.

### Identity & Authentication

Every agent has a cryptographic identity (Ed25519 keypair) with a W3C-format DID. Challenge-response authentication proves identity without exposing keys.

### Zero-Trust Architecture

- **Credentials** never touch the filesystem — Vault-backed, short-lived tokens
- **Modules** are signed and verified before loading
- **Tool access** is policy-controlled with explicit allow/deny lists
- **Inter-agent comms** use mTLS on all channels
- **Audit trail** on every operation — every tool call, every LLM request, every state change

### Lethal Trifecta Prevention

An agent must **never** simultaneously have:
1. Private data access
2. External communications
3. Untrusted input

...without explicit human-in-the-loop approval. This is enforced by the policy module.

### Compliance Targets

| Framework | Coverage |
|---|---|
| **FedRAMP** | Vault-backed secrets, audit trails, encryption at rest |
| **NIST 800-53** | Identity (IA), Audit (AU), Access Control (AC) |
| **CMMC** | CUI handling, classification-aware data flow |

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
│   └── modules/        # Memory, entity extraction, policy engine
├── integration/        # 20% — component interaction tests
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
│   └── memory/             # Markdown memory with hybrid search
│       ├── markdown_memory.py   # Core memory module
│       ├── hybrid_search.py     # BM25 + vector search
│       ├── entity_extractor.py  # Named entity extraction
│       └── policy_engine.py     # Self-learning policy
├── tools/                  # Built-in sandboxed tools
│   ├── bash.py, read.py, write.py, edit.py
│   ├── ls.py, find.py, grep.py
│   └── _validation.py     # Shared validation logic
└── utils/
    └── io.py               # Async I/O utilities
```

## Roadmap

- [x] **Phase 1a** — Core components (identity, config, telemetry, context, tools, modules)
- [x] **Phase 1b** — Agent runtime (ArcRun integration, sessions, skills, extensions, settings)
- [x] **Phase 1c** — Memory module (markdown memory, hybrid search, entity extraction, policy)
- [ ] **Phase 2** — MCP/HTTP/Process tool transports, CLI integration via ArcCLI
- [ ] **Phase 3** — arcTeam fleet coordination, scheduling, shared context
- [ ] **Phase 4** — Firecracker isolation, module marketplace, FedRAMP authorization package

## License

MIT

---

<p align="center">
  <strong>Built by <a href="https://blackarcsystems.com">BlackArc Systems</a></strong><br>
  <em>The only agent framework you can deploy in a SCIF.</em>
</p>
