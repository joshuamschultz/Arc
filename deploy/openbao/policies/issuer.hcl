# Policy arc-<tenant>-<deployment>-issuer. Replace @NS@ with arc-<tenant>-<deployment>.
# Creates one non-exportable Ed25519 key per account (key name arc-user-<hex>) and
# signs with it. No read of other keys; no config, rotate, export or delete.
path "@NS@-transit/keys/arc-user-*" { capabilities = ["create", "read"] }
path "@NS@-transit/sign/arc-user-*" { capabilities = ["update"] }

# Token self-management (lease renewal and revocation).
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
