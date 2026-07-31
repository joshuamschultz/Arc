# Specification: Arcui Reality Mirror

**Feature:** `arcui-reality-mirror`
**Created:** 2026-07-10

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | approved | 2026-07-10 |
| SDD | approved | 2026-07-10 |
| PLAN | approved | 2026-07-10 |
## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md)
- Tech: [`../../steering/tech.md`](../../steering/tech.md)
- Structure: [`../../steering/structure.md`](../../steering/structure.md)
- Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Decision Log Snippets

Cross-feature decisions referenced from [`../../decisions-log.md`](../../decisions-log.md):

_(none yet — link decisions as `[D-NNN](../../decisions-log.md#d-nnn)` when they apply to this feature)_

## Phase Notes

### Phase 1: Foundation

_Recorded 2026-07-10_

All three producer seams landed (commits 6612611, 0833d75, 9d7d561). Surprises: (1) the capability loader could not report per-item verdicts — additive CapabilityOutcome recording at decision sites was required, better than the planned consume-only seam; (2) arccli could not be reused for messaging-service construction (arccli imports arcui — cycle); arcteam-direct construction in arcui/messaging.py with a monkeypatchable backend factory avoided the NATS test swamp; (3) memory facade needed small store-API additions inside arcmemory (episodic update/delete, graph neighbor reads) — anticipated by the plan's risk note; (4) federal vault_transit signer custody for UI mutations deferred to a shared lower-layer factory (flagged in 9d7d561, matters from T-710 on at federal tier only).

## Learnings

Feature-specific insights captured here. Global / reusable patterns go to memory via `/memorize`.

_(none yet)_

## Open Questions

_(none yet)_
