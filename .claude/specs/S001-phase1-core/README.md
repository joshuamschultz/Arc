# S001: Phase 1 Core Components

## Metadata

| Field | Value |
|-------|-------|
| **Spec ID** | S001 |
| **Feature** | Phase 1 Core Components (Nucleus) |
| **Type** | Integration |
| **Status** | PENDING |
| **Created** | 2026-02-14 |
| **Author** | Claude (spec-driven workflow) |
| **Confidence** | 95% (fast-track) |
| **Prior Work** | `.claude/brainstorms/2026-02-14-phase1-technical-approach.md` (deepened) |

## Scope

7 core nucleus components that form ArcAgent's foundation:

1. **config.py** — TOML parser + Pydantic validation
2. **identity.py** — DID, Ed25519 keypair, file-based keys (Phase 1)
3. **telemetry.py** — OTel parent spans building on ArcLLM's SDK
4. **module_bus.py** — Async event system with bridge to ArcRun EventBus
5. **tool_registry.py** — Registration, discovery, policy, wraps to arcrun.Tool
6. **context_manager.py** — System prompt assembly, token monitoring, compaction
7. **agent.py** — Orchestrator (wires components, invokes ArcRun)

## NOT in Scope

- Markdown Memory module (separate spec)
- CLI commands (separate spec)
- Container isolation (separate spec)
- Vault integration (Phase 2)
- mTLS (Phase 2)
- PKI identity with challenge-response (Phase 2)
- Policy engine (Phase 2)
- Module signing (Phase 2)
- NATS inter-agent messaging (Phase 3)

## Key Decisions

All decisions from brainstorm session (2026-02-14):

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Module Bus bridges ArcRun EventBus via `on_event` callback | Clean separation, ArcRun stays generic |
| 2 | All handlers async-only | Simple contract, no sync/async detection |
| 3 | EventContext.veto(reason) for interception | Auditable, explicit, all handlers see state |
| 4 | Integer priority ordering (10=policy, 100=default, 200=logging) | Clear, simple, extensible |
| 5 | Hybrid token counting (client estimate + provider reported) | Proactive pruning + accurate tracking |
| 6 | ArcAgent owns Tool Registry, ArcRun receives tools | Clear ownership, audit wrapping |
| 7 | One loop (ArcRun), ArcAgent orchestrates | No duplication of ArcRun capabilities |
| 8 | All 4 tool transports (native, MCP, HTTP, process) | Full coverage from Phase 1 |
| 9 | TOML config (not YAML) | Consistent with ArcLLM/ArcRun siblings |
| 10 | OTel telemetry building on ArcLLM's SDK | Parent spans, auto-nesting |
| 11 | `loop.py` renamed to `agent.py` | It's an orchestrator, not a loop |

## Steering Doc Corrections

The brainstorm resolved several inconsistencies with steering docs:

| Steering Doc Says | Brainstorm Corrected To | Reason |
|-------------------|------------------------|--------|
| Config: YAML | Config: TOML | Consistency with ArcLLM/ArcRun |
| Core file: `loop.py` | Core file: `agent.py` | Orchestrator, not loop |
| ArcRun has Tool Registry | ArcRun receives tools only | ArcAgent owns registration |

## Learnings

_(Updated during implementation)_

## Files

- [PRD.md](./PRD.md) — Product Requirements Document
- [SDD.md](./SDD.md) — System Design Document
- [PLAN.md](./PLAN.md) — Implementation Plan
