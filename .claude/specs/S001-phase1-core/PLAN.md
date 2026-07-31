# PLAN: Phase 1 Core Components

**Spec ID**: S001
**Status**: COMPLETE
**Last Updated**: 2026-02-14

---

## Overview

7 core nucleus components implemented in 4 phases following dependency order. TDD throughout. Target: <3,000 LOC total.

## Phases

### Phase 1: Foundation (config + errors)
### Phase 2: Infrastructure (telemetry + identity + module_bus)
### Phase 3: Capabilities (tool_registry + context_manager)
### Phase 4: Orchestration (agent.py + integration)

---

## Phase 1: Foundation

> No dependencies. Config is the base everything else builds on.

- [x] **T1.1** Create project scaffolding `[activity: core-development]`
  - [x] T1.1.1 Create `arcagent/core/__init__.py` with package exports
  - [x] T1.1.2 Create `arcagent/__init__.py` with top-level exports
  - [x] T1.1.3 Create `tests/unit/core/` directory structure
  - [x] T1.1.4 Create `arcagent.toml.example` with all config sections
  - _Requirements: NFR-01 (project structure)_

- [x] **T1.2** Implement error hierarchy `[activity: core-development]`
  - [x] T1.2.1 Write tests for error classes (ArcAgentError, ConfigError, IdentityError, ToolError, ToolVetoedError, ContextError, ModuleBusError)
  - [x] T1.2.2 Implement `arcagent/core/errors.py` with error hierarchy
  - [x] T1.2.3 Verify: `pytest tests/unit/core/test_errors.py`
  - _Requirements: SDD Section 4.1_

- [x] **T1.3** Implement config parser `[activity: core-development]`
  - [x] T1.3.1 Write tests for TOML parsing: valid config, missing file, syntax errors with line numbers
  - [x] T1.3.2 Write tests for Pydantic validation: missing required fields, invalid types, field path in errors
  - [x] T1.3.3 Write tests for env var overrides: `ARCAGENT_LLM__MODEL`, `ARCAGENT_AGENT__NAME`
  - [x] T1.3.4 Write tests for defaults: all optional sections have sensible defaults
  - [x] T1.3.5 Implement Pydantic models (ArcAgentConfig and nested models)
  - [x] T1.3.6 Implement `load_config()` with two-phase error handling
  - [x] T1.3.7 Verify: `pytest tests/unit/core/test_config.py` (target >=90%)
  - [x] T1.3.8 Verify: `mypy arcagent/core/config.py --strict`
  - _Requirements: REQ-CFG-01 through REQ-CFG-05_
  - _Design: SDD Section 2.1_

**Phase 1 gate:** Config loads, validates, overrides work. `mypy` and `ruff` clean.

**Completion: 3/3 tasks | Remaining: 0**

---

## Phase 2: Infrastructure `[blocked-by: T1.2, T1.3]`

> Telemetry, identity, and module bus. These have minimal cross-dependencies.

- [x] **T2.1** Implement telemetry `[activity: core-development]` `[parallel: true]`
  - [x] T2.1.1 Write tests for span creation: session, turn, tool spans
  - [x] T2.1.2 Write tests for span nesting: tool span nests under turn, turn under session
  - [x] T2.1.3 Write tests for audit_event: structured log output, span event creation
  - [x] T2.1.4 Write tests for disabled telemetry (config.telemetry.enabled=False)
  - [x] T2.1.5 Implement `AgentTelemetry` class with OTel tracer + structured logger
  - [x] T2.1.6 Implement context managers: `session_span`, `turn_span`, `tool_span`
  - [x] T2.1.7 Implement `audit_event` with dual output (log + span event)
  - [x] T2.1.8 Verify: `pytest tests/unit/core/test_telemetry.py` (target >=80%)
  - _Requirements: REQ-TEL-01 through REQ-TEL-05_
  - _Design: SDD Section 2.3_

- [x] **T2.2** Implement identity `[activity: core-development]` `[parallel: true]`
  - [x] T2.2.1 Write tests for keypair generation: Ed25519 via PyNaCl
  - [x] T2.2.2 Write tests for DID creation: format `did:arc:{org}:{type}/{id}`
  - [x] T2.2.3 Write tests for sign/verify: roundtrip, invalid signature, wrong key
  - [x] T2.2.4 Write tests for file-based key storage: save, load, permissions
  - [x] T2.2.5 Write tests for vault fallback: vault hit, vault miss → file, no vault → file
  - [x] T2.2.6 Write tests for `from_config`: auto-generate when no keys exist
  - [x] T2.2.7 Implement `AgentIdentity` dataclass with generate/from_config classmethods
  - [x] T2.2.8 Implement `_load_signing_key` with vault → file fallback (reuses ArcLLM VaultResolver)
  - [x] T2.2.9 Implement file key storage with proper permissions (0600/0700)
  - [x] T2.2.10 Verify: `pytest tests/unit/core/test_identity.py` (target >=90%)
  - _Requirements: REQ-IDN-01 through REQ-IDN-07_
  - _Design: SDD Section 2.2_

