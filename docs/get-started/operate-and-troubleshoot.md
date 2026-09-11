# Operate and Troubleshoot Connectors

> **Get Started**  ·  Operate  ·  keep connected data healthy
> **For** operators running a live deployment who hit sync, index, or credential trouble
> [Docs home](../README.md)  ·  [Connections runbook →](../runbooks/operate/connections.md)  ·  [Troubleshooting reference →](../reference/troubleshooting.md)

---

## In one breath

Once sources are connected, most operational questions are variations on "why
isn't this data showing up?" The lifecycle gives you five operator actions —
**sync / pause / resume / reindex / revoke** — all operator-gated and audited, and
a handful of status fields (`last_synced_at`, `pages`, `bytes_processed`,
`error_code`, index health). This page maps the common symptoms to their cause
and the command that fixes them.

## The lifecycle actions

Every action routes through the service's bounded scheduler, is operator-gated,
and emits a `connected_data.<action>` mutation audit event
(`packages/arcui/src/arcui/routes/connected_data.py:412`, operations map at
`:430`). In ArcUI they are the buttons on the connection card; the same
operations exist behind the API:

| Action | Effect |
|---|---|
| `sync` / `retry` | run one sync now (`service.sync_now`) |
| `pause` / `resume` | stop / restart scheduled sync for that source |
| `reindex` | reset the cursor and rebuild that source's document pool |
| `revoke` | remove the source's materialized knowledge and live query access |

`reindex` is the blunt instrument when an index looks wrong; `revoke` is the
clean teardown ("disconnect = delete the pool").

## Symptom → cause → command

| Symptom | Likely cause | Do |
|---|---|---|
| Source stuck in **`awaiting_mapping`** | mapping never approved, or a changed proposal needs a fresh approval | Approve the exact mapping under **Knowledge → Connections** (a changed proposal re-gates by design) |
| **`activation_pending`** after a grant | the owning agent isn't in this process; it reconciles at startup / on a bounded cycle | Verify the agent is running and can reach ArcStore; it converges without editing files |
| **`last_synced_at` shows "Never"** / counters read 0 | the status object has no `last_synced_at` yet, so the wire falls back to `None` (`connected_data.py:110`, safe default) | Run one **Sync now**; if it persists after a successful sync, the index-count wiring across the two-DB split is a known gap — **needs confirmation** whether your build reads it |
| **Probe fails** | wrong attachment kind, missing host binary/MCP command, timeout, or vault secret | Check the declared attachment and its external command/secret — not the display name (`arc connector probe <id>`, `arc connector doctor <id>`) |
| **ArcStore unavailable** | the operational store is down | Connector startup stays usable from its grant snapshot and emits a degraded audit event; restore the store before relying on cross-process convergence |
| **Index health: degraded semantic channel** | no embedder configured, or `sqlite-vec` missing | Configure an embedder ([tune memory](tune-knowledge-and-memory.md)); keyword + graph retrieval keep working meanwhile |
| **Unsupported media / expired credential / rate limit** | surfaced as a source error, not silently skipped | Re-auth the credential, or narrow the resource selection; back-off does not advance the cursor, so no data is lost |

## Headless nodes hang on the keyring

A connector whose auth lives in the OS keyring (Gmail's `gog`, for example) will
**hang a CLI** on a headless box because the keyring tries to reach a D-Bus
session that isn't there. Your deploy automation should neutralize this by
exporting `DBUS_SESSION_BUS_ADDRESS=/dev/null` for the service and for deploy
commands. If you invoke a connector CLI by hand on a headless node and it hangs,
set that variable in your shell:

```bash
export DBUS_SESSION_BUS_ADDRESS=/dev/null
```

A headless Gmail box also needs `GOG_KEYRING_PASSWORD` in the service environment
so the keyring can unlock without an interactive prompt.

## Don't force it

Never edit a grant, manifest, installed skill/tool, or generated connector file
to force activation. Every artifact is reverified and every mutation must pass
identity, authorization, and audit boundaries — a hand-edited grant is either
ignored or rejected, and you lose the audit trail. Use the lifecycle actions
above; they are the supported, audited path.

## Verifying a connector end to end

The repository gate uses deterministic provider doubles plus a live PostgreSQL
contract when requested:

```bash
UV_CACHE_DIR=/tmp/arc-uv-cache uv run python tests/run_connected_data_release_gate.py
```

It does **not** claim a live call to a real Dropbox / Microsoft / Google / AWS /
Supabase account. For those, exercise each deployment's own credentials with
**Probe**, sync one restricted resource, search/query it, then revoke it before
widening access.

---

**Related:** [Connections runbook](../runbooks/operate/connections.md) (the
operator reference for grant/activation), the
[connected-source data flow](../walkthrough/flow-connected-source.md) (why sync is
fail-closed on mapping), and the general
[troubleshooting reference](../reference/troubleshooting.md).
