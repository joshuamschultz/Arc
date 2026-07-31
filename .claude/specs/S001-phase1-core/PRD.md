# PRD: Phase 1 Core Components

**Spec ID**: S001
**Status**: PENDING
**Last Updated**: 2026-02-14

---

## 1. Overview

### 1.1 Problem Statement

ArcAgent needs a minimal, secure nucleus (<3,000 LOC) that integrates with existing ArcLLM (provider-agnostic LLM calls) and ArcRun (runtime agent loop) foundations. The core must provide config, identity, telemetry, event-driven extensibility, tool management, context management, and orchestration — without duplicating any ArcRun capabilities.

### 1.2 Target Users

- **BlackArc engineering team** building ArcAgent core and specialized agents
- **Federal Systems Integrators** deploying agents in SCIF/air-gapped environments
- **Enterprise AI Platform Builders** evaluating agent frameworks

### 1.3 Success Criteria

| Criteria | Target | Measurement |
|----------|--------|-------------|
| Core LOC | < 3,000 total | `cloc arcagent/core/` |
| Test coverage | >= 80% line, >= 90% core | `pytest --cov` |
| Type safety | 0 mypy errors | `mypy arcagent/ --strict` |
| Lint clean | 0 ruff errors | `ruff check .` |
| ArcRun integration | Single loop, no duplication | Architecture review |
| Module Bus working | Events emit, handlers fire, veto works | Integration test |
| All 4 tool transports | native, MCP, HTTP, process functional | Integration test |
| Config loads | TOML parsed, validated, env overrides | Unit test |
| Telemetry exports | OTel spans nest correctly | Integration test |

---

## 2. Requirements

### 2.1 Config (config.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-CFG-01 | Parse `arcagent.toml` using `tomllib` (stdlib) | P0 | Brainstorm D9 |
| REQ-CFG-02 | Validate config with Pydantic 2.x models | P0 | Brainstorm D9 |
| REQ-CFG-03 | Support environment variable overrides with `ARCAGENT_` prefix | P0 | Research |
| REQ-CFG-04 | Two-phase error reporting: TOML syntax (line numbers) + Pydantic validation (key paths) | P1 | Research |
| REQ-CFG-05 | Config sections: agent, llm, tools, modules, telemetry, identity | P0 | Steering |

### 2.2 Identity (identity.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-IDN-01 | Generate Ed25519 keypair using PyNaCl | P0 | Steering |
| REQ-IDN-02 | Create W3C DID: `did:arc:{org}:{type}/{id}` | P0 | Steering |
| REQ-IDN-03 | Store keys file-based in dev mode | P0 | Brainstorm (Phase 1) |
| REQ-IDN-04 | Sign messages with private key | P0 | Steering |
| REQ-IDN-05 | Verify signatures with public key | P0 | Steering |
| REQ-IDN-06 | Load identity from config (DID, key paths) | P0 | Brainstorm |
| REQ-IDN-07 | Reuse ArcLLM `VaultResolver` for secret resolution (vault → file fallback) | P0 | User Decision |

### 2.3 Telemetry (telemetry.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-TEL-01 | Create parent OTel spans: `arcagent.session`, `arcagent.turn`, `arcagent.tool` | P0 | Brainstorm D10 |
| REQ-TEL-02 | ArcLLM spans auto-nest under ArcAgent spans via OTel context propagation | P0 | Brainstorm D10 |
| REQ-TEL-03 | Structured logging via Python `logging` module | P0 | Brainstorm D10 |
| REQ-TEL-04 | Every action is an audit event (NIST 800-53 AU) | P0 | Steering |
| REQ-TEL-05 | Configurable via `[telemetry]` config section | P1 | Steering |

### 2.4 Module Bus (module_bus.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-BUS-01 | Async event system with `subscribe(event, handler, priority)` | P0 | Brainstorm D1-D4 |
| REQ-BUS-02 | Bridge ArcRun EventBus to Module Bus via `on_event` callback | P0 | Brainstorm D1 |
| REQ-BUS-03 | All handlers `async def` (async-only contract) | P0 | Brainstorm D2 |
| REQ-BUS-04 | `EventContext.veto(reason)` for pre-event interception | P0 | Brainstorm D3 |
| REQ-BUS-05 | Integer priority ordering (lower runs first) | P0 | Brainstorm D4 |
| REQ-BUS-06 | Error isolation: one handler failure doesn't crash others | P0 | Research |
| REQ-BUS-07 | Handler timeout (30s default) | P1 | Research |
| REQ-BUS-08 | Module lifecycle: `startup()` / `shutdown()` with reverse-order shutdown | P0 | Research |
| REQ-BUS-09 | Events: `agent:init`, `agent:pre_plan`, `agent:post_plan`, `agent:pre_tool`, `agent:post_tool`, `agent:pre_respond`, `agent:post_respond`, `agent:compact`, `agent:error`, `agent:shutdown` | P0 | Steering |
| REQ-BUS-10 | Concurrent execution within same priority level | P1 | Research |

