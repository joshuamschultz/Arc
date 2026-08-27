# PRD — SPEC-075: Slack Connector (Knowledge & Task Extraction)

- **Status**: draft
- **Type**: integration
- **Owner**: josh
- **Sources**: `.claude/brainstorms/2026-08-27-slack-connector-knowledge-and-tasks.md`; decisions D-706..D-725 + Research Insights in `.claude/decisions-log.md`
- **Pillars order**: Simplicity → Modularity → Security → Scalability

## 1. Problem & Outcome

Most of Josh's decisions and commitments happen in Slack and are lost in the scroll. Arc already mines meetings (nightly-meeting-ingest v23: tasks→Jira, summary→Telegram, notes→Knowledge). This brings the same discipline to Slack: index everything Josh is in as fleet-searchable Knowledge, and run a nightly digest that extracts his tasks/followups/summaries. Success = "add the key once in either arcui or arccli, it saves, the connection stays working, and every agent I allow can search my Slack."

## 2. Scope

**In**: a Slack **data-source connector** (native, user-token OAuth), full-history-to-90d ingestion into Knowledge (fleet-RAG, per-agent grant), a nightly extraction workflow, read tools + an operator-gated per-channel write tool, arcui + arccli parity.
**Out**: replacing the existing Slack **chat gateway adapter**; reading channels/DMs Josh is not a member of (needs the licensed Discovery API); multi-workspace federation in v1; real-time streaming as the primary path.

## 3. Requirements (EARS-format acceptance criteria)

Priority = MoSCoW. Each criterion names its governing pillar.

### Connect & credentials
- **REQ-001 (Must)** — WHEN an operator runs `arc connector add slack` or uses the arcui connection card, the system SHALL prompt for the Slack app `client_id`/`client_secret` (masked for secrets), store them vault-backed (0600, per-connection `SecretRef`), and never echo a secret back. *Pillar: Security.* [D-717]
- **REQ-002 (Must)** — WHEN an operator runs `arc connector authorize slack`, the system SHALL run the OAuth v2 flow, obtain a **user-scoped token** via the `user_scope` param, and persist only the durable token/refresh token — never a short-lived access token on argv. *Pillar: Security.* [D-706]
- **REQ-003 (Must)** — WHERE the token is requested, the system SHALL request exactly these user scopes: `channels:history, groups:history, im:history, mpim:history, channels:read, groups:read, im:read, mpim:read, users:read` and (write only) `chat:write`. *Pillar: Simplicity.* [D-706]
- **REQ-004 (Must)** — WHEN install completes, the system SHALL validate the stored credential against a live `probe()` **before** persisting the connection as healthy, and refuse the install if the probe fails. *Pillar: Security.* [D-724]
- **REQ-005 (Must)** — The system SHALL expose connection health (`arc connector probe`/`doctor` and the arcui card) reflecting auth + reachability, with no false "healthy". *Pillar: Simplicity.*
- **REQ-006 (Should)** — WHERE the operator runs an internal (in-workspace) Slack app, the connector SHALL function within Slack's standard-tier history limits; the docs SHALL state that a non-Marketplace external app is rate-limited to ~1 req/min and is unsupported for backfill. *Pillar: Scalability.* [R-1]

### Ingestion → Knowledge
- **REQ-010 (Must)** — WHEN a source is granted and channels are selected, the system SHALL backfill ~90 days of history (configurable later) and index messages, threads, and channel metadata as OKF `ConnectedDocument`s into the existing pgvector `IndexBackend`, with provenance (source, channel, ts). *Pillar: Modularity.* [D-709, D-713]
- **REQ-011 (Must)** — The system SHALL sync incrementally on a nightly-aligned schedule, resuming from a per-channel watermark (max `ts`) and per-thread watermark (`latest_reply`), with idempotent upserts keyed `(channel_id, ts)`. *Pillar: Scalability.* [D-710]
- **REQ-012 (Should)** — WHILE syncing, the system SHALL reflect message **edits and deletes** to already-indexed content (via the Slack Events API tail, or a periodic re-scan), so the index does not retain deleted or stale content. *Pillar: Security.* [R-2]
- **REQ-013 (Must)** — WHEN Slack content is ingested, the system SHALL sanitize + injection-defang it at ingest and DATA-frame it at recall (treat as inert content, never instructions). *Pillar: Security.* [D-719]
- **REQ-014 (Must)** — The connector SHALL implement the six canonical source seams (`inspect/list/select/sync/fetch/close_source`) so a granted connection provably becomes searchable Knowledge (release-gate contract). *Pillar: Modularity.* [D-714]

