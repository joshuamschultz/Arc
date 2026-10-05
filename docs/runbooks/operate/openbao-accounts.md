# OpenBao account authority

People accounts (the Settings > People tab and `arc user`) live behind
Vault/OpenBao at every tier. Arc holds no account key and no local fallback.
Without this setup the People tab answers 503 "Account authority is unavailable".

| Part | What it is |
|---|---|
| `arc-<tenant>-<deployment>-transit` | Transit mount: one Ed25519 key per account (`arc-user-<hex>`) and the `account-seal` key |
| `arc-<tenant>-<deployment>-anchor` | KV v2 mount with `cas_required=true`: records `config` and `users` |
| Four AppRoles | `config-reader`, `issuer`, `cipher`, `anchor`, one policy each |
| Two signed files | authority config + capability grants, signed by the operator key |

`<ns>` below means `arc-<tenant>-<deployment>` (for example `arc-acme-dgx`).
`tenant` and `deployment` match `^[a-z][a-z0-9-]{1,31}$`.

The operator key signs the config and grants. It must be Ed25519. A federal
deployment whose operator key is `ecdsa-p256` cannot enroll yet: arctrust verifies
Ed25519 only, and `arc accounts enroll` stops with a clear error.

## 1. Mounts and the seal key

```sh
bao secrets enable -path=<ns>-transit transit
bao secrets enable -path=<ns>-anchor -version=2 kv
bao write <ns>-anchor/config cas_required=true
bao write -f <ns>-transit/keys/account-seal type=aes256-gcm96
```

Do not set `exportable`, `derived`, `allow_plaintext_backup` or `deletion_allowed`
on `account-seal`. Arc reads the key settings before use and refuses a key that
has any of them. Account keys are created by Arc with the same settings off.

## 2. Policies

Apply the files in `deploy/openbao/policies/` after replacing `@NS@`:

```sh
for p in issuer cipher anchor config-reader enroll; do
  sed "s/@NS@/<ns>/g" deploy/openbao/policies/$p.hcl \
    | bao policy write <ns>-$p -
done
```

Key names come from the account store: each account gets `arc-user-<uuid hex>`.
The issuer therefore needs `create` + `read` on `keys/arc-user-*` and `update` on
`sign/arc-user-*`, nothing else.

`issuer.hcl`

```hcl
path "@NS@-transit/keys/arc-user-*" { capabilities = ["create", "read"] }
path "@NS@-transit/sign/arc-user-*" { capabilities = ["update"] }

path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
```

`cipher.hcl`

```hcl
path "@NS@-transit/keys/account-seal"    { capabilities = ["read"] }
path "@NS@-transit/encrypt/account-seal" { capabilities = ["update"] }
path "@NS@-transit/decrypt/account-seal" { capabilities = ["update"] }

path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
```

`anchor.hcl`

```hcl
path "@NS@-anchor/data/users"     { capabilities = ["create", "read", "update"] }
path "@NS@-anchor/metadata/users" { capabilities = ["read"] }
path "@NS@-anchor/config"         { capabilities = ["read"] }
path "@NS@-anchor/delete/users"   { capabilities = ["deny"] }
path "@NS@-anchor/destroy/users"  { capabilities = ["deny"] }
path "@NS@-anchor/undelete/users" { capabilities = ["deny"] }

path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
```

`config-reader.hcl`

```hcl
path "@NS@-anchor/data/config"     { capabilities = ["read"] }
path "@NS@-anchor/metadata/config" { capabilities = ["read"] }
path "@NS@-anchor/config"          { capabilities = ["read"] }

path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
```

`enroll.hcl` (for the one-time admin token in step 4)

```hcl
path "@NS@-anchor/data/config"     { capabilities = ["create", "read", "update"] }
path "@NS@-anchor/metadata/config" { capabilities = ["read"] }
path "@NS@-anchor/metadata/users"  { capabilities = ["read"] }
path "@NS@-anchor/config"          { capabilities = ["read"] }
path "@NS@-anchor/delete/config"   { capabilities = ["deny"] }
path "@NS@-anchor/destroy/config"  { capabilities = ["deny"] }
```

## 3. AppRoles

Each token carries exactly one policy. `token_no_default_policy=true` is
required: Arc checks that the token's policy list equals `[<ns>-<capability>]`
and refuses a token that also holds `default`. It also checks the token is
renewable with a TTL of 1 to 3600 seconds.

