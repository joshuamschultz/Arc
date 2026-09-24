# Account authority WIP checkpoint

> **Superseded status notice — 2026-09-23:** This remains a historical implementation and verification record. Local consolidation is complete at code checkpoint `06b80bad` and integration commit `d6b34f48` on Arc main; local branch/worktree cleanup is verified. Continue from the [business reliability consolidation handoff](../../docs/design/business-reliability-consolidation.md). Branch-isolation and do-not-merge/resume instructions below are historical. No deployment is claimed.

Branch: `codex/reliability-account-authority`. This checkpoint is **not merge ready**.
The copied uncommitted `deploy/entrypoint.sh` and `deploy/systemd/arc.service`
are excluded from this commit. Main must not be changed by this branch yet.

## Scope captured

- Secret-free signed deployment config bound to independently supplied Ed25519
  verification key, tenant/deployment identity, and monotonic config head.
- Signed scoped capability grant with deterministic per-deployment Vault policy
  names; injected host credential provider and short-lived TLS Vault client with
  lookup, renewal, and revoke-self.
- Account authority factory over existing Vault Transit, cipher, and KV CAS
  adapters, plus injected actor verifier and strict audit sink.
- Neutral `ByteCipher` protocol replacing the duplicate user snapshot shape.
- Narrow `WormSink.write_durable` append method and optional strict user mutation
  audit path. Existing general `write` remains fail-open; default user wiring
  remains unchanged.
- Unit and disposable Vault integration tests added.

## Verification at checkpoint

- Worktree `.venv` imports `arctrust` from this isolated worktree (verified in
  prior session).
- New focused unit suite: 16 passed.
- Disposable real Vault test was run twice. First exposed missing self-token
  ACLs; fixture policies were amended. Second progressed through account
  creation and real renewal, then failed after restart because Docker changed
  the random published port while signed config retained the old URL. Fixture
  was just amended to pin a host port; **not rerun** at this checkpoint.
- Ruff check of touched files currently fails with 8 findings (mostly import
  order, root `__all__`, two long lines). Mypy and full package suite not run.
- `git diff --check` passed.

## Required follow-up before review or integration

1. Rerun disposable Vault authority test after fixed-port fixture change;
   address failures and add actual token expiry and cross-tenant abuse cases.
2. Resolve Ruff/Mypy and run focused and full `arctrust` tests. Verify other
   packages importing the removed `UserSnapshotCipher` shape.
3. Review strict audit under partial write, sync failure, rotation failure,
   crash between anchor CAS/local file/final audit, and uncertain append on
   reopen. The present implementation is provisional; in particular, the
   existing WORM startup recovery/rotation path has not been hardened or
   independently witnessed here.
4. Review lease concurrency, renewal/revocation races, exception redaction,
   and caller ability to access the internal HTTP client. No raw token is
   intentionally returned, logged, or sent to a subprocess by production code.
5. Review config head enrollment/rollback procedure and ACLs. The signed
   config alone is never a trust root. Trusted verification keys and config
   anchor must be supplied independently.
6. Review actor verifier contract end-to-end: actor DID is attribution only;
   no production identity implementation or route wiring is included.
7. Obtain real DGX trusted host identity enrollment, scoped token issuance,
   config/grant signing authority, config anchor provisioning, and audit
   signer/sink custody before production composition. Federal P-256/FIPS
   provider coverage remains an explicit gap (current Vault Transit adapter
   issues Ed25519 user keys).
8. Gateway, CLI, eager UI, lazy agent, queue/skill wiring, deployment config,
   and default auth integration are intentionally out of this slice. Keep the
   branch isolated until composition and those consumers are reviewed.
