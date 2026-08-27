# SPEC-075 — Slack Connector (Knowledge & Task Extraction)

A Slack **data-source connector** for Arc + a nightly extraction workflow. Reads everything the operator is in (channels + DMs) via user-scoped OAuth, indexes ~90 days into fleet-RAG Knowledge, and nightly extracts tasks → Jira, a summary → Telegram, notes → Knowledge. Read by default; per-channel write allowlist; per-agent grants; arcui + arccli parity.

## Documents

| Doc | Status | Purpose |
|---|---|---|
| [PRD.md](./PRD.md) | draft | Requirements (REQ-001..053), EARS acceptance criteria, MoSCoW |
| [SDD.md](./SDD.md) | draft | Components C1..C11, traceability, module boundaries, security/scale posture |
| [PLAN.md](./PLAN.md) | PENDING | 17 TDD tasks (T-001..T-017), domain-tagged, 4 phases |

## Origin (full workflow trail)

- **Brainstorm**: `.claude/brainstorms/2026-08-27-slack-connector-knowledge-and-tasks.md`
- **Build decisions**: D-706..D-725 in `.claude/decisions-log.md`
- **Deepen**: Research Insights block under the Slack section of the decisions log (5 parallel research streams)
- **Memory**: `project_slack_connector_design.md`

## Key facts to carry into /implement

- **Native connector** — copy `extensions/dropbox`; the bundle implements tool hooks + the six `SourceAdapter` seams in one class. No `packages/arcagent` changes except the net-new C6.
- **Prerequisite (R-1)** — the Slack app must be an **internal custom app** in the operator's workspace, or history reads throttle to ~1 req/min and backfill is infeasible.
- **One net-new primitive (R-3)** — the per-channel write allowlist (`ChannelAuthorizationBinding` + `allowed_channels` on `Connection`). Everything else extends live code.
- **Freshness gap (R-2)** — a poll-only nightly sync misses edits/deletes; T-015 adds the Events-API tail.
- **Reuse** — ingestion (SPEC-073 `connected_data` → pgvector `IndexBackend`), per-agent grants, trifecta write gate, untrusted-input sanitize/defang, `EgressProxy`, `arc connector`/arcui surfaces.
- **Workflow shape** — mirror `nightly-meeting-ingest` v23 (never-fail nodes; `deliver_to` telegram; Jira dedup).

## Learnings / phase notes
_(captured during /implement)_

## Next
- `/implement SPEC-075` — execute the plan (create a feature branch first; deploy from `main`).
- `/validate SPEC-075` — 3-Cs spec quality check.
