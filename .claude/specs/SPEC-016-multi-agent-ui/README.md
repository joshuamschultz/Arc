# SPEC-016: Multi-Agent UI Architecture

**Status**: approved
**Type**: integration
**Created**: 2026-03-03
**Feature**: multi-agent-ui-architecture

## Prior Work

- **Build decisions**: `.claude/decisions-log.md` — "Multi-Agent UI Architecture" section (24 decisions: 15 user, 9 auto-applied)
- **Prior build**: SPEC-015 arcui-llm-telemetry (2026-03-01) — single-agent UI foundation

## Summary

Evolve ArcUI from an embedded single-agent dashboard to a standalone multi-agent control plane. The UI runs as its own process (`arc ui start`), agents connect via WebSocket (`/api/agent/connect`), push events from 4 layers (llm, run, agent, team), and receive control commands on the same connection. A UIReporter module in arcagent bridges internal events to the UI. Clean break from embedded mode.

## Key Decisions

| Decision | Choice |
|----------|--------|
| Topology | Standalone process |
| Transport | WebSocket (UITransport Protocol for future NATS) |
| Event layers | 4: llm, run, agent, team |
| Auth model | 3 tokens: viewer, operator, agent |
| Agent discovery | Explicit TOML config `[ui]` section |
| Control plane | Bidirectional on same WebSocket |
| Aggregation | Per-agent sub-aggregators + global rollup |
| Migration | Clean break — remove embedded mode |

## Packages Affected

| Package | Changes |
|---------|---------|
| **arcui** | Agent WS endpoint, agent registry, subscription filters, control proxy, multi-agent aggregation |
| **arcagent** | New UIReporter module at `modules/ui_reporter/` |
| **arccli** | New `arc ui start` command, `--ui` flag means "connect to UI" |

## Packages NOT Changed

| Package | Reason |
|---------|--------|
| arcllm | Events flow via existing on_event callback |
| arcrun | Events flow via existing ModuleBus bridge |
| arcteam | Agents relay team messages — no direct arcui dependency |

## Files

- `PRD.md` — Product requirements
- `SDD.md` — System design document
- `PLAN.md` — Implementation plan

## Learnings

(Captured during implementation)
