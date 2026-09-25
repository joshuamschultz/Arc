# Hosted queue broker wire v1

This is the single queue recovery wire contract shared by the Arc client and the
Arc Cloud authority service. The broker owns the protected queue record. The
customer machine never receives a Vault token. All endpoints require mutual TLS;
the broker obtains the client certificate fingerprint from its TLS transport,
never from a request header. Server certificate validation and a signed release
pin protect the client side. A signed machine operation is still required for
each call. TLS and operation signatures are independent proofs.

## Canonical encoding

Canonical JSON is `arctrust.canonical_json` (UTF-8, sorted keys, compact separators).
Ed25519 signatures are lowercase hex encoding of 64 bytes. A signed envelope is
`{"facts": <model>, "signature": <hex>}` with no extra keys. Epochs on the wire are
canonical decimal strings matching `^[1-9][0-9]{0,18}$`; there are no leading
zeros. Digests and certificate/public-key fingerprints are lowercase SHA-256 or
32-byte Ed25519 public-key hex as specified below. All signed payloads include
`version: 1`, reject unknown fields, and use whole-second Unix UTC timestamps.

Each call supplies `X-Arc-Machine-Operation`, base64url without padding of the
canonical JSON of `MachineOperation`. Its fields are `version=1`, `tenant_id`,
`machine_id`, `lease_id`, `lease_epoch` (integer active owner epoch),
`tls_fingerprint` (SHA-256 of the peer DER certificate), `purpose`, `method`,
`path`, `body_sha256`, `sequence`, `nonce`, `issued_at`, `signature`. The signature
covers `b"arc:machine-broker-operation:v1\0" + canonical_json(fields without
signature)`. The nonce is at least 256 bits of browser-safe randomness. The
broker checks `abs(now-issued_at)<=30`, exact method/path/body digest, lease,
key, certificate, tenant, purpose, and current paid/provider machine binding.
For POST, `body_sha256` hashes the exact canonical JSON request bytes. For GET,
it hashes canonical JSON of validated query fields. A head GET uses
`sequence=0` and consumes its fresh signed nonce in a bounded replay set in
the same protected record by physical CAS; it does not change the logical head
or mutation sequence. Duplicate nonces are refused. When the set is full, the
broker refuses new reads until an entry expires rather than evicting a live
nonce. A lost GET response is retried with a new signed nonce. POST uses the current `next_sequence` and
advances it in the same protected CAS as the queue root. A lost POST response is
reconciled by a signed GET and exact intended head comparison.

The signed lease envelope has facts `version`, `tenant_id`, `journal_scope`,
`machine_id`, `machine_public_key` (32-byte Ed25519 hex), `tls_fingerprint`,
`lease_id`, `active_owner_epoch`, `fenced_through_epoch`, `issued_at`,
`expires_at`, `next_sequence`, and `allowed_purposes` (exactly
`anchor.read`, `anchor.advance`, `queue.recover`). The broker issuer signs
`b"arc:broker-queue-lease:v1\0" + canonical_json(facts)`. The client verifies
with the broker issuer public key pinned by the signed release. The lease
envelope is evidence, not authority by itself: every operation checks the
current protected lease and live paid/provider state. Renewal cannot add
operator or account-signing purpose.

The recovery proof envelope has facts `version`, `tenant_id`, `journal_scope`,
`prior_owner_epoch`, `active_owner_epoch`, `lease_id`, `purpose="queue.recover"`,
`nonce`, `issued_at`, `expires_at`. The broker issuer signs
`b"arc:broker-queue-recovery:v1\0" + canonical_json(facts)`. Its lifetime is
at most 60 seconds. The Arc client's `QueueRecoveryAuthority.validate` checks
signature, pinned key, time, purpose, tenant, journal scope, and prior epoch;
this is only preflight. Every recovery CAS rechecks current revocation and that
`prior_owner_epoch <= fenced_through_epoch < active_owner_epoch` in the same
protected record CAS as the root. A current/live owner cannot recover itself.

## Queue endpoints

`GET /broker/queue/head` query is exactly `tenant_id`, `journal_scope`, and
optional `prior_owner_epoch` (for a fresh recovery proof). The signed operation
uses purpose `anchor.read`, sequence zero, and hashes canonical JSON of this
query object. Response: `{"head": AnchorHead|null, "lease": signed_lease,
"recovery_proof": signed_recovery_proof|null}`. If the prior owner is not
fenced, the call is refused rather than returning an empty proof. A normal head
read omits `prior_owner_epoch` and receives null recovery proof.

`POST /broker/queue/cas` uses purpose `anchor.advance`, body
`{"tenant_id", "journal_scope", "expected_head": AnchorHead|null,
"digest": lowercase_sha256, "intent": string}`. The broker checks every field
of `expected_head` (scope, logical version, digest, previous digest, intent).
The protected record holds the full head, current lease, replay sequence, and
one-time bootstrap permission. A null head can be created only when that
permission exists and is consumed in this CAS. Response:
`{"head": AnchorHead, "lease": signed_lease}`.

`POST /broker/queue/recover-cas` uses purpose `queue.recover` and the same body
plus `prior_owner_epoch` and `recovery_proof` signed envelope. Response:
`{"head": AnchorHead, "lease": signed_lease, "recovery_proof":
signed_recovery_proof}`. The refreshed proof allows long bounded recovery
without extending a stale lease. If the previous proof expires before CAS,
the client gets a new proof via a signed head GET. `AnchorHead.version` is the
logical root version and advances exactly once for each root change. The Vault
KV physical version also advances for lease/nonce changes, but is never
exposed as the logical head version. Lease takeover or revocation therefore
defeats a stale root CAS even when the logical head is unchanged.

Refusal bodies are exactly `{"error":"authority_refused"}` (403),
`{"error":"authority_conflict"}` (409), or
`{"error":"authority_unavailable"}` (503). A 503 or transport loss is an
uncertain outcome; the client reads and compares the exact intended transition
before retrying. No secret, signed proof, email, or private key enters logs.

The protected record is in a CAS-required Vault KV v2 path with policy denying
delete, destroy, metadata rewrite/reset, mount configuration, and export. The
broker is the only client with that scoped capability. Queue root mutation,
lease revocation/renewal, and replay sequence are CAS writes of one record.
The broker must not derive authority from mutable SQLite order rows alone;
billing and provider checks are additional fresh gates.