```sh
bao auth enable approle
for cap in config-reader issuer cipher anchor; do
  bao write auth/approle/role/<ns>-$cap \
    token_policies=<ns>-$cap token_no_default_policy=true \
    token_ttl=15m token_max_ttl=1h
  bao read -field=role_id auth/approle/role/<ns>-$cap/role-id       # -> role_id in arcagent.toml
  bao write -f -field=secret_id auth/approle/role/<ns>-$cap/secret-id \
    | systemd-creds encrypt --name=arc-accounts-$cap - /etc/credstore.encrypted/arc-accounts-$cap
done
```

Role names are `<ns>-config-reader`, `<ns>-issuer`, `<ns>-cipher`, `<ns>-anchor`.
The secret IDs reach Arc only through a secret source (`credential:`, `file:`,
`env:` or `fd:`). Never put a secret ID in `arcagent.toml`.

Tokens last at most one hour. Arc renews them, and logs in again with the held
secret ID after a lease error.

## 4. Enroll the deployment

Mint a short-lived token with the `enroll` policy and pipe it in. It is never
an argument and never printed.

```sh
bao token create -policy=<ns>-enroll -ttl=10m -field=token \
  | arc accounts enroll --vault-url https://openbao.example:8200 \
      --ca-file /etc/arc/openbao-ca.pem --deployment-id dgx --tenant-id acme \
      --admin-token-stdin
```

It signs the config (revision 1) and three 365-day grants with the operator key,
writes the config head to `<ns>-anchor/config` with CAS, writes
`~/arc/config/accounts-authority.json` and `~/arc/config/accounts-grants.json`
(both 0600), and prints the `[security.accounts]` block. Every step is appended
to the operator audit chain. If the `users` record does not exist yet it also
leaves a single-use marker, `~/arc/config/users-bootstrap.once`, which Arc
consumes to create it.

Options: `--grant-days N` (default 365), `--rotate` (next revision: use it to
renew grants or move the Vault URL), `--authority-config PATH`, `--grants-file
PATH`. `--rotate` makes older config files and grants invalid; the running Arc
needs a restart.

## 5. Configure Arc

Paste the printed block into `~/arc/config/arcagent.toml` and fill in the role IDs:

```toml
[security.accounts]
vault_url = "https://openbao.example:8200"
ca_file = "/etc/arc/openbao-ca.pem"
deployment_id = "dgx"
tenant_id = "acme"
authority_config = "/home/arc/arc/config/accounts-authority.json"
grants_file = "/home/arc/arc/config/accounts-grants.json"
config_reader_role_id = "<role_id>"
config_reader_secret = "credential:arc-accounts-config-reader"
issuer_role_id = "<role_id>"
issuer_secret = "credential:arc-accounts-issuer"
cipher_role_id = "<role_id>"
cipher_secret = "credential:arc-accounts-cipher"
anchor_role_id = "<role_id>"
anchor_secret = "credential:arc-accounts-anchor"
```

The CA file's sha256 is pinned inside the signed config. A different CA file is
refused even when its TLS chain is valid. Add `LoadCredentialEncrypted=` lines
for the four credentials to the `arc.service` unit.

Restart (`arc restart`), then open Settings > People. The first account is
created with the setup code in the server log, as before.

## Failure answers

| Symptom | Cause |
|---|---|
| People tab 503, log `accounts: [security.accounts] is not configured` | block missing |
| 503, `AuthorityConfigError` | config edited, signed by another key, or older than the anchored revision: run `--rotate` |
| 503, `VaultLeaseError` | grant expired or forged, CA changed, token has extra policies, wrong role, or wrong secret ID |
| 503, `AnchorUnavailableError` | Vault down, or the `users` record was reset or rolled back: investigate before anything else |
| Account change refused, audit error | the operator chain could not append; fix the disk, no change was made |

## Notes for reviewers

- Account mutations are audited twice: the account store writes `users.change`
  to the operator chain with the serving deployment as actor (derived from the
  operator public key under the tenant org), and arcui writes `ui.mutation` with
  the signed-in human.
- Re-opening the authority after a lease error is the only recovery path. An
  anchor error never re-opens it, because a fresh process would forget the
  highest version it saw and weaken rollback detection.
