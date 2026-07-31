# SPEC-007: DID Identity Unification

## Metadata

| Field | Value |
|-------|-------|
| **ID** | SPEC-007 |
| **Name** | DID Identity Unification |
| **Type** | Integration |
| **Status** | Draft |
| **Created** | 2026-02-17 |
| **Author** | Josh / my_agent |
| **Priority** | High |

## Summary

Unify the two separate identity systems — arcagent's DID (`did:arc:{org}:{type}/{hash}`) and arcteam's URI (`agent://{name}`) — into a single DID-based identity used everywhere: messaging, entity registration, audit trails, stream addressing, and tool calls.

## Prior Work

- **SPEC-006**: ArcTeam Messaging Subsystem (defines current URI scheme)
- **Brainstorm**: `.claude/brainstorms/2026-02-17-arcteam-messaging.md` (competitive analysis)
- **Identity module**: `arcagent/core/identity.py` (DID implementation)
- **Types module**: `arcteam/src/arcteam/types.py` (URI scheme implementation)

## Decision Log

| ID | Decision | Rationale |
|----|----------|-----------|
| D-001 | DID is the canonical identity, URIs become aliases | One identity system, cryptographically verifiable |
| D-002 | Entity registry maps name→DID for human-friendly lookup | Operators still use names, system resolves to DID |
| D-003 | Stream names derived from DID hash, not full DID string | Filesystem safety, NATS compatibility |
| D-004 | Backward-compatible migration with alias resolution | Existing agent:// references resolve to DID |

## Learnings

_(To be populated during implementation)_
