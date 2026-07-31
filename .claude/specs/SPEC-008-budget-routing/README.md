# SPEC-008: Budget Control & Compliance-Aware Routing

| Field | Value |
|-------|-------|
| **ID** | SPEC-008 |
| **Feature** | Budget Control & Compliance-Aware Routing |
| **Type** | Integration |
| **Status** | COMPLETE |
| **Confidence** | 90% |
| **Route** | Fast-track |
| **Package** | `arcllm` |
| **Created** | 2026-02-21 |

## Prior Work

- **Build Decisions**: `.claude/decisions-log.md` (14 decisions, Feature: ArcLLM Budget Control & Compliance-Aware Routing)
- **Deepen**: Research Insights appended to decisions-log.md (codebase analysis, security edge cases, OTel conventions, NIST mapping)
- **Roadmap**: `packages/arcllm/.claude/roadmap.md` (Steps 12 + 26)

## Key Decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Budget-telemetry integration | Extend TelemetryModule (not separate module) |
| 2 | Budget scope | Per-agent ID scope |
| 3 | Budget storage | In-memory accumulator + OTel spans for durable persistence |
| 4 | Budget period | Calendar monthly + daily + per-call max |
| 5 | Enforcement | Configurable warn/block, default block |
| 6 | API surface | Extend telemetry kwarg + mandatory budget_scope |
| 7 | Router stack position | Replace adapter at innermost position |
| 8 | Classification source | Caller-declared via kwargs |
| 9 | Routing rules | Classification -> provider + model mapping |
| 10 | Adapter lifecycle | Eager loading at init |
| 11 | Budget-routing interaction | Budget tracks total spend only |
| 12 | Pre-flight estimate | max_tokens * output cost rate |
| 13 | Unknown classification | Follows enforcement config |
| 14 | Testing | Standard TDD + security-specific tests |

## Design Principles

- **Budget IS telemetry** — one module, one cost concern
- **Router IS the provider** — replaces adapter at innermost position
- **Enforcement is configurable** — enterprise (warn, open) vs federal (block, closed)
- **OTel is the durable store** — in-memory accumulators for enforcement, external collectors for audit
- **Per-agent isolation** — shared-nothing budget tracking, maps to DID identity
- **Fail closed in federal mode** — unknown classification, exceeded budget, missing scope all raise errors

## Learnings

### Implementation (Phase 1-5)
- Budget integrated cleanly into TelemetryModule (~200 LOC added) — ADR-1 validated
- RoutingModule at 120 LOC — well under 150 LOC target
- Float arithmetic for cost tracking works fine at enforcement scale (ADR-2 validated)
- `_budget_registry` pattern mirrors `_bucket_registry` — consistent codebase convention
- TDD cycle caught a test_config.py regression early when `[modules.budget]` was removed from config.toml

### Review Findings (4-agent swarm review)
- **Defensive copy**: `RoutingModule.__init__` copies adapters dict to prevent post-init mutation (AC-3 violation in federal context) — FIXED
- **Error resilience**: Router `close()` tolerates individual adapter failures via ExceptionGroup — FIXED
- **Complexity**: `_check_budget_pre_call` refactored to extract `_enforce_limit()` helper (DRY + complexity fix) — FIXED
- **Thread safety**: `_budget_registry` and `BudgetAccumulator` use `threading.Lock` with double-check locking for PEP 703 readiness — FIXED
- **Alert threshold**: Range validation (0 < pct <= 100) prevents nonsensical values — FIXED
- **Pre-flight default**: `default_max_tokens` sourced from config (injected from `[defaults].max_tokens` in registry) — FIXED
- **Classification enumeration**: Error messages no longer leak valid classification names; format validation rejects invalid input before lookup — FIXED
- **Integration tests**: `test_budget_telemetry.py` and `test_routing_stack.py` created (SDD deliverables) — FIXED
- **DRY in registry**: `_build_adapter()` helper extracted, eliminating duplicated vault/config/adapter construction — FIXED
- **Type safety**: Span parameters typed as `trace.Span` instead of `Any` in telemetry methods — FIXED
- **Classification validation**: RoutingModule validates classification format (lowercase alphanumeric + `_:.-`, max 128) before lookup — FIXED

### Patterns Established
- Budget fields colocate with telemetry config (not separate module section)
- `_enforce_limit()` extracted as reusable block-or-warn enforcement pattern
- Security test directory (`tests/security/`) established for adversarial input testing
- `ExceptionGroup` used for multi-error aggregation in `close()` methods
- `_build_adapter()` helper in registry.py eliminates adapter construction duplication
- Classification and scope strings share the same format validation regex
- Double-check locking pattern used consistently for module-level registries