### Knowledge access (fleet, per-agent)
- **REQ-020 (Must)** — Slack Knowledge SHALL be grantable **per agent**; an agent in no grant attaches nothing and reads nothing (deny-by-default). The operator toggles which agents see it from BOTH `arc connector grant/revoke` and the arcui card. *Pillar: Security.* [D-708]
- **REQ-021 (Must)** — A granted agent SHALL retrieve Slack Knowledge via `document_search`, scope-isolated to that agent+source (no cross-agent leakage) and classification no-read-up. *Pillar: Security.* [D-708]
- **REQ-022 (Must)** — WHEN an operator revokes a grant, live reads SHALL be denied immediately and indexed content purged before the registration is dropped. *Pillar: Security.*

### Nightly extraction workflow
- **REQ-030 (Must)** — The system SHALL provide a signed ArcFlow workflow that, nightly, extracts Josh's tasks/followups/summaries from newly-synced Slack activity and delivers: tasks → Jira (deduped, search-before-create), a summary → Telegram (pinned via `deliver_to`), detailed notes → Knowledge. *Pillar: Simplicity.* [mirrors nightly-meeting-ingest v23]
- **REQ-031 (Must)** — No workflow node may hard-fail the run; failures are reported in the summary. The summary SHALL always deliver to the operator's Telegram. *Pillar: Simplicity.*
- **REQ-032 (Should)** — Josh SHALL be able to author additional Slack workflows through arcui or agent chat on top of the connection, without code changes. *Pillar: Modularity.*

### Write (operator-gated)
- **REQ-040 (Must)** — The Slack write tool (`slack_send_message`) SHALL be `state_modifying` + `capability_tags=["network_egress"]`, OFF by default, and post only to channels on a per-connection **allowlist** the operator sets in arcui + arccli. *Pillar: Security.* [D-707, D-719]
- **REQ-041 (Must)** — A read-private + untrusted-input + Slack-post sequence SHALL trip the lethal-trifecta gate and pause for `arc approve` (never over agent chat). *Pillar: Security.* [D-720]
- **REQ-042 (Must)** — Outbound Slack writes SHALL route through `EgressProxy.authorize` (origin allowlist + no-exfil classification gate + leg recording). *Pillar: Security.*

### Cross-cutting non-functional
- **REQ-050 (Must)** — Every read, write, sync, grant change, and token op SHALL emit an audit event; docs carry provenance. *Pillar: Security.* [D-715, D-716]
- **REQ-051 (Must)** — Slack API calls SHALL use rate-limit-aware retry with backoff honoring `Retry-After`, a circuit breaker, and a per-method+workspace token bucket; a network blip degrades to a typed partial result, never a silent abort. *Pillar: Scalability.* [D-721]
- **REQ-052 (Must)** — The connector SHALL install/remove cleanly as an optional extension (seam): removing its files leaves imports, startup, and unrelated features working. *Pillar: Modularity.* [D-722]
- **REQ-053 (Must)** — Every operator action SHALL have both an arcui and an arccli path (surface parity). *Pillar: Simplicity.* [D-724]

## 4. Personas
- **Josh (operator)** — connects Slack once, grants agents, gets the nightly digest, searches Slack via any allowed agent.
- **Fleet agents** — RAG Josh's Slack Knowledge within their grant + clearance.

## 5. Known pitfalls (from research)
- Non-Marketplace external app → ~1 req/min history throttle → backfill infeasible. Use an **internal custom app**. [R-1]
- Poll-only sync silently drops edits/deletes → index drift. [R-2]
- Per-channel write allowlist is **net-new** (no existing Arc primitive). [R-3]
- "Everything you're in" is bounded by Josh's own membership. [R-4]

## 6. Out-of-scope / deferred
Multi-workspace federation; Discovery-API all-org reads; real-time as primary; on-demand deep-backfill UX beyond 90d (v2).
