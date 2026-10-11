# Policy arc-<tenant>-<deployment>-anchor. Replace @NS@ with arc-<tenant>-<deployment>.
# The monotonic heads of the account records: users, app principals and the
# callback allowlist (KV v2, cas_required). Never deletes, destroys or undeletes
# a version.
path "@NS@-anchor/data/users"          { capabilities = ["create", "read", "update"] }
path "@NS@-anchor/metadata/users"      { capabilities = ["read"] }
path "@NS@-anchor/delete/users"        { capabilities = ["deny"] }
path "@NS@-anchor/destroy/users"       { capabilities = ["deny"] }
path "@NS@-anchor/undelete/users"      { capabilities = ["deny"] }
path "@NS@-anchor/data/principals"     { capabilities = ["create", "read", "update"] }
path "@NS@-anchor/metadata/principals" { capabilities = ["read"] }
path "@NS@-anchor/delete/principals"   { capabilities = ["deny"] }
path "@NS@-anchor/destroy/principals"  { capabilities = ["deny"] }
path "@NS@-anchor/undelete/principals" { capabilities = ["deny"] }
path "@NS@-anchor/data/callbacks"      { capabilities = ["create", "read", "update"] }
path "@NS@-anchor/metadata/callbacks"  { capabilities = ["read"] }
path "@NS@-anchor/delete/callbacks"    { capabilities = ["deny"] }
path "@NS@-anchor/destroy/callbacks"   { capabilities = ["deny"] }
path "@NS@-anchor/undelete/callbacks"  { capabilities = ["deny"] }
path "@NS@-anchor/config"              { capabilities = ["read"] }

# Token self-management (lease renewal and revocation).
path "auth/token/lookup-self" { capabilities = ["read"] }
path "auth/token/renew-self"  { capabilities = ["update"] }
path "auth/token/revoke-self" { capabilities = ["update"] }
