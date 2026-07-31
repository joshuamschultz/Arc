# PRD: Budget Control & Compliance-Aware Routing

## Problem Statement

ArcLLM currently has no mechanism to enforce spend limits or route requests based on data classification. Without budget controls, a runaway agent or misconfigured loop can burn unlimited API credits — violating the Anti-Deficiency Act (31 U.S.C. 1341) in federal deployments. Without classification-aware routing, sensitive data (CUI, PII) may be sent to unauthorized providers — violating NIST 800-53 AC-3/AC-4 and FedRAMP authorization boundaries.

Both gaps are security-critical for federal environments (DOE, SCIF) and cost-critical for enterprise environments.

## Requirements

### Functional Requirements — Budget

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-B1 | TelemetryModule tracks cumulative spend per budget scope (agent ID) in an in-memory accumulator | Must |
| FR-B2 | Budget enforces three limit types: monthly, daily, and per-call max | Must |
| FR-B3 | Monthly and daily accumulators auto-reset on calendar boundaries (UTC) | Must |
| FR-B4 | `enforcement = "block"` raises `ArcLLMBudgetError` when any limit would be exceeded | Must |
| FR-B5 | `enforcement = "warn"` logs warning + emits OTel event but allows the call, sets `response.metadata["budget_warning"] = True` | Must |
| FR-B6 | Alert threshold (default 80%) emits OTel event when cumulative spend crosses threshold, before hard limit | Must |
| FR-B7 | Pre-flight estimate uses `max_tokens * cost_output_per_1m / 1_000_000` to reject obviously excessive calls before execution | Must |
| FR-B8 | `budget_scope` is a required top-level kwarg on `load_model()` when budget is enabled | Must |
| FR-B9 | Budget scope string is validated (lowercase alphanumeric + colons/dots/hyphens, max 128 chars, NFKC normalized) | Must |
| FR-B10 | Cost is clamped to `max(0.0, cost)` before deducting from accumulator (prevents negative cost injection) | Must |
| FR-B11 | Budget-related OTel span attributes use `arcllm.budget.*` namespace | Must |
| FR-B12 | Budget config fields live under `[modules.telemetry]` in config.toml, not a separate section | Must |

### Functional Requirements — Routing

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-R1 | RoutingModule implements `LLMProvider` and replaces the single adapter at the innermost stack position | Must |
| FR-R2 | Routing rules map `classification` string to `{provider, model}` pairs via TOML config | Must |
| FR-R3 | `classification` kwarg passes through the module stack via `**kwargs` and is consumed (popped) by RoutingModule | Must |
| FR-R4 | All adapters are eagerly loaded at RoutingModule init (fail-fast on bad config) | Must |
| FR-R5 | Unknown classification with `enforcement = "warn"` logs warning and routes to `default_classification` | Must |
| FR-R6 | Unknown classification with `enforcement = "block"` raises `ArcLLMConfigError` | Must |
| FR-R7 | `RoutingModule.close()` closes ALL internal adapters | Must |
| FR-R8 | `routing` kwarg added to `load_model()` with same `bool | dict | None` pattern as other modules | Must |
| FR-R9 | RoutingModule resolves vault API keys per-provider (each adapter may use different vault paths) | Must |
| FR-R10 | RoutingModule exposes `name` and `model_name` properties from the default route's adapter | Must |

### Non-Functional Requirements

| ID | Requirement | Threshold |
|----|-------------|-----------|
| NFR-1 | Zero new dependencies | 0 new packages |
| NFR-2 | Budget additions to TelemetryModule | <= 80 LOC added |
| NFR-3 | RoutingModule total | <= 150 LOC |
| NFR-4 | All existing tests pass | 0 regressions |
| NFR-5 | `mypy --strict` passes | 0 errors |
| NFR-6 | `ruff check` passes | 0 errors |
| NFR-7 | Test coverage for new code | >= 90% |
| NFR-8 | No global state mutation outside registry patterns | 0 violations |
| NFR-9 | Pre-flight check latency | < 1ms (single multiplication) |

## Success Criteria

1. Budget enforcement blocks calls when monthly/daily/per-call limits are exceeded in `block` mode
2. Budget enforcement warns but allows calls in `warn` mode with metadata annotation
3. Alert threshold fires OTel event at 80% (configurable) spend
4. Pre-flight estimate rejects calls whose estimated cost exceeds per-call max
5. Budget accumulators reset on calendar boundaries (UTC month/day)
6. RoutingModule routes `classification="cui"` to a FedRAMP-authorized provider and `classification="unclassified"` to a cost-optimized provider
7. Unknown classification behavior follows enforcement config
8. All adapters within Router are eagerly loaded and independently closeable
9. `classification` kwarg flows through the entire module stack without modification by intermediate modules
10. All budget and routing events have proper OTel span attributes under `arcllm.budget.*` and `arcllm.routing.*` namespaces
11. Security tests cover: scope injection, negative cost injection, classification downgrade, adapter isolation
12. All existing tests pass with zero regressions
13. `mypy --strict` and `ruff check` clean

## Out of Scope

- Per-provider spend tracking (OTel already records provider per span — use Grafana to query)
- Budget persistence across restarts (OTel collectors hold durable records)
- Hot-reload of routing rules (consistent with all other modules)
- Content-based classification detection (deferred to Step 18 Content Scanner)
- Automatic model fallback within Router (FallbackModule handles this separately)
- Billing reconciliation (budget is for enforcement, not billing)

## Compliance Mapping

| Control | Requirement |
|---------|-------------|
| **Anti-Deficiency Act (31 U.S.C. 1341)** | FR-B1 through FR-B7 — pre-obligation spend caps |
| **NIST 800-53 SA-2** | FR-B1 — resource allocation tracking per agent |
| **NIST 800-53 AU-3** | FR-B11, FR-R3 — audit record content for budget/routing events |
| **NIST 800-53 AC-3** | FR-R1 through FR-R6 — access enforcement based on classification |
| **NIST 800-53 AC-4** | FR-R2 — information flow enforcement |
| **NIST 800-53 SC-7** | FR-R1 — boundary protection (CUI stays within authorized systems) |
| **OWASP LLM10** | FR-B1 through FR-B7 — unbounded consumption protection |
| **OWASP ASI02** | FR-B9, FR-B10 — tool misuse prevention |
