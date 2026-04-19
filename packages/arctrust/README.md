# arctrust

Ed25519 trust-store primitives shared across Arc packages.

Provides the authoritative source for loading and caching operator and
manifest-issuer Ed25519 public keys from TOML trust files at
`~/.arc/trust/`.

## Usage

```python
from arctrust import load_operator_pubkey, load_issuer_pubkey, TrustStoreError

pubkey = load_operator_pubkey("did:arc:org:operator/abc123")
```

## Security

- Files must have 0600 permissions (fail on group/other-readable)
- Keys are base64-encoded 32-byte Ed25519 pubkeys
- 60-second TTL cache; explicit invalidation via `invalidate_cache()`
