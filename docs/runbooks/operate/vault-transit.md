# HashiCorp Vault Transit custody

Arc can keep the operator signing key and the connector-credential key in
HashiCorp Vault Transit. The keys never leave Vault. Arc sends a message or a
value to Vault and gets back a signature or ciphertext.

Use this at enterprise and federal tier. Personal and enterprise deployments
without Vault use the local notary (`[security] notary_keystore`) instead.

| What Arc asks Vault to do | Vault endpoint | Key type |
|---|---|---|
| Seal a connector credential | `POST /v1/<mount>/encrypt/<key>` | `aes256-gcm96` |
| Open a connector credential | `POST /v1/<mount>/decrypt/<key>` | `aes256-gcm96` |
| Sign as the operator | `POST /v1/<mount>/sign/<key>` | `ed25519`, or `ecdsa-p256` at federal |
| Check an operator signature | `POST /v1/<mount>/verify/<key>` | same as sign |
| Read the public key and check the key is safe | `GET /v1/<mount>/keys/<key>` | both |

Each credential is bound to its connection and field with Vault's
`associated_data` (AEAD additional data). Vault refuses to open a value under
any other connection or field. Arc does not use derived keys.

## 1. Create the keys

```sh
vault secrets enable transit
vault write -f transit/keys/arc-connector-credentials type=aes256-gcm96
vault write -f transit/keys/arc-operator type=ed25519      # enterprise
# federal: vault write -f transit/keys/arc-operator type=ecdsa-p256
```

Do not set `exportable`, `allow_plaintext_backup`, `derived` or
`deletion_allowed`. Arc reads each key's settings before first use and refuses a
key that has any of them.

**Federal (FIPS).** Federal tier forces `signing_algorithm = "ecdsa-p256"`, so
the operator key must be `ecdsa-p256`. Run the Vault Enterprise FIPS 140-3 build
(or seal-wrap the keys into an HSM). Arc sees only signatures and ciphertext, so
FIPS validation of the keys is Vault's.

## 2. Grant exactly these paths

This policy is the whole grant. The test suite applies this block to a real
Vault, so it is proven to be enough.

```hcl
path "transit/keys/arc-operator" { capabilities = ["read"] }
path "transit/sign/arc-operator" { capabilities = ["update"] }
path "transit/verify/arc-operator" { capabilities = ["update"] }
path "transit/keys/arc-connector-credentials" { capabilities = ["read"] }
path "transit/encrypt/arc-connector-credentials" { capabilities = ["update"] }
path "transit/decrypt/arc-connector-credentials" { capabilities = ["update"] }
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self" { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
```

`read` on `transit/keys/<name>` returns key settings and the public key only.
The keys are not exportable, so no key material can leave.

```sh
vault policy write arc-custody arc-custody.hcl
```

## 3. Set up AppRole

```sh
vault auth enable approle
vault write auth/approle/role/arc-custody \
    token_policies=arc-custody token_no_default_policy=true \
    token_ttl=15m token_max_ttl=4h secret_id_ttl=720h
vault read -field=role_id auth/approle/role/arc-custody/role-id
vault write -f -field=secret_id auth/approle/role/arc-custody/secret-id > arc-secret-id
```

The `role_id` is not secret and goes in the config. The `secret_id` is secret
and never goes in the config. Give it to Arc with systemd (next step), then
delete `arc-secret-id`.

Arc renews its token at two-thirds of its TTL. When renewal is refused or the
max TTL is reached, Arc logs in again with the same `secret_id`. So do not set
`secret_id_num_uses` to 1. Rotate the `secret_id` before `secret_id_ttl` ends.

Kubernetes: use `auth_method = "kubernetes"` and `role = "<vault role>"`. Arc
reads the projected service-account token at
`/var/run/secrets/kubernetes.io/serviceaccount/token`. Other JWT issuers: use
`auth_method = "jwt"`, `role`, and a `secret_source` for the JWT.