### 2.5 Tool Registry (tool_registry.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-TRG-01 | Register tools from 4 transports: native, MCP, HTTP, process | P0 | Brainstorm D8 |
| REQ-TRG-02 | Produce `list[arcrun.Tool]` with wrapped execute functions | P0 | Brainstorm D6 |
| REQ-TRG-03 | Wrap tool execution with audit events via Module Bus | P0 | Brainstorm D6 |
| REQ-TRG-04 | Permission gating via config-driven allowlists/denylists | P0 | Steering |
| REQ-TRG-05 | MCP client via official `mcp` SDK v1.26.0 (stdio transport minimum) | P0 | Research |
| REQ-TRG-06 | Tool discovery from MCP servers at startup | P0 | Research |
| REQ-TRG-07 | HTTP tool transport via httpx async client | P1 | Brainstorm |
| REQ-TRG-08 | Process tool transport via asyncio subprocess | P1 | Brainstorm |
| REQ-TRG-09 | Tool timeout enforcement (30s default, configurable) | P0 | Steering |

### 2.6 Context Manager (context_manager.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-CTX-01 | Assemble system prompt from identity.md + policy.md + context.md | P0 | Brainstorm D7 |
| REQ-CTX-02 | Client-side token estimation for proactive pruning | P0 | Brainstorm D5 |
| REQ-CTX-03 | Provider-reported token tracking from `LLMResponse.usage` | P0 | Brainstorm D5 |
| REQ-CTX-04 | Observation masking: prune old tool outputs before LLM summarization | P0 | Research |
| REQ-CTX-05 | Provide `transform_context` callback to ArcRun | P0 | Brainstorm D7 |
| REQ-CTX-06 | Configurable token budget with thresholds (70% prune, 85% compact, 95% emergency) | P0 | Research |
| REQ-CTX-07 | Conservative 1.1x multiplier on client-side estimates | P1 | Brainstorm D5 |

### 2.7 Agent Orchestrator (agent.py)

| ID | Requirement | Priority | Source |
|----|-------------|----------|--------|
| REQ-AGT-01 | Prepare inputs for `arcrun.run()`: model, tools, system, task, hooks | P0 | Brainstorm D7 |
| REQ-AGT-02 | Load LLM model via `arcllm` from config | P0 | Brainstorm D7 |
| REQ-AGT-03 | Bridge: `on_event` → Module Bus, `transform_context` → Context Manager | P0 | Brainstorm D7 |
| REQ-AGT-04 | Process `LoopResult` after run: emit post events, update memory | P0 | Brainstorm D7 |
| REQ-AGT-05 | No second loop — ArcRun IS the loop | P0 | Brainstorm D7 |
| REQ-AGT-06 | Initialize all components in dependency order | P0 | Steering |
| REQ-AGT-07 | Graceful shutdown with reverse-order teardown | P0 | Research |

---

## 3. Non-Functional Requirements

| ID | Requirement | Target |
|----|-------------|--------|
| NFR-01 | Core LOC | < 3,000 lines total across 7 files |
| NFR-02 | Cold start | < 500ms to ready state |
| NFR-03 | Memory baseline | < 50MB per agent |
| NFR-04 | Type safety | `mypy --strict` passes |
| NFR-05 | No global state | Config via dependency injection |
| NFR-06 | Async-first | All I/O operations use asyncio |
| NFR-07 | Python 3.12+ | Minimum supported version |

---

## 4. Acceptance Criteria

### 4.1 Integration Test: End-to-End Agent Run

```
Given: Valid arcagent.toml with LLM config and native tools
When: Agent orchestrator starts and receives a task message
Then:
  - Config loads and validates
  - Identity keypair loads or generates
  - Telemetry session span opens
  - Module Bus emits agent:init
  - Context Manager assembles system prompt
  - Tool Registry produces arcrun.Tool list
  - arcrun.run() executes with bridge hooks
  - Module Bus emits pre/post events during run
  - LoopResult processed, post events emitted
  - Telemetry session span closes
  - All components shut down cleanly
```

### 4.2 Unit Test Coverage Targets

| Component | Coverage Target |
|-----------|----------------|
| config.py | >= 90% |
| identity.py | >= 90% |
| telemetry.py | >= 80% |
| module_bus.py | >= 90% |
| tool_registry.py | >= 85% |
| context_manager.py | >= 85% |
| agent.py | >= 80% |

---

## 5. Constraints

- ArcLLM and ArcRun are external dependencies — use via interface only, never modify
- TOML config format (not YAML) for consistency with sibling projects
- File `agent.py` (not `loop.py`) — it's an orchestrator
- No vault integration in Phase 1 — file-based keys only
- No module signing in Phase 1
- No mTLS in Phase 1
