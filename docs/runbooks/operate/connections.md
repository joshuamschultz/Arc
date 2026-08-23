# Connections — setup and durable activation

> **Runbooks** · Operate · **For** operators connecting agents to external systems

Connections are deployment-wide credentials with deny-by-default, per-agent
grants. ArcUI and ArcCLI call the same typed connector service using the
extension's canonical coordinate; display names are presentation only.

!!! note "Existing agents created before connected-data enrollment"
    If a connection works as an agent tool but does not appear under
    **Knowledge → Connections**, install the removable sync layer for that
    agent and restart it:

    ```bash
    arc module install --from-source connected_data --agent <agent-id>
    ```

    Enterprise and federal installations should stage the signed
    `connected_data` bundle and omit `--from-source`.

    If the agent's `arcagent.toml` has no `[modules.connected_data]` block at
    all, bring the whole file up to the current scaffold first — see
    [Agent config drift](agent-config-drift.md).

## Configure

```bash
arc connector list --agent <agent>
arc connector add <agent> <connector>
arc connector auth <agent> <connector>
arc connector probe <agent> <connector>
```

`add` validates that the target agent exists before prompting or writing.
`auth` passes credentials through the connector's secret boundary; secrets must
remain vault-backed and must not be written to manifests, logs or agent
workspaces. `probe` exercises the real native, CLI or MCP attachment.

## Grant and activation

A mutation first updates the durable grant snapshot and records a per-agent
reconciliation command. If the owning agent is in this process, Arc applies the
change immediately and acknowledges it. Otherwise the surface reports
`activation_pending`; the agent reconciles the snapshot at startup and on a
bounded periodic cycle. The queue only accelerates wakeup, so a crash between
the grant write and queue write cannot lose the change. Revocation/removal uses
the same path and withdraws owned tools.

## Troubleshooting

- **Unknown agent:** use an agent from the deployment roster; CLI and UI reject
  inert grants identically.
- **`activation_pending`:** verify the target agent is running and can reach
  ArcStore. It will converge without editing files or restarting solely to load
  the grant.
- **Probe failure:** verify the declared attachment kind and its external
  executable/MCP command, timeout and vault secret—not the display name.
- **ArcStore unavailable:** connector startup remains usable from its grant
  snapshot where possible and emits a degraded audit event; restore the store
  before relying on cross-process convergence.

Never edit a grant, manifest, installed skill/tool or generated connector file
to force activation. Every artifact is reverified and every mutation must pass
identity, authorization and audit boundaries.

## Connected data: from grant to agent retrieval

A connector grant makes an account reachable; it does not silently decide how
that account becomes knowledge. Open **Knowledge → Connections** in ArcUI for
the target agent and complete the source lifecycle:

1. Select the source resources the agent may read (folders, labels, buckets,
   prefixes, schemas or tables).
2. Choose one or more compatible destinations. Arc stages an exact mapping
   proposal; approve it with operator controls. A changed proposal needs a new
   approval.
3. Start synchronization. The source cursor, selected resources and object
   versions are durable, so restart and redelivery do not duplicate records.
4. Check **Documents**, **Datastore**, **Blob folders**, **Profile review** and
   **Index health**. Use **Reindex** to reset the cursor and rebuild that source,
   **Pause/Resume** to control scheduled work, or **Revoke** to remove its
   materialized knowledge and live query access.

Sync remains fail-closed in `awaiting_mapping` until the operator approves the
exact mapping. Unsupported media, expired credentials and rate limits appear as
source errors rather than being silently skipped.

### Alpha provider matrix

| Provider | Selectable resource | Supported destination | Agent retrieval |
|---|---|---|---|
| SQLite | tables | datastore, profile | typed read-only `datastore_query`; approved profile facts |
| PostgreSQL / Supabase | schemas and tables | datastore, profile | typed read-only `datastore_query`; approved profile facts |
| Dropbox | folders | document | `document_search` over extracted, chunked and indexed files |
| OneDrive | drives and folders | document | `document_search` over Graph delta-synchronized files |
| S3-compatible / MinIO | buckets and prefixes | blob, document | blob inventory plus `document_search` for extractable objects |
| Gmail | labels/mailbox | memory, document | memory capture and `document_search` over messages |
| Outlook | mail folders | memory, document | memory capture and `document_search` over messages |
| Confluence | spaces | document | `document_search` over full page bodies |
| GitHub | repositories | document | `document_search` over issue and pull-request bodies |
| Jira | projects | document | `document_search` over full issue descriptions and metadata |
| Readwise Reader | library locations and tags | document | `document_search` over saved document content |

