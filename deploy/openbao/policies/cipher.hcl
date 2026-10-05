# Policy arc-<tenant>-<deployment>-cipher. Replace @NS@ with arc-<tenant>-<deployment>.
# Seals and opens the account record under the single account-seal key.
path "@NS@-transit/keys/account-seal"    { capabilities = ["read"] }
path "@NS@-transit/encrypt/account-seal" { capabilities = ["update"] }
path "@NS@-transit/decrypt/account-seal" { capabilities = ["update"] }

# Token self-management (lease renewal and revocation).
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
