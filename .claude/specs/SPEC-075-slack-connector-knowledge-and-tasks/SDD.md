# SDD — SPEC-075: Slack Connector (Knowledge & Task Extraction)

- **Status**: draft · **PRD**: `./PRD.md` · **Decisions**: D-706..D-725 (`.claude/decisions-log.md`)
- **Design rule**: copy `extensions/dropbox` (the canonical native + OAuth data-source). No changes to `packages/arcagent` except the one net-new binding (C6). The workflow (C9) is signed data, not code.

## 1. Architecture overview

Slack is a **native data-source connector** (no vendor CLI reads user history). The bundle is `extensions/slack/` with `extension.toml` (`attachment="native"`, `[knowledge] mode="source"`) + `arc_ext_slack/__init__.py` holding one `SlackAttachment` class that implements BOTH the tool hooks and the six `SourceAdapter` seams — exactly as `DropboxAttachment` does. It plugs into three framework seams only: `build_native_attachment(context)`, the `[[tools.declared]]` allow-list, and the `SourceAdapter` contract. Ingestion, indexing, retrieval, per-agent grants, the trifecta write gate, and untrusted-input handling are **reused** from live code. The nightly extraction is an ArcFlow workflow mirroring `nightly-meeting-ingest` v23.

```
extensions/slack/ (bundle, deletable)
  extension.toml ─────────────► manifest schema (arcagent/extension/manifest.py)
  arc_ext_slack/__init__.py
    SlackClient (httpx Web API) ◄── rate-limit bucket + Retry-After + circuit breaker
    SlackAttachment
      tool hooks: slack_search / slack_read_channel / slack_read_thread / slack_list_channels / slack_send_message
      SourceAdapter: inspect/list/select/sync/fetch/close_source  ── emits OKF ConnectedDocument
                                              │
            reused, unchanged ◄───────────────┤
  connected_data (arcagent) → arcmemory.connected_data → IndexBackend (pgvector) → document_search
  grants.py (per-agent) · capability_ledger + policy (trifecta) · security.py (sanitize/defang) · EgressProxy
  NET-NEW: ChannelAuthorizationBinding (per-channel write allowlist)
state/workflows/slack-nightly-ingest/ (signed ArcFlow) → tasks→Jira, summary→Telegram(deliver_to), notes→Knowledge
```

## 2. Components

| # | Component | Responsibility | Reuse vs new | Key files |
|---|---|---|---|---|
| **C1** | Slack extension bundle | `extension.toml` (native, knowledge=source, secrets, tools allow + declared classification/tags, approval=outbound, optional `[oauth]`) | **new** (copy dropbox) | `extensions/slack/extension.toml` |
| **C2** | `SlackClient` | httpx Web API: OAuth token use, `conversations.list/history/replies`, `search.messages`, `chat.postMessage`, cursor pagination, rate-limit bucket per method+workspace honoring `Retry-After`, circuit breaker | **new** | `extensions/slack/arc_ext_slack/client.py` |
| **C3** | `SlackAttachment` tool hooks | `requirements/probe/describe_tools/invoke` for the 4 read tools + `slack_send_message`; classifications match manifest | **new** (mirror `DropboxAttachment`) | `extensions/slack/arc_ext_slack/__init__.py` |
| **C4** | `SlackSourceAdapter` (same class) | 6 seams: `inspect_source` (workspace), `list_source_resources` (channels/DMs the user may pick), `select_source_resources`, `sync_source` (paged, watermarked, ~90d slabs), `fetch_source` (one msg/thread ≤ max_bytes), `close_source`; emits OKF `ConnectedDocument` | **new** | same file |
| **C5** | Ingestion & retrieval wiring | maps documents into pgvector index + provenance; `document_search` retrieval; per-agent DID-scoped | **reuse** | `packages/arcmemory/.../connected_data.py`, `index/backend.py`; `arcagent/.../modules/connected_data/*` |
| **C6** | `ChannelAuthorizationBinding` (per-channel write allowlist) | intercepts `slack_send_message`, denies a `channel` not in the per-connection `allowed_channels`; audits verdict | **NET-NEW** (model on `SourceAuthorizationBinding` + `_targets_only_owner`) | `arcagent/.../modules/connectors/` (new binding); `allowed_channels` field on `Connection` (`extension/grants.py`) |
| **C7** | Egress routing for writes | outbound `chat.postMessage` via `EgressProxy.authorize` (allowlist + no-exfil + leg record) | **reuse** | `arcagent/.../tools/_egress.py` |
| **C8** | OAuth connect flow | `arc connector authorize` code-exchange, store durable token; masked key entry | **reuse** | `arcagent/.../extension/oauth.py`, `connections.py`; `arccli/.../connector.py`; arcui card |
| **C9** | `slack-nightly-ingest` ArcFlow | nightly: list new activity → extract Josh's tasks/followups/summaries → tasks→Jira (dedup), summary→Telegram (`deliver_to`), notes→Knowledge; never-fail nodes | **new (data)** | `state/workflows/slack-nightly-ingest/` |
| **C10** | Surfaces (arcui + arccli) | grant/revoke, key add/authorize, health, per-channel allowlist edit, per-agent Knowledge status + configure-and-sync | **reuse + extend** for allowlist | `arccli/.../commands/connector.py`; `arcui/.../routes/connectors.py` + `web/src/.../knowledge-connections.tsx` |
| **C11** | Events-API tail (edits/deletes) | subscribe `message`/`message_changed`/`message_deleted` for go-forward freshness; reconcile index | **new (phase 4)** | `extensions/slack/arc_ext_slack/events.py` |