SQLite and PostgreSQL/Supabase are live, read-only datastore adapters: Arc
introspects only selected tables and exposes bounded `get_record`, `find` and
`list` operations with typed parameters. It does not copy a transactional
database into the document index. Dropbox, OneDrive, Gmail and Outlook extract
source content into the document pipeline. S3 maintains a folder/object
inventory and also indexes extractable objects when `document` is selected.

The `profile` destination is deliberately review-gated. Inferred facts appear
under **Knowledge → Connections → Profile review** as pending. Only facts an
operator approves enter agent context; decline and undo are audited and remove
the fact from recall.

### Provider setup notes

- **SQLite:** provide the local database path to the optional SQLite source
  adapter. The file is opened read-only and blocking calls are moved off the
  event loop. Select tables before approval.
- **PostgreSQL/Supabase:** provide a vault-backed, read-only DSN to the
  PostgreSQL extension. Use a Supabase direct or transaction-pooler URL the same
  way. TLS, pooling and reconnect behavior remain inside the adapter.
- **Dropbox:** create a scoped Dropbox app, provide its app key/secret, then run
  `arc connector authorize` to exchange the one-time code for a vault-held
  refresh token. Select the root or explicit folders.
- **Microsoft 365:** install the pinned `ms-365-mcp-server`, configure the Entra
  application values through the connector secret surface, and complete its
  device-code login. One grant contributes distinct Outlook and OneDrive
  sources, so each has its own resources and mapping.
- **S3/MinIO:** provide vault-backed access key material, region and an HTTPS
  endpoint. Grant the credential read-only list/get access only to intended
  buckets; then select buckets or narrower prefixes.
- **Gmail:** install the pinned `gog` binary and run `gog auth add` as a person on
  the host. The OAuth refresh token stays in the platform keyring. Select the
  mailbox or labels after granting the Google Workspace connection.
- **Confluence, GitHub, Jira and Readwise Reader:** connect and grant the account
  on **Connections**, then use the per-agent **Configure & sync** action on that
  same card. Select the spaces, repositories, projects, library locations or
  tags the agent may index; approve the mapping; and run the first sync.

Connecting an account grants its interactive tools; it does not silently copy
all account data into an agent. The connection card is the start of the governed
Knowledge journey: enable sync if needed, select the least-privilege resource
set, approve its destination, sync, then verify retrieval in **Documents**.
1Password is intentionally excluded because vault items are credentials rather
than knowledge documents and must never enter embedding or retrieval indexes.

### Sync cadence

Connected sources synchronize continuously while their agent is running. The
schedule is owned by ArcAgent's removable module, not ArcMemory:

```toml
[modules.connected_data]
enabled = true
interval_seconds = 60
```

The default is every 60 seconds. Change `interval_seconds` in that agent's
`arcagent.toml`; use **Sync now** for an immediate run. Provider rate limits and
temporary failures back off without advancing the durable cursor, so the next
successful run resumes rather than skipping data.

Provider-specific pinned artifacts, scopes and secret prompts are the signed
`extension.toml` manifests under `extensions/`; those manifests are the source
of truth when a provider changes its authentication flow.

### What agents receive

The memory capability exposes three governed retrieval paths:

- `document_search(query, source?, top_k?)` returns classified chunks with
  source provenance from synchronized document sources.
- `datastore_query(source, operation, table, parameters)` routes only to a live,
  approved datastore and permits the bounded `get_record`, `find` and `list`
  operations.
- approved profile facts are injected into context automatically; pending,
  declined and undone facts are never recalled.

`index.md` and other OKF documents pass through the same extraction, chunking,
embedding and retrieval pipeline. When no embedder is configured, keyword and
graph retrieval remain available and **Index health** reports the degraded
semantic channel explicitly.

## Connected-data release gate

Run the deterministic cross-package lifecycle and ArcUI gate:

```bash
UV_CACHE_DIR=/tmp/arc-uv-cache uv run python scripts/run_connected_data_release_gate.py
```

Include the real PostgreSQL source-sync contract when a disposable database is
available. The runner refuses to pretend this passed when the DSN is missing:

```bash
ARC_RELEASE_GATE_POSTGRES=1 \
ARCSTORE_TEST_POSTGRES_DSN='postgresql://arc:secret@127.0.0.1:5432/arc_test' \
UV_CACHE_DIR=/tmp/arc-uv-cache \
uv run python scripts/run_connected_data_release_gate.py
```

The repository gate uses deterministic provider doubles plus a live PostgreSQL
contract when requested. It does not claim a live call to a real Dropbox,
Microsoft, Google, AWS or Supabase account; exercise each deployment's own
credentials with **Probe**, sync one restricted resource, search/query it, then
revoke it before widening access.
