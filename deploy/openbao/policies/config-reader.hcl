# Policy arc-<tenant>-<deployment>-config-reader. Replace @NS@ with arc-<tenant>-<deployment>.
# Read-only view of the anchored authority-config head; cannot write or delete it.
path "@NS@-anchor/data/config"     { capabilities = ["read"] }
path "@NS@-anchor/metadata/config" { capabilities = ["read"] }
path "@NS@-anchor/config"          { capabilities = ["read"] }

# Token self-management (lease renewal and revocation).
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
