# SPEC-019: ArcUI Zero-Config Launch

## Metadata

| Field | Value |
|-------|-------|
| **ID** | SPEC-019 |
| **Feature** | arcui-zero-config |
| **Type** | Integration (cross-package: arcteam, arcllm, arcui, arcagent, arccli) |
| **Status** | DRAFT |
| **Created** | 2026-04-26 |
| **Author** | Claude Opus 4.7 (planning session) |
| **Priority** | High |
| **Confidence** | 85% (well-scoped; supersedes 4 SPEC-016 decisions) |

## Coding Identity

Per `~/.claude/agents/principled-coder.md`. Pillars in priority order: **Simplicity → Modularity → Security → Scalability**. Every requirement and design choice in this spec is filtered through them. The unification rationale below is itself a Pillar 3 application: configuration (bind address) flips the security posture; no code fork by deployment tier.

## Substrate

| Source | Location | Status | Relationship |
|--------|----------|--------|--------------|
| SPEC-015 | `.claude/specs/SPEC-015-arcui-llm-telemetry/` | PENDING | TraceStore, on_event, ConfigController. Reused unchanged. |
| SPEC-016 | `.claude/specs/SPEC-016-multi-agent-ui/` | superseded by SPEC-019 | UIEvent envelope, AgentRegistry, UITransport Protocol, agent_ws route, SubscriptionManager, control proxy, RollingAggregator, UIReporterModule, agent_token mechanism — kept; auth/discovery semantics replaced wholesale by SPEC-019. No backwards-compat layer. |

## Adds

1. **`workspace_path` field on `arcteam.types.Entity`** + persistence + `arc team register --workspace` flag + idempotent backfill of existing 5 entities by scanning `team/*/arcagent.toml`.
2. **Registry-driven multi-workspace warm-start** for arcui's aggregator. New method `RollingAggregator.warm_start_multi(stores: list[TraceStore])` instead of a new MultiWorkspaceTraceStore class (Pillar 1: don't conflate one-writer and many-reader semantics).
3. **Loopback bootstrap auth flow** — `arc ui start` calls `webbrowser.open("http://127.0.0.1:8420/#auth=<viewer_token>")`. Browser bootstrap stores token in localStorage. Zero paste, full audit.
4. **`ui_reporter` auto-enable logic** — module checks for `~/.arcagent/ui-token` (0600, owned by current UID) at startup; probes URL; enables itself if both pass.
5. **Audit events for the new auth path** — `ui.session_start` with `auth_method: "browser_bootstrap" | "manual_token" | "agent_token"`, plus `ui.agent_autoconnect`. Satisfies NIST AU-2 even with no manual interaction.

## Packages Affected

| Package | Changes | Approx LOC |
|---------|---------|------------|
| **arcteam** | `Entity.workspace_path` field; one-shot backfill | 80 |
| **arcllm** | `RollingAggregator.warm_start_multi()` (added in arcui — see SDD); no arcllm changes if aggregator stays in arcui | 0 |
| **arcui** | `warm_start_multi()`, `FederatedTraceStore` (read-only, fans out `/api/traces` queries), browser bootstrap auth flow, audit emissions, registry-driven trace discovery | 280 |
| **arcagent** | `ui_reporter` auto-enable probe (~30 LOC change to existing module init) | 40 |
| **arccli** | `arc ui start` zero-arg behavior; `arc team register --workspace`; backfill subcommand `arc team backfill-workspaces` | 150 |
| **Tests** | Unit + integration across all five packages | 250 |
| **Total** | | **~800 LOC** |

## Key Decisions

| ID | Decision | Pillar |
|----|----------|--------|
| D-1 | Single security axis = bind address (loopback vs network), not deployment tier | Simplicity, Security |
| D-2 | Tokens always exist; never bypassed for audit; only the *delivery mechanism* changes (URL hash on loopback, manual paste off-loopback) | Security |
| D-3 | Workspace discovery via arcteam registry, not filesystem glob | Modularity (one source of truth) |
| D-4 | `warm_start_multi(stores)` method on aggregator (in-memory rollup) AND a separate read-only `FederatedTraceStore` for `/api/traces` queries; both keep one-writer-many-stores out of `JSONLTraceStore` | Simplicity (don't conflate writer/reader); Modularity (two distinct read-only seams) |
| D-5 | `ui_reporter` enabled-by-default on probe success; opt-out via TOML | Simplicity (works out of the box) |
| D-6 | `webbrowser.open()` with token in URL hash; hash never leaves browser, never logged server-side | Security (no paste, full audit, industry-standard pattern from Jupyter/vscode tunnel) |
| D-7 | Backfill is one-shot CLI (`arc team backfill-workspaces`), idempotent, dry-run by default | Simplicity, Modularity |
| D-8 | All four CLAUDE.md tiers run the same code path; tier choice in `~/.arc/arcagent.toml` only flips defaults that any user can override | Security (Pillar 3 explicit guidance) |

## Solutions Referenced

- None applicable — no `.claude/solutions/` entries match `arcui` or `multi-workspace` patterns at time of writing.

## Files

- `PRD.md` — Product requirements (FR/NFR/SR), supersedes table, acceptance criteria
- `SDD.md` — System design with explicit module boundaries and supersession map
- `PLAN.md` — Phased tasks (single PR, logically phased for review)

## Learnings

_Captured during implementation._
