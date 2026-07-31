# PRD: Multi-Agent UI Architecture

**Spec**: SPEC-016 | **Type**: Integration | **Date**: 2026-03-03

## Problem Statement

ArcUI currently runs embedded in a single agent process, wired to one LLM provider via `attach_llm()`. This design cannot support the core use case: monitoring and controlling teams of agents — multiple agents, multiple teams, multiple runs, and multiple LLM instances. The `--ui` flag on `arc agent serve` tightly couples the dashboard lifecycle to a single agent, making it impossible to observe fleet-wide behavior.

## Goals

1. **Standalone UI process** — `arc ui start` launches the dashboard independently of any agent
2. **Multi-agent connections** — Multiple agents connect to the UI simultaneously, each pushing telemetry from all 4 event layers (llm, run, agent, team)
3. **Full control plane** — Operators can steer, cancel, configure, and shut down agents from the UI (optional — CLI works standalone)
4. **Layer-level observability** — Monitor arcllm, arcrun, and arcagent as first-class entities with per-agent drill-down
5. **Clean migration** — Remove embedded mode entirely; `--ui` on agent means "connect to running UI server"

## Non-Goals

- NATS transport (future — UITransport Protocol enables this)
- Real-time collaborative editing
- Agent code deployment from UI
- Custom dashboard plugins (beyond configuration)

## User Stories

### US-1: Operator launches dashboard
As an operator, I run `arc ui start` to launch the dashboard. It starts on port 8420, prints viewer and operator tokens, and waits for agents to connect.

### US-2: Agent connects to UI
As an agent (configured with `[ui]` in TOML), I connect to the UI's `/api/agent/connect` WebSocket on startup. I authenticate with my agent token, register my identity and capabilities, and begin streaming events.

### US-3: Operator monitors fleet
As an operator viewing the dashboard, I see all connected agents in a sidebar. I can click any agent to filter events. I can switch between layer tabs (All/LLM/Run/Agent/Team) to focus on specific telemetry.

### US-4: Operator sends control command
As an operator, I click "Cancel Run" on an agent. The UI sends a control message to that agent's WebSocket. The agent receives it, cancels the current run, and sends a confirmation event.

### US-5: Agent disconnects and reconnects
An agent loses network connectivity. It buffers events locally (bounded deque, 1000 items) and reconnects with exponential backoff (1s → 60s cap). On reconnect, it flushes buffered events and re-registers.

### US-6: Browser filters events
A browser client sends a subscribe message specifying which agents and layers to receive. The UI server filters events server-side, reducing bandwidth for browsers monitoring subsets of a large fleet.

### US-7: CLI-only operation
An operator runs agents without the UI. Everything works exactly as before. The UIReporter module is opt-in via `[ui]` config — no overhead when disabled.

## Requirements

### Functional

| ID | Requirement | Decision Ref |
|----|-------------|--------------|
| FR-1 | UI starts as standalone process via `arc ui start` CLI command | A-6 |
| FR-2 | Agents connect via WebSocket at `/api/agent/connect` | API-1 |
| FR-3 | Agent registration includes identity, capabilities, model, provider, team, tools, modules | D-2 |
| FR-4 | Events use flat UIEvent envelope: layer, event_type, agent_id, agent_name, source_id, timestamp, data, sequence | D-1 |
| FR-5 | 4 event layers: llm, run, agent, team | A-4 |
| FR-6 | Control messages: steer, cancel, config, ping, shutdown with request_id correlation | D-3 |
| FR-7 | Three token types: viewer (read), operator (read + control), agent (connect + stream) | API-3 |
| FR-8 | Browser REST endpoints for agent list, agent detail, control proxy | API-2 |
| FR-9 | Server-side subscription filters for browser clients | A-9 |
| FR-10 | Per-agent sub-aggregators + global rollup | O-1 |
| FR-11 | Agent reconnect with exponential backoff and local event buffer | A-2 |
| FR-12 | UIReporter module in arcagent, opt-in via config | A-5 |
| FR-13 | Same WebSocket for events and control (bidirectional) | A-7 |
| FR-14 | In-memory agent registry (no persistence) | A-8 |
| FR-15 | `--ui` on `arc agent serve` means "connect to UI server" (not "embed UI") | DEP-1 |
| FR-16 | Agent sidebar + layer tabs (All/LLM/Run/Agent/Team) in dashboard | UX-1 |

### Non-Functional

| ID | Requirement | Decision Ref |
|----|-------------|--------------|
| NFR-1 | Max 100 concurrent agent connections (configurable) | P-1 |
| NFR-2 | ~60KB memory per agent connection | P-1 |
| NFR-3 | 429 response when connection limit exceeded | P-1 |
| NFR-4 | 5-minute idle timeout with heartbeat keepalive | S-AUTO-3 |
| NFR-5 | All endpoints rate-limited | API-AUTO-1 |
| NFR-6 | UITransport Protocol abstraction for future NATS | E-1 |

### Security

| ID | Requirement | Mandate |
|----|-------------|---------|
| SR-1 | TLS 1.2+ on agent connections (Federal: mTLS) | NIST 800-52r2, SC-8 |
| SR-2 | All connection lifecycle events audited | NIST AU-2 |
| SR-3 | Agent identity verified on connect | NIST IA-2/IA-8 |
| SR-4 | No secrets in event payloads | NIST IA-5 |
| SR-5 | Control commands require operator role | NIST AC-3 |
| SR-6 | Registration, control, auth failures audited | NIST AU-2 |
| SR-7 | Tamper-evident UI audit log | NIST AU-9 |
| SR-8 | Shared agent_token with server-side binding (Federal: DID challenge) | S-1 |

### Tier Behavior

| Concern | Federal | Enterprise | Personal |
|---------|---------|------------|----------|
| Agent auth | DID + signed challenge | Token | Token |
| Transport | mTLS required | TLS | Optional (localhost) |
| Audit | Full (connection, control, subscription) | Connection + control | None |
| Control signing | Required | Optional | None |
| Workspace paths | Redacted in registration | Included | Included |

## Acceptance Criteria

1. `arc ui start` launches standalone dashboard on configurable port
2. At least 2 agents can connect simultaneously and stream events
3. Browser shows real-time events from all connected agents with layer filtering
4. Control commands (cancel, config) reach target agent and produce responses
5. Agent disconnect + reconnect preserves event continuity
6. Existing `arc agent serve` works without `--ui` (no regression)
7. Unit tests cover UIReporter, agent registry, subscription filters, control proxy
8. Integration tests with InMemoryTransport cover full agent→UI→browser flow
