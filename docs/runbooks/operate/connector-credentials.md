# Connector credentials

Where Arc keeps the credentials for your connected accounts, how they stay fresh,
and what to do when something goes wrong.

## Where credentials live

- Every connector credential lives in one **sealed custody row** per connection, in
  the Arc data store (arcstore, collection `connector_credentials`).
- Every value is encrypted (XChaCha20-Poly1305). The key comes from your operator key.
  A copy of the database, or a backup, holds only ciphertext.
- A value is bound to its exact connection and field. A value copied into another
  connection does not open.
- There is no plaintext credential file. The old `connections.env` file is gone.

## How agents use them

- An agent never holds a refresh token, a client secret, or a stored API token.
- Its connection gets a **credential handle**. On every call the handle:
  - checks that the agent is still granted the connection;
  - returns a fresh access token (OAuth) or the declared field.
- So a reconnect reaches running agents on their next call. No restart is needed.
- A revoke stops a running agent at its next call.
- Every read is audited as `secret.read`. The audit records the connection and field
  names only, never the value.

## How OAuth tokens stay fresh

- arcui renews each OAuth access token after 75 % of its lifetime.
- An agent can also renew on demand, for example after a provider answers 401.
- Only one process renews a connection at a time. A lease in the custody row makes
  sure of this, across processes and across hosts.
- A rotated refresh token is saved **before** anyone uses the new access token. All
  the new values are written in one atomic database write.
- If the provider says the sign-in is dead (`invalid_grant`), the card shows
  **Needs you → Reconnect** and you get one notice. Arc makes no more calls with
  that dead token until you reconnect.

## Moving off `connections.env` (one time)

arcui does this by itself at startup. To check it first, or to run it by hand:

```bash
arc connector migrate-secrets --dry-run   # list what would move and what would be dropped
arc connector migrate-secrets             # move, verify, delete the file
```

- Only credentials that a connection's bundle declares are moved.
- Leftover keys that no connection declares are dropped. The report lists them by name.
- Each value is read back and compared before the file is deleted.
- If any step fails, the file is kept and arcui does not start. Run the command and
  read its report.

## Tiers

| Custody | Who | Connector credentials |
|---|---|---|
| `in_process` | personal, and enterprise when chosen | sealed with a key derived from the operator key |
| `vault_transit` | enterprise default, federal always | **refused** until the Vault Transit row cipher ships (P18-2F) |

Federal deployments must use Vault Transit. XChaCha20 is not FIPS-approved.

## Rotating the operator key

Custody rows are sealed with a key derived from the operator key. `arc trust rotate`
does not re-seal custody rows yet. **If you rotate the operator key, every
connection becomes "The stored credential could not be read; connect again".**
Reconnect each one after a rotation.

## Threat-model note (personal tier)

Any process that runs as the deployment user and holds the operator key can derive
the custody key. At the personal tier, the handle is a code and audit boundary, not
an operating-system boundary. Vault Transit (federal) takes the key out of the process.