## 4. Give Arc the secret_id with systemd

Encrypt the `secret_id` to the host so it is never on disk in plain text:

```sh
systemd-creds encrypt --name=vault-secret-id arc-secret-id /etc/credstore.encrypted/vault-secret-id
shred -u arc-secret-id
```

In the Arc unit (`systemctl edit arc.service`):

```ini
[Service]
LoadCredentialEncrypted=vault-secret-id:/etc/credstore.encrypted/vault-secret-id
```

systemd decrypts it into `$CREDENTIALS_DIRECTORY/vault-secret-id`, a
non-swappable in-memory file only this unit can read. Arc reads it once at
start. The Vault token Arc gets with it is kept in memory only, never written.

Other sources Arc accepts for `secret_source`:

| Source | Meaning |
|---|---|
| `credential:<name>` | `$CREDENTIALS_DIRECTORY/<name>` (systemd `LoadCredential`) |
| `fd:<n>` | read an inherited file descriptor to end, then close it |
| `env:<VAR>` | read an environment variable, then remove it so child processes do not get it |
| `file:/abs/path` | a tmpfs-projected token, such as the Kubernetes one; symlinks are refused |

## 5. Configure Arc

In `arcagent.toml`:

```toml
[security]
tier = "enterprise"            # or "federal"
custody = "vault_transit"

[security.vault]
addr = "https://vault.example.internal:8200"   # https only
ca_bundle = "/etc/arc/vault-ca.pem"            # the CA that signs Vault's certificate
mount = "transit"
namespace = ""                                  # Vault Enterprise namespace, e.g. "arc/prod"
auth_method = "approle"
role_id = "<role_id from step 3>"
secret_source = "credential:vault-secret-id"

[security.vault.keys]
operator = "arc-operator"
connector-credentials = "arc-connector-credentials"
```

Optional limits (defaults shown): `timeout_s = 5`, `max_retries = 2`,
`breaker_failures = 5`, `breaker_cooldown_s = 30`.

When `[security.vault]` is present, Arc uses Vault for signing and for connector
credentials. When it is absent, `vault_transit` custody uses the local notary.
`[security.vault]` with `custody = "in_process"` is refused at start.

Restart Arc. Then check:

```sh
arc team status         # "Operator audit key" resolves through Vault
```

If Vault cannot serve the operator key, `arc team status` reports the operator
key as missing and Arc refuses to sign. Check the audit log for the
`custody.transit.*` event with outcome `error` or `refused`.

## What happens when Vault is down

- Each call has a timeout and up to `max_retries` retries for network errors,
  `429` and `5xx`.
- After `breaker_failures` failed calls in a row, Arc stops calling Vault for
  `breaker_cooldown_s` seconds, then tries one call.
- Nothing falls back to a key on the host. Signing fails. Connector cards show
  **"Arc's credential vault is not answering"** (`CREDENTIAL_CUSTODY_UNAVAILABLE`)
  with action **wait**. Do not reconnect. The stored credentials are fine.

## Audit

Every Vault call writes one audit event: `custody.transit.encrypt`,
`.decrypt`, `.sign`, `.verify`, `.key_read`, `.login` or `.renew`. Each has the
key name, the outcome (`allow`, `refused` or `error`), the attempt count and the
time. No plaintext, ciphertext, signature, token or `secret_id` is ever in an
event or a log line.

## Moving an existing deployment to Vault

- **Operator key.** The Vault key is a new operator key. Moving to it is the
  same as an operator key rotation: everything pinned to the old public key
  (bundles, skills, workflows, schedules) must be signed again by the new key.
- **Connector credentials sealed in-process** (`custody = "in_process"` before):
  run `arc connector migrate-secrets --reseal` after the switch. See
  [Connector credentials](connector-credentials.md).
- **Connector credentials sealed by the local notary** (`notary:v1:` values):
  Vault cannot open them. Connect those connections again after the switch.
