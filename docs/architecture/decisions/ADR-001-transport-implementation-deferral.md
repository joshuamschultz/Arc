# ADR-001: MCP/HTTP/Process Transport Implementation Deferred

**Status**: Accepted
**Date**: 2026-02-14
**Decision Makers**: Josh Schultz
**Relates to**: S001 Phase 1 Core Components

---

## Context

The Tool Registry design (SDD Section 2.5) specifies four transport types:

| Transport | Purpose | Phase 1 Status |
|-----------|---------|----------------|
| **Native** | Python function tools (in-process) | Implemented |
| **MCP** | Model Context Protocol servers (subprocess) | Deferred |
| **HTTP** | Remote HTTP-based tools | Deferred |
| **Process** | Subprocess-based tools | Deferred |

Phase 1 focuses on the nucleus: config, identity, telemetry, module bus, tool registry core, context manager, and orchestrator. The tool registry defines the `ToolTransport` enum and `RegisteredTool` dataclass for all four transports, but only the native transport has working registration and execution paths.

## Decision

**Defer MCP, HTTP, and Process transport implementations to Phase 2.** The registry's architecture (enum, dataclass, policy enforcement, wrapping) is transport-agnostic and ready. Only the transport-specific registration methods (`discover_mcp_tools`, `register_http_tools`, `register_process_tools`) are stubs.

## Rationale

1. **Native tools prove the architecture.** All registry concerns (policy, wrapping, audit, timeout, veto) are exercised through native tools. The wrapping layer is transport-agnostic.

2. **MCP requires external process management.** The MCP SDK (`mcp` v1.26.0) uses `stdio_client` context managers that need lifecycle management, reconnection logic, and error handling. This is a distinct scope.

3. **HTTP requires connection pooling.** `httpx.AsyncClient` lifecycle, retry policies, and auth header management are separate concerns.

4. **Process requires sandboxing.** Subprocess tools in a federal environment need sandboxing, resource limits, and output parsing — all Phase 2+ work.

5. **No Phase 1 consumers.** The built-in tools (read, write, edit, bash) are all native. No MCP/HTTP/process tools are configured in the example config.

## Consequences

### Positive
- Phase 1 stays under 3,000 LOC budget
- Core registry architecture is proven and tested
- Each transport can be implemented independently in Phase 2

### Negative
- Cannot use MCP tools until Phase 2
- `ToolTransport` enum has values without corresponding registration paths
- Config models (`MCPServerEntry`, `HTTPToolEntry`, `ProcessToolEntry`) exist but aren't exercised

### Mitigations
- Config models are tested via Pydantic validation
- Enum values are tested
- Phase 2 transport work is tracked in tech-debt.json

## Phase 2 Implementation Order

1. **MCP** (highest value — largest tool ecosystem)
2. **HTTP** (remote tool APIs)
3. **Process** (subprocess sandboxing — requires security review)
