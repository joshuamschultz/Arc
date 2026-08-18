# Architecture Decision Records — index

**38 decision records in the repo-level series, across three locations. The next free number in
that series is ADR-035.** Specs carry their own ADR series with their own numbering — see
[An ADR number is only unique within its series](#an-adr-number-is-only-unique-within-its-series).

This file is the authority on what exists and what number is free. Three pages under `docs/`
previously kept their own tables and disagreed with each other about the count, the range, and where
ADRs live; they now point here.

## Where they live, and why there are three places

| Location | Numbers | Form |
|---|---|---|
| `.claude/architecture/decisions/` | ADR-001–007, ADR-018–032 | One file per decision. **New ADRs go here.** |
| `.claude/specs/*/SDD.md` | ADR-008–017 | Recorded inline in the spec that produced them, under `### ADR-NNN:` headings |
| `.claude/adrs/` | ADR-017A–017D | One file per decision, suffixed off ADR-017 |

**ADR-008 through ADR-017 are not a gap.** They are ten real decisions — API-key resolution,
`BaseAdapter`, the provider registry, CLI structure — written inside `003-anthropic-adapter`,
`005-openai-adapter`, `006-provider-registry`, `014-arc-cli` and `SPEC-017-arc-core-hardening`
rather than as standalone files. **Do not reuse those numbers, and do not renumber to close the
gap:** every number from 001 to 032 is allocated in this series, and renumbering would break
references across docs, specs, and source while colliding with decisions that already own them.

### An ADR number is only unique within its series

There are around 106 `### ADR-` headings across `.claude/specs/`, and **they do not share one
numbering line with the table below.** `010-audit-trail-module/SDD.md` has its own ADR-031 and
ADR-032; so does `011-otel-export/SDD.md`; `015-content-guardrails/SDD.md` runs a 4xx series
(ADR-419–434) that is cited from source, including `ADR-429` in
`packages/arcllm/src/arcllm/modules/guardrails.py`.

So **a bare "ADR-031" is ambiguous.** A number identifies a decision only together with where it
lives. When citing one outside its own document, say which: "ADR-031 (repo-level)" or "ADR-031 in
`010-audit-trail-module`". The "next free number is ADR-035" above applies to **this series only** —
the standalone files in this directory.

## ADR-001–007

| ADR | Status | Title |
|---|---|---|
| [ADR-001](ADR-001-transport-implementation-deferral.md) | Accepted | MCP/HTTP/Process transport implementation deferred |
| [ADR-002](ADR-002-arcrun-bridge-vs-wrapper.md) | Accepted | ArcRun bridge pattern vs event wrapper |
| [ADR-003](ADR-003-config-basesettings-vs-manual-overrides.md) | Accepted | Pydantic `BaseModel` + manual env overrides vs `BaseSettings` |
| [ADR-004](ADR-004-core-loc-budget-increase.md) | Accepted | Core LOC budget increase to 3,500 |
| [ADR-005](ADR-005-sync-filesystem-io-in-async-tools.md) | Accepted | Synchronous filesystem I/O in async tool functions |
| [ADR-006](ADR-006-assemble-prompt-subscriber-contract.md) | Accepted | Canonical contract for `agent:assemble_prompt` subscribers |
| [ADR-007](ADR-007-roster-pull-vs-push-for-large-teams.md) | Accepted (Phase 1 pull model) | Roster pull-vs-push strategy for large teams |

## ADR-008–017 — inside spec SDDs

| ADR | Recorded in |
|---|---|
| ADR-008 – ADR-011 | `.claude/specs/003-anthropic-adapter/SDD.md` |
| ADR-012 – ADR-013 | `.claude/specs/005-openai-adapter/SDD.md`, `.claude/specs/006-provider-registry/SDD.md` |
| ADR-014 – ADR-016 | `.claude/specs/014-arc-cli/SDD.md` |
| ADR-017 | `.claude/specs/SPEC-017-arc-core-hardening/REVIEW.md` |
| [ADR-017A](../../adrs/ADR-017A-opt-in-policy-pipeline.md) | Opt-in policy pipeline |
| [ADR-017B](../../adrs/ADR-017B-legacy-module-clean-delete.md) | Legacy module clean delete |
| [ADR-017C](../../adrs/ADR-017C-defense-in-depth-dynamic-sandbox.md) | Defense in depth — dynamic sandbox |
| [ADR-017D](../../adrs/ADR-017D-tier-flows-through-registry-construction.md) | Tier flows through registry construction |

## ADR-018–034

| ADR | Status | Title |
|---|---|---|
| [ADR-018](ADR-018-no-mcp-no-migration-no-acp.md) | Accepted for migration tooling and ACP; **the MCP-client exclusion is superseded by [ADR-030](ADR-030-mcp-capability-and-extension-placement.md)** | SPEC-018 excludes MCP client, migration tooling, and ACP adapter |
| [ADR-019](ADR-019-four-pillars-universal.md) | Accepted | Four Pillars (identity, sign, authorize, audit) are universal defaults |
| [ADR-020](ADR-020-arcgateway-as-data-plane.md) | Accepted | `arcgateway` owns the data plane; `arcui` is a pure consumer |
| [ADR-021](ADR-021-agent-self-description-via-toml-ui-section.md) | Accepted | Agent self-description via optional `[ui]` section in `arcagent.toml` |
| [ADR-022](ADR-022-storage-split-arctrust-worm-arcstore-operational.md) | **Proposed** | Storage split — `arctrust` owns WORM audit, `arcstore` owns operational persistence |
| [ADR-023](ADR-023-capability-resolution-and-arcrun-provider.md) | Accepted | Capability resolution — unified provider, layered roots, last-wins, signed-to-load |
| [ADR-024](ADR-024-unified-streaming-run-entry.md) | Accepted | One streaming, session-bound `agent.run` — every surface goes through `arcrun` |
| [ADR-025](ADR-025-cache-control-confined-to-anthropic-adapter.md) | Accepted | Provider cache directives confined to the Anthropic adapter |
| [ADR-026](ADR-026-transform-context-append-only-with-emergency-valve.md) | Accepted | `transform_context` is append-only; compaction is a between-run boundary event |
| [ADR-027](ADR-027-per-run-tool-set-freeze-security-invariant.md) | Accepted | Per-run tool-set freeze as a structural security invariant |
| [ADR-028](ADR-028-append-only-prefix-contract-debug-gated.md) | Accepted | Append-only prefix contract enforced only under a debug flag |
| [ADR-029](ADR-029-workspace-vs-working-dir-and-state-persistence.md) | Accepted | Agent home (workspace) vs working directory; state persists via direct workspace I/O |
| [ADR-030](ADR-030-mcp-capability-and-extension-placement.md) | Accepted | Agents get MCP capability; extensions plug in from outside |
| [ADR-031](ADR-031-dynamic-script-not-declared-graph.md) | Accepted | Ad-hoc model-authored orchestration is a restricted script, not a declared graph |
| [ADR-032](ADR-032-channel-responder-selection-routes-on-published-indexes.md) | Accepted | A channel responder is chosen by routing over published indexes, not self-assessment |
| [ADR-033](ADR-033-module-dependency-contract-is-the-configure-signature.md) | Accepted | A module's dependency contract is its `configure()` signature, not a core registry |
| [ADR-034](ADR-034-live-module-activation-is-a-revertible-transaction.md) | Accepted | Live module enable/disable/upgrade is one revertible transaction, no restart |

## Writing a new one

Take **ADR-035**, add a file here as `ADR-NNN-<slug>.md`, and follow the existing template:
`Status`, `Date`, `Relates to` / `Supersedes` where applicable, then `Context`, `Decision`,
`Consequences`.

Then add a row to the ADR-018–034 table above and bump the next-free number in the heading. That is
the only list that needs updating — the pages under `docs/` link here rather than copying it.

If a decision supersedes part of an earlier ADR, say so in **both** — ADR-018/ADR-030 is the worked
example, and the reason ADR-018's status is a sentence rather than a word.