- [x] **T2.3** Implement module bus `[activity: module-development]` `[parallel: true]`
  - [x] T2.3.1 Write tests for EventContext: veto, multiple vetos (first wins), is_vetoed state
  - [x] T2.3.2 Write tests for subscribe: register handler with priority, duplicate handlers
  - [x] T2.3.3 Write tests for emit: priority ordering (10 before 100 before 200)
  - [x] T2.3.4 Write tests for concurrent execution: same-priority handlers run concurrently
  - [x] T2.3.5 Write tests for error isolation: one handler exception doesn't crash others
  - [x] T2.3.6 Write tests for handler timeout: 30s default, handler exceeds timeout
  - [x] T2.3.7 Write tests for veto flow: pre_tool vetoed → post_tool not called
  - [x] T2.3.8 Write tests for module lifecycle: startup order, shutdown reverse order
  - [x] T2.3.9 Implement `EventContext` dataclass
  - [x] T2.3.10 Implement `ModuleBus.subscribe()` and `ModuleBus.emit()`
  - [x] T2.3.11 Implement handler wrapping with `asyncio.wait_for` timeout
  - [x] T2.3.12 Implement module lifecycle: `startup()` / `shutdown()` with reverse ordering
  - [x] T2.3.13 Implement `Module` protocol
  - [x] T2.3.14 Verify: `pytest tests/unit/core/test_module_bus.py` (target >=90%)
  - _Requirements: REQ-BUS-01 through REQ-BUS-10_
  - _Design: SDD Section 2.4_

**Phase 2 gate:** Telemetry spans nest, identity signs/verifies, bus dispatches with priority + veto. All parallel tasks complete. `mypy` and `ruff` clean.

**Completion: 3/3 tasks | Remaining: 0**

---

## Phase 3: Capabilities `[blocked-by: T2.1, T2.3]`

> Tool registry and context manager. Both need bus and telemetry.

- [x] **T3.1** Implement tool registry `[activity: core-development]` `[parallel: true]`
  - [x] T3.1.1 Write tests for native tool registration: import function, register, execute
  - [x] T3.1.2 Write tests for MCP tool discovery: mock MCP server, list_tools, call_tool
  - [x] T3.1.3 Write tests for HTTP tool registration: httpx call, timeout handling
  - [x] T3.1.4 Write tests for process tool registration: subprocess execution, timeout
  - [x] T3.1.5 Write tests for policy enforcement: allowlist, denylist, denied tool blocked
  - [x] T3.1.6 Write tests for tool wrapping: pre_tool event emitted, veto blocks execution, post_tool event emitted, audit logged
  - [x] T3.1.7 Write tests for `to_arcrun_tools()`: returns correct arcrun.Tool format
  - [x] T3.1.8 Write tests for timeout enforcement: tool exceeds timeout
  - [x] T3.1.9 Implement `RegisteredTool` dataclass and `ToolTransport` enum
  - [x] T3.1.10 Implement native tool registration (dynamic import from module path)
  - [x] T3.1.11 Implement MCP tool discovery using `mcp` SDK (stdio_client → ClientSession → list_tools)
  - [x] T3.1.12 Implement HTTP tool registration with httpx
  - [x] T3.1.13 Implement process tool registration with asyncio.subprocess
  - [x] T3.1.14 Implement policy check (allow/deny) on registration
  - [x] T3.1.15 Implement `_wrapped_execute` with bus events + telemetry + timeout
  - [x] T3.1.16 Implement `to_arcrun_tools()` conversion
  - [x] T3.1.17 Implement `shutdown()` for closing MCP connections and httpx clients
  - [x] T3.1.18 Verify: `pytest tests/unit/core/test_tool_registry.py` (target >=85%)
  - _Requirements: REQ-TRG-01 through REQ-TRG-09_
  - _Design: SDD Section 2.5_

