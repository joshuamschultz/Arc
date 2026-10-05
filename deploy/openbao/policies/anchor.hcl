# Policy arc-<tenant>-<deployment>-anchor. Replace @NS@ with arc-<tenant>-<deployment>.
# The monotonic head of the account record (KV v2, cas_required). Never deletes,
# destroys or undeletes a version.
path "@NS@-anchor/data/users"     { capabilities = ["create", "read", "update"] }
path "@NS@-anchor/metadata/users" { capabilities = ["read"] }
path "@NS@-anchor/config"         { capabilities = ["read"] }
path "@NS@-anchor/delete/users"   { capabilities = ["deny"] }
path "@NS@-anchor/destroy/users"  { capabilities = ["deny"] }
path "@NS@-anchor/undelete/users" { capabilities = ["deny"] }

# Token self-management (lease renewal and revocation).
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
