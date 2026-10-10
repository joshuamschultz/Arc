# App principals

An app principal is an outside app with its own identity in Arc. It is not a person. It is not the operator token. It has its own key, its own scopes, and its own off switch.

Use one when a script, an integration, or a SaaS app must call the Arc dashboard API. The app gets only the scopes and agents it needs. You can revoke it alone. Nothing else breaks.

## What a principal holds

| Field | Meaning |
|---|---|
| DID | `did:arc:<tenant>:app/<hash>`. Derived from the app's own Ed25519 public key. |
| Scopes | A closed list. Each scope is bound to named agent DIDs. |
| Subjects | The people the app may act for. Empty means none. |
| Expiry | Optional at personal. Required at enterprise and federal. |
| Status | `pending`, `active`, or `revoked`. |
| Secret | A bearer token. Shown once. Arc keeps only its SHA-256 digest. |

Principals live in the account authority (Vault or OpenBao). That is the same sealed, anchored store that holds people. See [OpenBao account authority](../runbooks/operate/openbao-accounts.md).

## Scopes

There are ten scopes. Nothing else is accepted. An unknown scope is refused with HTTP 400.

An app can reach only the routes in one table. Today that table has eight rows.

| Scope | Route rows today | Status |
|---|---|---|
| `workflows:run` | `POST /api/workflows/{id}/run` | Reachable now |
| `runs:read` | `GET /api/workflow-runs/{id}`, `GET /api/runs/{run_id}/timeline` | Reachable now |
| `gates:resolve` | `POST /api/workflow-tasks/{id}/gate` | Reachable now |
| `tasks:write` | `POST /api/team/tasks`, `POST /api/tasks/{id}/approve`, `POST /api/tasks/{id}/reject` | Reachable now |
| `inbox:send` | `POST /api/agents/{agent}/inbox` | Reachable now |
| `runs:start` | none yet | Arrives with SPEC-088 |
| `workflows:event` | none yet | Arrives with SPEC-088 |
| `events:subscribe` | none yet | Arrives with SPEC-088 |
| `memory:write` | none yet | Arrives with a later spec |
| `feedback:write` | none yet | Arrives with a later spec |

You can issue the five later scopes now. They do nothing until their routes ship. Later specs add rows. They never add new scope kinds.

Two groups matter for the rules below:

- Decision scopes: `gates:resolve` and `tasks:write`.
- Read scopes: `runs:read` and `events:subscribe`.

A call passes only when the app holds the route's scope and the target agent is bound to that scope. No agent named means refused.

## The tier dial

Tier changes how strict Arc is. It does not turn the feature off. The same code runs at every tier.

| | Personal | Enterprise | Federal |
|---|---|---|---|
| Issue | Active at once | Pending until an operator approves | Pending until a second operator approves |
| Wildcard agent or subject (`*`) | Allowed | Refused | Refused |
| Expiry | Optional | Required, at most 90 days | Required, at most 30 days |
| Signed requests | Bearer, or signed (not required) | Bearer, or signed if `require_signed_requests = true` | Signed always. Bearer is refused |
| Two-person approval | No | No | Yes. The requester (a signed-in operator) cannot approve. The CLI cannot issue |
| Person delegation for decisions | Not used | Not used | Required |
| Decisions on the static operator token | Allowed | Allowed now, refused later (see rollout) | Refused |

Config lives in `[ui.principals]`:

```toml
[ui.principals]
require_signed_requests = false   # enterprise only; federal forces true
require_named_decider   = false   # enterprise; federal forces true; ignored at personal
rate_per_minute         = 600     # per principal
```

The tier itself comes from your security tier setting. This table cannot loosen it.

### Rollout of `require_named_decider`

A decision is a gate, human input, task review, or approval. If the caller is the static operator token, no person is named.

- This release at enterprise: the decision is allowed. Arc records the session pseudo-DID and writes an `operator_token_decision` warning audit event. The dashboard shows a banner that asks you to create named operator accounts.
- Next release at enterprise: the default flips to `true`. These decisions are refused with 403 `operator_account_required`. You can still set it to `false` on purpose.
- Federal: refused now. The setting is forced `true`.
- Personal: always allowed.

You can set `require_named_decider = true` today. Do it early.

## Bearer or signed requests

### Bearer

Send the token you got at issue time:

```
Authorization: Bearer arcp_<id>.<secret>
```

Use TLS. A stolen bearer token works until you revoke it. Below federal, Arc accepts that risk.

### Signed

The app signs each request with its Ed25519 private key. Arc checks the signature with the public key on file. A signed request is tied to its method, path, query, body, and on-behalf-of value. A captured request cannot be replayed on another route.

You send these headers:

| Header | Value |
|---|---|
| `X-Arc-Principal` | The app DID |
| `X-Arc-Nonce` | Random, base64url, at least 16 bytes |
| `X-Arc-Timestamp` | ISO 8601 in UTC, such as `2026-10-10T14:03:00+00:00` |
| `X-Arc-Signature` | Base64 Ed25519 signature |
| `X-Arc-On-Behalf-Of` | Optional. A person. Part of the signed data |