- [x] **T3.2** Implement context manager `[activity: core-development]` `[parallel: true]`
  - [x] T3.2.1 Write tests for system prompt assembly: identity.md + policy.md + context.md concatenation
  - [x] T3.2.2 Write tests for missing workspace files: graceful handling when files don't exist
  - [x] T3.2.3 Write tests for token estimation: character-based heuristic, 1.1x multiplier
  - [x] T3.2.4 Write tests for reported usage tracking: update from LLMResponse.usage
  - [x] T3.2.5 Write tests for observation masking: old tool outputs replaced, recent protected
  - [x] T3.2.6 Write tests for threshold triggers: prune at 70%, compact at 85%, emergency at 95%
  - [x] T3.2.7 Write tests for transform_context callback: messages modified correctly
  - [x] T3.2.8 Implement `ContextManager.__init__` with config
  - [x] T3.2.9 Implement `assemble_system_prompt` (read and concatenate workspace files)
  - [x] T3.2.10 Implement `estimate_tokens` with conservative multiplier
  - [x] T3.2.11 Implement `update_reported_usage`
  - [x] T3.2.12 Implement `_prune_observations` (observation masking)
  - [x] T3.2.13 Implement `transform_context` callback
  - [x] T3.2.14 Implement `compact` (LLM-based summarization stub — full implementation with real LLM in Phase 4 integration)
  - [x] T3.2.15 Verify: `pytest tests/unit/core/test_context_manager.py` (target >=85%)
  - _Requirements: REQ-CTX-01 through REQ-CTX-07_
  - _Design: SDD Section 2.6_

**Phase 3 gate:** All 4 tool transports register and execute, policy blocks denied tools, context manager prunes and assembles prompts. `mypy` and `ruff` clean.

**Completion: 2/2 tasks | Remaining: 0**

---

## Phase 4: Orchestration `[blocked-by: T2.2, T3.1, T3.2]`

> Wire everything together. Integration tests.

- [x] **T4.1** Implement agent orchestrator `[activity: core-development]`
  - [x] T4.1.1 Write tests for startup sequence: components initialize in dependency order
  - [x] T4.1.2 Write tests for shutdown sequence: reverse order, all components cleaned up
  - [x] T4.1.3 Write tests for vault resolver creation: backend configured vs empty
  - [x] T4.1.4 Write tests for run: model loaded, tools prepared, arcrun.run called with correct args
  - [x] T4.1.5 Write tests for ArcRun bridge: ArcRun events mapped to Module Bus events
  - [x] T4.1.6 Write tests for error handling: component failure during startup, runtime error
  - [x] T4.1.7 Implement `ArcAgent.__init__` with config storage
  - [x] T4.1.8 Implement `startup()` with dependency-ordered initialization
  - [x] T4.1.9 Implement `run()` with arcllm.load_model + arcrun.run + result processing
  - [x] T4.1.10 Implement `create_arcrun_bridge()` event mapping function
  - [x] T4.1.11 Implement `shutdown()` with reverse-order teardown
  - [x] T4.1.12 Verify: `pytest tests/unit/core/test_agent.py` (target >=80%)
  - _Requirements: REQ-AGT-01 through REQ-AGT-07_
  - _Design: SDD Section 2.7_

- [x] **T4.2** Integration tests `[activity: integration-testing]` `[blocked-by: T4.1]`
  - [x] T4.2.1 Write integration test: config → identity → telemetry → bus → tools → context → agent startup
  - [x] T4.2.2 Write integration test: agent.run() with mock LLM and native tools
  - [x] T4.2.3 Write integration test: Module Bus event flow during tool execution
  - [x] T4.2.4 Write integration test: veto blocks tool execution end-to-end
  - [x] T4.2.5 Write integration test: context manager prunes during arcrun.run
  - [x] T4.2.6 Write integration test: graceful shutdown with reverse teardown
  - [x] T4.2.7 Verify: `pytest tests/integration/` passes
  - _Requirements: PRD Section 4.1 (End-to-End acceptance criteria)_

- [x] **T4.3** Quality gates `[activity: core-development]` `[blocked-by: T4.2]`
  - [x] T4.3.1 Verify: `cloc arcagent/core/` < 3,000 LOC (1,509 total / 1,155 code-only)
  - [x] T4.3.2 Verify: `pytest --cov=arcagent/core` >= 80% line coverage (94.58%)
  - [x] T4.3.3 Verify: `mypy arcagent/ --strict` passes with 0 errors
  - [x] T4.3.4 Verify: `ruff check .` passes with 0 errors
  - [x] T4.3.5 Verify: Per-component coverage targets met (PRD Section 4.2)
  - _Requirements: NFR-01 through NFR-07, Quality Gates_

**Phase 4 gate:** End-to-end agent run works with mock LLM. All quality gates pass. Core under 3,000 LOC.

**Completion: 3/3 tasks | Remaining: 0**

---

## Summary

| Phase | Tasks | Parallel | Dependencies |
|-------|-------|----------|--------------|
| 1: Foundation | 3 | No | None |
| 2: Infrastructure | 3 | Yes (T2.1, T2.2, T2.3) | Phase 1 |
| 3: Capabilities | 2 | Yes (T3.1, T3.2) | T2.1, T2.3 |
| 4: Orchestration | 3 | Partial (T4.2 after T4.1) | T2.2, T3.1, T3.2 |

**Total tasks: 11 | Total subtasks: 98**
**Completion: 11/11 tasks | Remaining: 0**
