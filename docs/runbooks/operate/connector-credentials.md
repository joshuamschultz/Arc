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
arc connector migrate-secrets --dry-run            # show the fate of every key; writes nothing
arc connector migrate-secrets                      # move, verify, delete the file
arc connector migrate-secrets --drop-undeclared    # also drop the UNRESOLVED keys on purpose
```

- Credentials that a connection's bundle declares are moved into sealed custody.
- A bundle's old sign-in app pair (for Dropbox, `<NAME>_APP_KEY` and
  `<NAME>_APP_SECRET`) is moved into that provider's sealed sign-in app slot. A
  bundle names its old pair with `[oauth] legacy_client_id_env` and
  `legacy_client_secret_env`.
- Every other value is UNRESOLVED: no connection declares it, custody or the app slot
  already holds a different value, or it is half of an app pair. Arc never drops an
  UNRESOLVED value by itself. arcui does not start, names the keys, and keeps the file.
- To continue, read `--dry-run`. Re-enter any value you still need (for example
  `arc connector oauth-app dropbox`, or connect the account again). Then run
  `--drop-undeclared`. Each dropped key is audited by name.
- Each value is read back and compared before the file is deleted. There is no
  plaintext backup: until you resolve it, the original file is the only copy.
- If any step fails, the file is kept and arcui does not start.

## Tiers

| Custody | Who | Connector credentials |
|---|---|---|
| `in_process` | personal, and enterprise when chosen | sealed with a key derived from the operator key |
| `vault_transit` | enterprise default, federal always | sealed by reference in the transit (AES-256-GCM); the key never enters an Arc process |

Federal deployments must use Vault Transit. XChaCha20 is not FIPS-approved.

### Vault Transit custody (enterprise and federal)

Each value is sent to the deployment's transit for encryption and decryption. The
transit holds the key `connector-credentials`. Arc never reads it. The associated
data binds each value to its connection and field, the same as in-process.

The reference transit is the notary keystore that already signs for the operator
(`[security] notary_keystore`, default `<operator_key_dir>/notary`). It mints
`connector-credentials.aes256` (`0600`) the first time a credential is sealed.
Back up that file with the keystore. If it is lost, every connection must be
connected again.

If the transit does not answer, nothing is read or written. The connection card
shows **"Arc's credential vault is not answering"** with action **wait**. Do not
reconnect: the stored credential is fine. It clears when the transit is back.

Steps for an enterprise deployment (custody defaults to `vault_transit`):

1. Provision the notary keystore so it can serve the `operator` key.
2. Restart arc. New credentials are now sealed in the transit.

Steps for an enterprise deployment that ran `custody = "in_process"` before:

1. Keep the old operator key file in place.
2. Set `[security] custody = "vault_transit"` and provision the notary keystore.
3. Run `arc connector migrate-secrets --reseal --dry-run` to see the rows that will move.
4. Run `arc connector migrate-secrets --reseal`. Each row moves in one verified
   write. It is safe to run again after a crash; rows that already moved are skipped.
5. Restart arc. Until step 4 runs, such rows show "could not be read" and name the
   reseal command.

Federal: the same steps; `require_fips = true` also needs a FIPS-validated
OpenSSL provider, or Arc refuses to start.

The reseal is a manual, audited command on purpose. It must read the old
in-process key once. A long-running server under `vault_transit` must never hold it.

## Rotating the operator key

Custody rows are sealed with a key derived from the operator key. `arc trust rotate`
does not re-seal custody rows yet. **If you rotate the operator key, every
connection becomes "The stored credential could not be read; connect again".**
Reconnect each one after a rotation.

## Threat-model note (personal tier)

Any process that runs as the deployment user and holds the operator key can derive
the custody key. At the personal tier, the handle is a code and audit boundary, not
an operating-system boundary. Vault Transit (enterprise default, federal always)
takes the key out of the process.
