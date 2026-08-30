# Source Cookbook — The Grant and Auth for Each Connector

> **Get Started**  ·  Set up  ·  one recipe per provider
> **For** operators who know the [connect-a-source](connect-a-source.md) journey and need the provider-specific step
> [Docs home](../README.md)  ·  [← Connect a source](connect-a-source.md)  ·  [Connections runbook →](../runbooks/operate/connections.md)

---

## In one breath

Every connector ships as a **signed extension** under `extensions/`, discovered
as a leaf — the core never names a provider. What differs per provider is only
*how you authenticate*: a connection string, an API key/token, or an OAuth
sign-in (native in-harness, or delegated to the provider's own binary). The
`arc connector` verbs are the same everywhere; this page tells you which auth
path each one takes and what it needs. All nine providers below implement **both**
seams — interactive tools **and** the connected-source Knowledge lifecycle.

## `arc connector` verbs

Verified in `packages/arccli/src/arccli/commands/connector.py`. The connector id
on the CLI is the **extension bundle name**; `--name` sets the per-connection
instance name.

| Verb | Does |
|---|---|
| `available` | list bundles this deployment can connect |
| `add` | connect an account: prompt secrets, probe, persist, grant |
| `grant` / `revoke` | give / take an account to/from agents |
| `auth` | authorise by hidden secret prompt (Arc holds the credential) |
| `authorize` | sign in a connector whose own binary/OAuth holds the credential |
| `host-setup` | install the host binaries a bundle pins (digest-verified) |
| `list` / `tools` | list connected accounts / the verbs an instance offers |
| `semantic` | show/locate a datastore's editable table meanings |
| `probe` / `doctor` | prove reachability / report prerequisites |
| `approve` | approve the tool contract an instance serves now |
| `remove` | disconnect an account, its credential, its grants |

**`arc connector authorize`** (`_authorize`, `connector.py:248`) runs a fully
in-harness OAuth exchange for a bundle that declares a native `[oauth]` block:
Arc builds the authorize URL from the stored app key, you paste the one-time
code, and `connections.complete_oauth(...)` (`:294`) swaps it for a **durable
refresh token** held in the vault — you never see or type the refresh token.
There is deliberately no `--token` flag. Only **dropbox** ships a native
`[oauth]` block; Microsoft 365 and Google Workspace authorize through a host
`authorize_command` instead.

## The recipe table

Auth types: **DSN** = read-only connection string · **key/token** = API
key/secret sent as Bearer or Basic · **OAuth (native)** = `arc connector
authorize` · **OAuth (host)** = provider's own binary/MCP owns the token.

| Provider | Bundle (`connector` id) | Auth | You provide |
|---|---|---|---|
| **PostgreSQL** | `postgresql` | DSN (vault) | read-only Postgres DSN in secret `database_dsn` |
| **Supabase** | `postgresql` *(same bundle)* | DSN (vault) | Supabase direct or pooler URL as the DSN |
| **S3 / MinIO** | `s3` | key/token (SigV4) | `access_key_id`, `secret_access_key`, optional `session_token`, `region`, `endpoint_url` |
| **Dropbox** | `dropbox` | **OAuth (native)** | `app_key`, `app_secret`; run `arc connector authorize` for the refresh token |
| **OneDrive** | `microsoft365` | OAuth (host) | Entra `MS365_MCP_CLIENT_ID` / `_TENANT_ID` / `_CLIENT_SECRET`; `ms-365-mcp-server --login` |
| **Outlook** | `microsoft365` *(same bundle)* | OAuth (host) | same MS365 Entra values / device-code login |
| **Gmail** | `google_workspace` | OAuth (host) | `gog auth add` browser consent; token in host keyring |
| **Slack** | `slack` | key/token (Bearer) | User OAuth token `xoxp-…` in secret `user_token` |
| **Confluence** | `confluence` | key/token (Basic) | `api_token`, `email`, `base_url` |

*(The `extensions/` tree also ships `github`, `jira`, `onepassword`, `sqlite`,
and `readwise_reader` — see the [Connections runbook](../runbooks/operate/connections.md)
provider matrix.)*

## Per-provider notes

- **PostgreSQL / Supabase** (`extensions/postgresql/`). One bundle serves both
  (`display_name = "PostgreSQL / Supabase"`). Provide a **read-only** DSN through
  the connector secret surface; TLS, pooling, and reconnect stay inside the
  adapter. Its source is a *live datastore* shape — `sync_source` only advances a
  checkpoint; rows are never copied into the document index. Agents get typed
  `postgres_schema` / `get` / `find` / `list`, never raw SQL.
- **S3 / MinIO** (`extensions/s3/`). Provide vault-backed access-key material,
  region, and an HTTPS endpoint. Grant the credential read-only list/get on the
  intended buckets only, then select buckets or narrower prefixes. Blob inventory
  plus `document_search` over extractable objects.
- **Dropbox** (`extensions/dropbox/`). Create a scoped Dropbox app, provide its
  `app_key`/`app_secret`, then `arc connector authorize` exchanges the one-time
  code for a vault-held refresh token; access tokens are minted per call
  (`grant_type=refresh_token`). Select the root or explicit folders.
- **Microsoft 365** (`extensions/microsoft365/` → OneDrive + Outlook).
  Install the pinned `ms-365-mcp-server` (needs Node ≥ 20), configure the Entra
  application values through the connector secret surface, and complete its
  device-code login. **One grant contributes two distinct sources** (Outlook and
  OneDrive), each with its own resources and mapping.
- **Gmail / Google Workspace** (`extensions/google_workspace/`). Install the
  pinned `gogcli` binary and run `gog auth add` as a person on the host; the
  OAuth refresh token stays in the platform keyring — **Arc never holds it**. For
  multiple accounts, set `account` → `GOG_ACCOUNT`; a headless box needs
  `GOG_KEYRING_PASSWORD` in the service environment. Select the mailbox or labels
  after granting.
- **Slack** (`extensions/slack/`). Provide a **User OAuth token** (`xoxp-…`) as
  `user_token`, sent as a Bearer header; these tokens don't expire, so there is
  no refresh dance. History rate limits require an internal/Marketplace app. One
  document per channel/DM; plus interactive read/search/send tools.
- **Confluence** (`extensions/confluence/`). Provide an `api_token`, your
  `email`, and the `base_url` (`{site}.atlassian.net`); auth is HTTP Basic
  `(email, api_token)`. `document_search` over full page bodies; plus
  search/get/create/update tools.

## Federal note

Every connector's write/send verb (`send_mail`, `create_page`,
`slack_send_message`, Dropbox egress, …) carries `capability_tags =
["network_egress"]`, which **bars those bundles at federal tier at install**
(D-580). Plan connector use accordingly for a federal-facing deployment.

Provider-specific pinned artifacts, scopes, and secret prompts are the signed
`extension.toml` manifests under `extensions/` — those manifests are the source of
truth when a provider changes its authentication flow.

---

**Next:** verify retrieval and tune it in
[Tune knowledge & memory](tune-knowledge-and-memory.md). **Trouble?** see
[Operate & troubleshoot](operate-and-troubleshoot.md).
