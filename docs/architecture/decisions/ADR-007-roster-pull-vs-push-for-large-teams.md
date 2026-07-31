# ADR-007: Roster Pull-vs-Push Strategy for Large Teams

**Status**: Accepted (Phase 1 pull model)
**Date**: 2026-03-01
**Decision Makers**: Josh Schultz
**Relates to**: SPEC-013 Convention-Driven Prompt Injection, ADR-006 Assemble Prompt Contract

---

## Context

The messaging module builds a team roster by pulling all entities from `EntityRegistry.list_entities()` on every prompt assembly (with TTL caching). This works for small teams but raises scaling concerns:

1. **Roster size grows linearly** with team size. At 50 entities with 5 fields each, the XML roster consumes ~2,000 tokens — meaningful pressure on context budgets.
2. **Pull model polls the registry** every TTL interval regardless of whether the roster changed.
3. **No relevance filtering** — every entity appears in every agent's roster, even when agents only collaborate with a subset.

The architecture review for SPEC-013 identified this as a design decision requiring documentation.

## Decision

**Accept the pull model with TTL caching for Phase 1.** The current implementation is correct for teams of 1–30 agents. Document the crossover threshold and migration path to a push model for Phase 2.

### Phase 1: Pull Model (Current)

```
Agent starts → EntityRegistry.list_entities() → Build XML roster → Cache for TTL
                                                                       ↓
Prompt assembly → Return cached roster (or rebuild if TTL expired)
```

- **TTL default**: 60 seconds (configurable via `roster_ttl_seconds`)
- **Cache scope**: Per-agent instance (no shared cache across agents)
- **Invalidation**: TTL expiry only

### Phase 2: Push Model (Future)

```
EntityRegistry → emits "entity:registered" / "entity:status_changed" events
                                    ↓
MessagingModule → Subscribes → Invalidates roster cache on change
                                    ↓
Prompt assembly → Rebuild only when roster actually changed
```

Migration path:
1. Add `entity:registered` and `entity:status_changed` events to arcteam's EntityRegistry.
2. Subscribe in MessagingModule alongside the existing TTL fallback.
3. On event receipt, set `_roster_cache = None` to trigger rebuild on next prompt assembly.
4. Keep TTL as a safety net (prevents stale roster if events are lost).
5. Once push is stable, increase TTL to 5 minutes (reduces polling for unchanged rosters).

### Phase 3: Filtered Roster (Future)

For teams >50 agents, inject only relevant entities:

1. Filter by shared channels or recent communication history.
2. Add a `roster_max_entities` config to cap roster size.
3. Use an LRU strategy: most-recently-communicated-with entities appear first.
4. Add a `<roster-summary count="150" shown="20">` element so the agent knows the full team size.

## Alternatives Considered

### 1. Push model now

Implement event-driven roster invalidation immediately. Rejected because:
- Requires changes to arcteam's EntityRegistry (cross-project dependency)
- Phase 1 teams are <10 agents — no performance pressure
- TTL caching already prevents per-prompt I/O

### 2. Shared roster cache (Redis/NATS KV)

Store the roster in a shared cache that all agents read. Rejected because:
- Adds infrastructure dependency for a problem that doesn't exist yet
- Shared-nothing architecture is a core principle (CLAUDE.md)
- Per-agent caching is simpler and sufficient at current scale

### 3. No roster (discover via tools only)

Remove the roster from the prompt and let agents discover teammates via the `messaging_entities` tool. Rejected because:
- Agents need roster context to know *who* to message without a tool call
- Removing the roster degrades agent autonomy — agents can't reason about team composition without explicit tool use
- Tool calls cost tokens and latency; prompt context is free once injected

## Rationale

1. **Right-size for Phase 1.** Teams of <30 agents produce rosters under 1,000 tokens. TTL caching ensures at most one registry read per 60 seconds. No performance concern.
2. **Clear migration path.** The push model is a straightforward enhancement that doesn't change the roster format or prompt structure — only the invalidation trigger.
3. **No premature optimization.** Building push infrastructure for a problem that manifests at >50 agents when current deployments are <10 violates YAGNI.
4. **Arcteam coordination.** The push model requires arcteam to emit entity lifecycle events. This is a natural evolution of arcteam and should be planned as part of its Phase 2 roadmap.

## Crossover Thresholds

| Team Size | Roster Tokens | Strategy | TTL |
|-----------|--------------|----------|-----|
| 1–30 | <1,000 | Pull + TTL | 60s |
| 30–50 | 1,000–2,000 | Pull + TTL (increase TTL) | 120s |
| 50–100 | 2,000–4,000 | Push + filtered roster | 300s |
| 100+ | >4,000 | Push + LRU filtered + summary | 300s |

## When to Revisit

- **Team size exceeds 30**: Increase TTL and monitor prompt token usage.
- **Team size exceeds 50**: Implement push model (Phase 2) and roster filtering (Phase 3).
- **Arcteam adds entity lifecycle events**: Migrate to push invalidation.
- **Prompt token budget enforcement**: If a token budget guard is added (per ADR-006), roster may need to be truncated or summarized to fit.

## Consequences

### Positive
- Simple, correct implementation for Phase 1 scale
- No cross-project dependencies on arcteam changes
- Clear documented path for scaling
- TTL is configurable per-deployment for tuning

### Negative
- Roster may be up to TTL seconds stale after entity changes
- No change-driven invalidation — polling continues even when roster is static
- Full roster included for all agents regardless of relevance
- Requires future work when team sizes grow

### Mitigations
- TTL default of 60s limits staleness to 1 minute
- Audit event `prompt.roster_rebuilt` enables monitoring of rebuild frequency
- Entity status changes (online/offline) propagate within TTL window
