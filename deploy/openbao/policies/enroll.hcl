# Policy for the short-lived admin token piped to `arc accounts enroll`.
# Replace @NS@ with arc-<tenant>-<deployment>. Writes the config head once per
# revision (CAS) and checks whether the users and principals anchors exist;
# nothing else.
path "@NS@-anchor/data/config"         { capabilities = ["create", "read", "update"] }
path "@NS@-anchor/metadata/config"     { capabilities = ["read"] }
path "@NS@-anchor/metadata/users"      { capabilities = ["read"] }
path "@NS@-anchor/metadata/principals" { capabilities = ["read"] }
path "@NS@-anchor/config"              { capabilities = ["read"] }
path "@NS@-anchor/delete/config"       { capabilities = ["deny"] }
path "@NS@-anchor/destroy/config"      { capabilities = ["deny"] }
