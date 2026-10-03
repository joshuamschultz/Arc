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
workspaces. `probe` runs the connection's declared health check once and
records the result (the same check the background loop runs), so what it prints
is what the card and the next notice say. `list` shows each connection's
`Status`, `Reason` and `Checked` columns from that record.

## Health

Every connection has one durable health record. Its stored status is `unknown`,
`healthy`, `needs_you` or `error`; the card adds a derived `syncing` while an
agent's sync holds a live lease (a crash cannot leave it stuck).

- **Who writes it.** Only the health authority, from five sources: the
  scheduled probe, the end of a sync run, the credential lifecycle, an operator
  action (connect, re-auth, approve, Check now) and the tool-contract ledger.
- **What the probe is.** Each bundle declares `[health]` in its
  `extension.toml`: `probe = "attachment"` (the attachment's own probe),
  `"host_verify"` (the host sign-in check) or `"tool:<name>"` (one read-only
  tool). A bundle without it gets the default for its shape; a bare CLI with no
  sign-in check stays "Not checked yet" rather than guessed healthy.
- **Schedule.** ArcUI runs one probe loop: 30 minutes (with jitter) for
  `healthy`/`unknown`, 10 for `error`, 5 for `needs_you`.
- **When something breaks.** A credential the provider rejects moves the
  connection to `needs_you` at once; failures that may be a blip (timeouts, 5xx,
  rate limits) move it to `error` only after three failures over at least ten
  minutes, or 24 hours without a success. A changed tool contract is `needs_you`
  with **Approve changed tools**, and a working probe does not clear it.
- **Notices.** One message per outage, delivered through the first granted agent
  that can reach you (`notify_operator`), claimed with a lease on the record so a
  restart or several agents cannot send it twice. Retries are bounded (three
  tries), then the card shows **Could not notify you**. Changes you cause
  yourself send no notice.

Notices need a granted agent loaded in the ArcUI process and a channel you have
used with it; without both the notice is recorded as undeliverable and the card
is the only signal.

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

### The semantic layer — telling agents what the data MEANS

Introspection can say a table is called `inv_hdr` with a column `amt`. Only you
can say that is an invoice and a dollar amount. On first connect, a datastore
writes an editable file naming everything it found:

```
~/arc/config/semantic/<connection>.toml
```

```toml
[table.inv_hdr]
entity = "invoice"                  # what ONE ROW is. Agents search by this.
description = "One row per invoice we billed."
hidden = false                      # keep it out of what agents are SHOWN
searchable = ["note"]               # omit to keep what was detected

[table.inv_hdr.column.amt]
label = "amount"
description = "What we billed, in whole dollars."
```

`sqlite_schema` answers from this file, so an edit reaches agents on their next
call — no restart, no re-sync. It is **never overwritten**: a later scan only
appends tables that have appeared, and an entry for a table that has gone away
is left alone. A syntax error degrades to the schema's own names rather than
taking the connection down.

`hidden` is readability, not permission. What an agent may REACH is the resource
selection you approved when connecting; hiding a table here does not secure it,
and un-hiding one does not grant it.

The `profile` destination is deliberately review-gated. Inferred facts appear
under **Knowledge → Connections → Profile review** as pending. Only facts an
operator approves enter agent context; decline and undo are audited and remove
the fact from recall.

### Provider setup notes

- **SQLite:** connect with `arc connector add sqlite --name <id>`, or the
  Connections card. Two fields, neither a credential: `database_path` is the
  absolute path to the `.db`/`.sqlite` file, and `host` is `user@hostname` when
  that file is on another machine — leave it EMPTY when it sits beside the agent.
  A remote database is copied here over your existing ssh key and re-copied only
  when its fingerprint moves. The file is opened read-only (`mode=ro`), symlinks
  are refused, and blocking calls are moved off the event loop. Select tables
  before approval; a table you did not select cannot be read. Agents get
  `sqlite_schema`, `sqlite_get`, `sqlite_find` and `sqlite_list` — there is no
  raw-SQL verb.
- **PostgreSQL/Supabase:** provide a vault-backed, read-only DSN to the
  PostgreSQL extension. Use a Supabase direct or transaction-pooler URL the same
  way. TLS, pooling and reconnect behavior remain inside the adapter.
- **Dropbox:** create a scoped Dropbox app, provide its app key/secret, then run
  `arc connector authorize` to exchange the one-time code for a vault-held
  refresh token. Select the root or explicit folders.
- **Microsoft 365 (Outlook, calendar, OneDrive; commercial and GCC):** no
  binary and no device code. Arc talks to Microsoft Graph directly and keeps the
  refresh token in its own sealed storage. See
  [Microsoft 365 setup](#microsoft-365-setup) below. One connection contributes
  two distinct sources, Outlook (`<name>:outlook`) and OneDrive
  (`<name>:onedrive`), each with its own resources and mapping.
- **S3/MinIO:** provide vault-backed access key material, region and an HTTPS
  endpoint. Grant the credential read-only list/get access only to intended
  buckets; then select buckets or narrower prefixes.
- **Gmail:** set up Google sign-in once, then add one Google Workspace
  connection per account and click **Connect** on its card. Arc talks to Google
  directly and keeps the refresh token in its own sealed storage. Select the mailbox or labels after
  granting the connection. See [Google accounts](google-accounts.md), including
  how to stop tokens expiring every week.
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

### Microsoft 365 setup

Do this once per deployment. It works for Microsoft 365 commercial and for
**GCC (moderate)** tenants: both use `login.microsoftonline.com` and
`graph.microsoft.com`. GCC High and DoD are a different cloud setting, not a
different setup.

1. **Register the app in Microsoft Entra.** Entra admin center → App
   registrations → New registration. Supported account types: *Accounts in this
   organizational directory only* (single tenant).
2. **Add the redirect address.** Platform **Web**. The value is the address the
   "Set up Microsoft sign-in" panel shows, which is `<public address>/oauth/callback`,
   or `http://127.0.0.1:8420/oauth/callback` when no public address is saved in
   **Settings → Access**. Entra accepts `http` only for `localhost` /
   `127.0.0.1`, and the portal's Web Redirect URI box refuses
   `http://127.0.0.1…`: add it in the app's **Manifest** (`replyUrlsWithType`,
   `"type": "Web"`) instead, or (better) save an https public address and
   register that. If the browser you sign in with cannot reach the redirect
   address, paste the address you landed on into the card's paste box (see
   [Sign in from any browser](#sign-in-from-any-browser)).
3. **Create a client secret.** Certificates & secrets → New client secret → copy
   the **Value** (not the Secret ID). Entra shows it once.
4. **Add API permissions.** Microsoft Graph → *Delegated*: `openid`, `profile`,
   `offline_access`, `User.Read`, `Mail.Read`, `Mail.Send`,
   `Calendars.ReadWrite`, `Files.Read`. Then click **Grant admin consent**.
   GCC tenants usually block users from consenting for themselves, so an admin
   must do this once; without it, sign-in stops at "Need admin approval".
5. **Set up Microsoft sign-in in Arc.** Connections → Microsoft 365 → paste the
   Application (client) ID, the Directory (tenant) ID (a GUID from the Overview
   page; `common` is refused) and the secret, and pick the cloud
   (*Commercial / GCC* unless the tenant is GCC High or DoD) → Save. Headless:
   `arc connector oauth-app microsoft --client-id <id> --tenant-id <guid> --cloud global --client-secret-stdin`
   with the secret on stdin.
6. **Connect.** Add a Microsoft 365 connection (optionally naming the mailbox in
   `account`), click **Connect**, sign in. Arc checks the sign-in came from your
   tenant (`tid`) as that account before it stores anything. The card turns
   **Healthy**.
7. **Knowledge.** Grant the connection to an agent, then **Configure & sync** on
   the card: pick the Outlook folder (Inbox by default) and the OneDrive folder,
   approve the mapping, run the first sync. Outlook uses Graph's message delta,
   so later syncs fetch only changes and deletions.

Why each permission: `openid`/`profile` give Arc the sign-in's tenant and
account to check (no data); `offline_access` gives the refresh token;
`User.Read` is the health check; `Mail.Read` reads mail; `Mail.Send` is used
only by `send-mail`, which asks for approval; `Calendars.ReadWrite` lets
`create-calendar-event` invite attendees (also approval-gated); `Files.Read`
reads only the user's own OneDrive.

Honest limits: Entra rotates the refresh token on every refresh (Arc stores
each new one). A password reset, an admin revoking sessions, or a Conditional
Access change can end the sign-in; the card then says **Reconnect** and you get
one notice. Changing the app's tenant or cloud in Arc makes existing
connections ask for a reconnect: a token from one directory is never sent to
another. In OneDrive for Business, selecting a subfolder (rather than the whole
drive) may be refused by Graph's delta query; select the drive root if so.

### Sign in from any browser

Every step is in ArcUI. Nothing here needs a terminal.

1. **Save the public address.** **Settings → Access → Public address**: the
   address people type to open this dashboard from other computers, for
   example `https://arc.tail1234.ts.net`. It must be `https`; plain `http` is
   accepted only for `127.0.0.1` / `localhost` at the personal tier. If ArcUI
   sees `tailscale serve` in front of its port, it offers that address; it also
   offers the https address your browser is on. You still click **Save**: Arc
   never takes the address from a request. The change applies to the next
   sign-in, with no restart. The panel shows the return address
   (`<public address>/oauth/callback`) to register with Google, Microsoft and
   Atlassian.
2. **Give the dashboard https, if nothing else does.** A reverse proxy or
   `tailscale serve` in front of ArcUI already provides https: then skip this.
   Otherwise paste a certificate chain and its private key under **Settings →
   Access → HTTPS certificate** and click **Restart stack**. The key is stored
   encrypted; its passphrase is sealed by the deployment's custody (the
   operator key, or the Vault transit). At the federal tier the dashboard
   serves https only: off loopback it will not start without a certificate.
3. **Connect.** Before you click **Connect**, the card says where the provider
   will send the browser. When this browser is not on that address, the
   paste box is already open: after you approve, copy the "can't connect"
   page's address and paste it. Arc checks it against the saved address and
   the sign-in's state; anything else is refused.

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
UV_CACHE_DIR=/tmp/arc-uv-cache uv run python tests/run_connected_data_release_gate.py
```

Include the real PostgreSQL source-sync contract when a disposable database is
available. The runner refuses to pretend this passed when the DSN is missing:

```bash
ARC_RELEASE_GATE_POSTGRES=1 \
ARCSTORE_TEST_POSTGRES_DSN='postgresql://arc:secret@127.0.0.1:5432/arc_test' \
UV_CACHE_DIR=/tmp/arc-uv-cache \
uv run python tests/run_connected_data_release_gate.py
```

The repository gate uses deterministic provider doubles plus a live PostgreSQL
contract when requested. It does not claim a live call to a real Dropbox,
Microsoft, Google, AWS or Supabase account; exercise each deployment's own
credentials with **Check now**, sync one restricted resource, search/query it, then
revoke it before widening access.