The signature covers this JSON, in canonical form: `method`, `path`, `query`, `body_sha256`, `nonce`, `ts`, `on_behalf_of`.

Rules Arc enforces:

- The timestamp must be at most 300 seconds old and at most 30 seconds ahead.
- Each nonce works once inside that window. A reused nonce is refused.
- The DID in the header must match the stored record and its key.
- A revoked, pending, or expired principal is refused before any crypto runs.

### Worked example

`arctrust.principal_auth.sign_request` is the reference signer. It returns the header dict. Pass the same `path`, `query`, and `body` bytes you send. `query` has no leading `?`.

```python
import json

from arctrust.keypair import generate_keypair
from arctrust.principal_auth import sign_request

keypair = generate_keypair()  # keep private_key in your secret store
print("public key (hex):", keypair.public_key.hex())  # give this to `arc principal issue`

app_did = "did:arc:acme:app/0123abcd"  # the DID printed by `arc principal issue`
body = json.dumps({"inputs": {"ticket": "T-42"}}).encode()

headers = sign_request(
    keypair.private_key,
    principal_did=app_did,
    method="POST",
    path="/api/workflows/triage/run",
    query="",
    body=body,
    on_behalf_of="ana@example.com",  # or None
)
print(sorted(headers))
```

Then send those headers with the same body bytes:

```python
import httpx

httpx.post(
    "https://arc.example.com/api/workflows/triage/run",
    content=body,
    headers={**headers, "Content-Type": "application/json"},
)
```

Do not re-serialize the body after you sign it. One changed byte breaks the signature.

## Acting for a person

An app can act for a signed-in person. Send `X-Arc-On-Behalf-Of` with the person's email or user DID.

Arc checks:

1. The person exists and is enabled in the account authority.
2. The person is in the app's subject allowlist.

Any failure is the same 403 `on_behalf_of_denied`. The app cannot learn which people exist.

### The intersection rule

The app adds no authority. The call may do only what both sides allow:

- the app's scopes and agent bindings, and
- the person's own roles and approver entries.

Records name the person as the actor and the app as the app. A gate decided this way shows the person as decider.

## Person delegations (federal)

At federal, an app that resolves a gate or reviews a task for a person needs a live delegation. The person makes it from their own signed-in session. The app cannot.

| Route | Use |
|---|---|
| `POST /api/me/delegations` | Body: `principal_did`, `scopes` (decision scopes only), `ttl_seconds` (1 to 28800) |
| `GET /api/me/delegations` | The person's own delegations |
| `POST /api/me/delegations/{id}/revoke` | Revoke one now |

A delegation lasts at most 8 hours. A person can manage only their own. Static tokens and apps cannot call these routes. Without a live delegation the app gets 403 `delegation_required`.

At personal and enterprise, delegations are optional and are not used to decide access.

## Revocation and rollback protection

Revoke a principal and its next request fails. There is no cache and no grace period. Arc reads the record on every request.

The principal store is sealed and anchored, like the user store. If someone restores an older copy of the file, Arc detects the rollback and refuses every principal request. A revoked app cannot be brought back by file edits.

If the account authority cannot be reached, Arc answers 503. It never falls back to a cached or static credential. Human sessions are not affected.

## What apps can never do

These limits hold at every tier, whatever scopes the app holds (decision D-826):

- Reach any route that is not in the app table. Even read-only routes are refused with 403 `route_not_allowed`.
- Touch approvals, standing grants, principals, users, trust, or signing routes. These prefixes are hard-denied: `/api/approvals`, `/api/standing-grants`, `/api/principals`, `/api/users`, `/api/trust`, `/api/me`, and any path with a segment ending in `signature`.
- Resolve an Arc approval. An approval click relayed by an app would travel on a channel that can be forged.
- Issue, approve, or revoke principals, or create delegations.
- Use a wildcard binding at enterprise or federal.
- Send more than `rate_per_minute` requests in a minute. Extra calls get 429.

## Audit

Every request from an app is audited, allowed or refused. Arc binds a causal root to the request with initiator `app`. Every audit event written while serving it carries that root.

| Field | Meaning |
|---|---|
| `actor_did` | Who acted. The person when acting for one, else the app |
| `on_behalf_of` | The person DID, when present |
| `via_app` | The app DID, when the call came through an app |
| causal initiator | `app`, with the app DID as the initiator id |

Principal changes write `principal.request`, `principal.activate`, `principal.revoke`, and `principal.delegation.*` events. Refusals write a `ui.mutation` row with the error code. Secrets and signatures are never logged.

Mail sent for a person carries `on_behalf_of` and `via` in its signed metadata. The inbox shows "from person via app".

## Related

- [App principals runbook](../runbooks/operate/app-principals.md): issue, approve, revoke, rotate.
- [OpenBao account authority](../runbooks/operate/openbao-accounts.md): the store behind principals.
