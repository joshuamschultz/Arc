# ADR-018: SPEC-018 excludes MCP client, migration tooling, and ACP adapter

**Status**: Accepted for migration tooling and ACP; the MCP-client exclusion is superseded by ADR-030
**Date**: 2026-04-18
**Spec**: SPEC-018 (Hermes Parity Roadmap)

## Context

During `/build` and `/deepen` for SPEC-018 the following candidates were considered for inclusion in M1 and explicitly cut:

1. MCP client (arcgateway or arcagent as MCP host/client for IDE integrations)
2. Migration tooling (`arc migrate hermes`, `arc migrate claw`)
3. ACP/IDE adapter (Agent Context Protocol adapter for IDE sessions)

## Decision

SPEC-018 ships with **none** of these. They are not deferred to M2/M3 — they are out of scope unless the trigger conditions below change.

## Rationale

### MCP client

- Users adopt Arc fresh, not bridging from MCP-enabled tools
- arcgateway's `AdapterBase` Protocol already accepts arbitrary platform adapters — community can write MCP as an adapter without core changes
- Adding MCP host support grows the protocol surface we maintain; no demonstrated demand from federal/enterprise pilots

### Migration tooling

- Hermes and Claw users are zero in our current audience; writing automated migration for zero users is pure YAGNI
- Config schema is documented (`GatewayConfig` in `arcgateway/config.py`); manual port is ~15 minutes for the few users who need it
- Migration tools are permanent maintenance burden for one-time value

### ACP adapter

- CLI (`arccli`), TUI (`arctui`, in progress), and Web (`arcui`) cover dev UX
- ACP still evolving; taking on protocol-version churn before demand is visible violates simplicity pillar
- Community can implement as `AdapterBase` subclass if desired

## Consequences

**Positive**
- M1 scope = one new sibling package (`arcgateway`) + module additions. Small enough to deliver in one sprint of parallel agent work.
- No speculative protocol surface in the trust boundary.
- Community extensions stay first-class citizens.

**Negative**
- Users migrating from Hermes config must hand-translate YAML → TOML (10-line config).
- Users who want MCP integration must implement it themselves or wait for community.
- ACP-native IDE experience not available.

## Reconsider when

- **MCP**: reconsidered and reversed on 2026-08-04 — see ADR-030. The trigger that fired was not customer demand but the discovery that every maintained integration for the ten target services is an MCP server, so "the community can write an adapter" resolved to "adopt MCP" in practice.
- **Migration**: a significant population of Hermes users is blocked and config delta makes automation materially better than manual
- **ACP**: two or more enterprise customers explicitly request ACP/IDE integration AND the use case is not covered by arccli/arctui/arcui

In every case: explicit demand, not anticipated demand.