## 3. Traceability (REQ → component)

| REQ | Components |
|---|---|
| REQ-001,004,005 | C1, C8, C10 |
| REQ-002,003 | C1, C2, C8 |
| REQ-006, REQ-051 | C2 |
| REQ-010,011,014 | C4, C5 |
| REQ-012 | C11 (or C4 periodic re-scan) |
| REQ-013 | C5 (reused sanitize/defang) |
| REQ-020,021,022 | C5, C10, grants (reused) |
| REQ-030,031,032 | C9 |
| REQ-040 | C1, C3, **C6** |
| REQ-041 | trifecta (reused), C3 tags |
| REQ-042 | C7 |
| REQ-050 | audit (reused) across C2–C9 |
| REQ-052 | C1 (seam) |
| REQ-053 | C10 |

## 4. Key design decisions (from /build + /deepen)
- **Native, not CLI** [D-706]: Slack's CLI can't read user history; use user-token OAuth + Web API. Copy dropbox.
- **Internal custom app** [R-1]: the deployment's Slack app MUST be an internal (in-workspace) app to escape the ~1 req/min non-Marketplace history throttle. Documented as an install prerequisite; the connector is unsupported on a throttled external app for backfill.
- **Per-agent grant is native** [D-708]: no new access model; `allowed_channels` (write) is the only per-connection field added.
- **Events tail for correctness** [R-2]: periodic sync misses edits/deletes; C11 closes it (phased).
- **One net-new primitive** [R-3]: C6, the per-channel write allowlist binding.

## 5. Module boundaries (Modularity pillar)
- The bundle imports ONLY `arcagent.extension.{attachment,source}` value types → deletable (REQ-052). No optional Slack dep leaks into core contracts.
- C6 lives in `modules/connectors` (the enforcement home), not in the bundle — a channel allowlist is policy, enforced host-side.
- C9 is signed data under `state/workflows/`, executed by the existing ArcFlow runner.

## 6. Security posture (Security pillar)
Secrets vault-backed, validated by live probe before persist, never on argv/echoed. Read verbs `read_only`, no egress tag → install at every tier. Write verb `network_egress` → federal install refusal accepted; trifecta gate + `arc approve` on the read→post chain; EgressProxy no-exfil. Ingested content sanitized/defanged/DATA-framed (LLM01). Per-agent DID-scoped index (LLM08). Full audit + provenance.

## 7. Scalability posture (Scalability pillar)
One deployment connection backs unlimited agents (per-agent grant). Backfill: recent-first 90d in time slabs, resumable cursors, idempotent `(channel_id, ts)` upserts; token bucket per method+workspace; parallelize across workspaces only (limit is per app+workspace). Ingestion on pgvector HNSW with a cached async pool + bounded per-sync byte/time caps.

## 8. Abuse cases (for the adversarial battery)
Forged/self-minted approval; injection via message content (forged markers, "ignore previous instructions", zero-width); cross-agent + DM leakage; rate-limit exhaustion; classification laundering; the full read-private→ingest-untrusted→Slack-post exfil chain (must trip `forbidden_composition`).
