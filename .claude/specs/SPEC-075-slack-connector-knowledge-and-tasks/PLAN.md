# PLAN — SPEC-075: Slack Connector (Knowledge & Task Extraction)

- **Status**: PENDING · **PRD**: `./PRD.md` · **SDD**: `./SDD.md`
- **TDD**: every impl task is preceded by its failing test. `domain:` ∈ {test, api, backend, ui, db, ai-workflow, ai-chain, auth, infra, mixed}.
- **Prereq (R-1)**: the deployment's Slack app MUST be an **internal custom app** in the operator's workspace (escapes the ~1 req/min non-Marketplace history throttle). Backfill is unsupported otherwise.

## Phase 1 — Foundation (bundle · client · auth)

- [ ] **T-001** `domain:test` — SlackClient contract tests against a fake Web API: cursor pagination, `types=` filtering, 429 + `Retry-After` honored, circuit-breaker opens, partial-page on blip. *(C2 · REQ-051)*
- [ ] **T-002** `domain:backend` — `SlackClient`: `conversations.list/history/replies`, `search.messages`, `chat.postMessage`; cursor pagination; per-method+workspace token bucket honoring `Retry-After`; circuit breaker; typed partial result on blip (never silent abort). *(C2 · REQ-006, REQ-051)*
- [ ] **T-003** `domain:auth` — `extensions/slack/extension.toml`: `attachment="native"`, `[knowledge] mode="source"`, `[[secrets]]` (client_id non-sensitive, client_secret, refresh/user token written-by-flow), `[tools] allow` + `[[tools.declared]]` (4 reads `read_only`; `slack_send_message` `state_modifying`+`network_egress`), `[approval] default="outbound"`, `[config.native] entrypoint`, `[oauth]` block. Header documents D-588 check (native chosen). *(C1 · REQ-001, REQ-003, REQ-040, REQ-052)*
- [ ] **T-004** `domain:auth` — OAuth connect: `arc connector authorize slack` obtains user-scoped token via `user_scope`, stores durable token vault-backed; masked key entry (no `--token`); `probe()` validates before persist. *(C8 · REQ-002, REQ-004, REQ-005)*

## Phase 2 — Core (tools · source seams · ingestion)

- [ ] **T-005** `domain:test` — `SlackAttachment` tool-hook + `probe()` contract tests (describe_tools classifications match manifest; 401/403 → actionable `_refused` text). *(C3)*
- [ ] **T-006** `domain:backend` — `SlackAttachment` tool hooks: `slack_search`, `slack_read_channel`, `slack_read_thread`, `slack_list_channels`, `slack_send_message`; imports only `arcagent.extension.{attachment,source}` value types. *(C3 · REQ-003, REQ-040)*
- [ ] **T-007** `domain:test` — Source-adapter contract tests incl. release-gate shape: grant → select → mapping-approve → `sync_source` → `ArcMemoryBrain.document_search` returns the synced chunk; delete → tombstone; restart → resumes. *(C4/C5 · REQ-014)*
- [ ] **T-008** `domain:backend` — `SlackSourceAdapter` (same class): `inspect/list/select/sync/fetch/close_source`; emit OKF `ConnectedDocument` (provenance: source/channel/ts); ~90d recent-first backfill in time slabs; per-channel `history_watermark` + per-thread `thread_watermark`; idempotent `(channel_id, ts)` upserts; threads via `conversations.replies`. *(C4 · REQ-010, REQ-011, REQ-014)*
- [ ] **T-009** `domain:test` — Integration: ingested Slack doc is sanitized/defanged, indexed to pgvector with provenance, and retrieved scope-isolated (no cross-agent leak; classification no-read-up). *(C5 · REQ-013, REQ-021)*

## Phase 3 — Integration (grant · write gate · workflow · surfaces)

- [ ] **T-010** `domain:test` — Abuse-case battery (added to `scripts/run_adversarial_tests.py`): forged/self-minted approval, injection via message content, cross-agent + DM leakage, rate-limit exhaustion, classification laundering, full read-private→ingest-untrusted→Slack-post exfil chain trips `forbidden_composition`. *(SDD §8 · REQ-041)*
- [ ] **T-011** `domain:auth` — **NET-NEW** `ChannelAuthorizationBinding`: add `allowed_channels` to `Connection` (`extension/grants.py`); intercept `slack_send_message`, deny a `channel` not on the allowlist (model on `SourceAuthorizationBinding` + `_targets_only_owner`), audit verdict; route the write through `EgressProxy.authorize`. *(C6/C7 · REQ-040, REQ-042)*
- [ ] **T-012** `domain:test` — Trifecta E2E: read a private channel + ingest an injected DM + attempt `slack_send_message` → gate pauses for `arc approve`; approval never accepted over agent chat. *(REQ-041)*
- [ ] **T-013** `domain:ai-workflow` — `state/workflows/slack-nightly-ingest/` ArcFlow: `list_new` → `extract` (Josh's tasks/followups/summaries, rich notes) → `create_jira`(dedup search-before-create) → `notify_summary`(`deliver_to = telegram:<josh>`) → notes→Knowledge; every node never-fails (errors as data); `arc workflow sign`. *(C9 · REQ-030, REQ-031, REQ-032)*
- [ ] **T-014** `domain:ui` — arcui connection card: per-channel write-allowlist editor + per-agent Knowledge on/off toggle + configure-and-sync journey; `arc connector` arccli parity for the allowlist and per-agent grant. *(C10 · REQ-020, REQ-053)*

## Phase 4 — Polish (freshness · docs · deploy · release gate)

- [ ] **T-015** `domain:backend` — Events-API tail: subscribe `message`/`message_changed`/`message_deleted`; reconcile index (apply edits, tombstone deletes) so the index never retains deleted/stale content. *(C11 · REQ-012)*
- [ ] **T-016** `domain:infra` — Install runbook + deploy: internal-custom-app prerequisite, exact scopes, rate-limit note (R-1); `arc connector add/authorize/grant` steps; deploy the extension to DGX from `main`. *(REQ-006)*
- [ ] **T-017** `domain:test` — E2E release gate on a real test workspace: granted connection → backfill → `document_search` → nightly workflow fires → Jira task + Telegram summary land; revoke → live reads denied + content purged. *(REQ-014, REQ-022, REQ-030)*

## Dependencies
T-002←T-001 · T-004←T-003 · T-006←T-003,T-005 · T-008←T-004,T-007 · T-009←T-008 · T-011←T-006,T-010 · T-012←T-011 · T-013←T-008 · T-014←T-011 · T-015←T-008 · T-017←(T-013,T-014,T-016)

## Definition of done (per Arc core.md)
Unit + integration + abuse-case tests pass · `mypy --strict` + `ruff` clean · audit emitted on every new op · no plaintext secrets · docstrings on public API · the release-gate E2E (T-017) green · deployed from `main`.
