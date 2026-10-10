# App principals: connect an app

> **For** Operators who issue app access, and developers who build the app.
> Background: [App principals](../../concepts/app-principals.md).

This page shows how to give an app access to Arc, check it, and take it away.

You need:

- A configured account authority. See [OpenBao account authority](openbao-accounts.md).
- A signed-in operator account, or the `arc` CLI on the Arc host.
- The app's Ed25519 public key as 64 hex characters in a file.
- The DIDs of the agents the app may touch.

## 1. Make a key (app side)

```python
from arctrust.keypair import generate_keypair

keypair = generate_keypair()
open("app-public-key.hex", "w").write(keypair.public_key.hex())
# Store keypair.private_key in your secret store. Arc never sees it.
```

Give the operator only `app-public-key.hex`.

## 2. Issue

```bash
arc principal issue \
  --name billing-bot \
  --public-key-file app-public-key.hex \
  --scope runs:read=<agent-did> \
  --scope workflows:run=<agent-did> \
  --subject ana@example.com \
  --expires 30d
```

Flags:

| Flag | Meaning |
|---|---|
| `--name` | Display name. Required. Up to 64 characters |
| `--public-key-file` | Hex Ed25519 public key. Required |
| `--scope SCOPE=AGENT_DID[,AGENT_DID]` | Required. Repeat for more scopes |
| `--subject EMAIL\|DID` | A person the app may act for. Repeat. Must be a known user |
| `--expires Nd` | Lifetime in days, such as `30d` |

The command prints the principal ID, the DID, and the bearer token. Copy the token now. Arc shows it once and keeps only its digest.

You can do the same in the dashboard under Settings, Apps. Use `arc principal list` and `arc principal show <id>` to inspect.

Tier limits apply:

- Enterprise: `--expires` required, at most `90d`. No `*` agents or subjects.
- Federal: the CLI refuses to issue. Issue in the dashboard under Settings, Apps, signed in as an operator. `--expires` at most `30d`. No `*` agents or subjects.

## 3. Approve (enterprise and federal)

At personal, the principal is active at once. Skip this step.

At enterprise, `arc principal issue` prints `Status: pending` and an approval ID. At federal, the dashboard issue shows the pending approval:

```bash
arc approve <approval-id>
```

- Enterprise: an operator runs `arc approve`.
- Federal: the CLI refuses. A second operator, not the requester, must approve it in the dashboard. The requester must be a signed-in operator account. A request made with the deployment key names no person, so Arc refuses to approve it.

Approval fails if the record changed after the request. Issue a new one instead.

Denying the approval revokes the pending record.

## 4. Call Arc

At personal and enterprise, send the bearer token:

```bash
curl -H "Authorization: Bearer arcp_<id>.<secret>" \
  https://arc.example.com/api/workflow-runs/<run-id>
```

At federal, or at enterprise with `require_signed_requests = true`, sign each request. See the [worked example](../../concepts/app-principals.md#worked-example).

To act for a person, add `X-Arc-On-Behalf-Of: ana@example.com`. The person must be in `--subject`.

At federal, that person must first create a delegation for decision scopes. See [Person delegations](../../concepts/app-principals.md#person-delegations-federal).

## 5. Check use

```bash
arc principal show <id>
```

It shows status, scopes, agents, subjects, issuer, approver, expiry, and last use. It never shows the secret.

## 6. Revoke

```bash
arc principal revoke <id>
```

The next request with that credential is refused. Dashboard users can revoke in Settings, Apps.

## 7. Rotate

There is no in-place rotation. Replace the principal:

1. Make a new key. Issue a new principal with the same scopes.
2. Approve it if your tier needs that.
3. Deploy the new token or key to the app.
4. Check `arc principal show <new-id>` shows a recent last use.
5. Revoke the old one: `arc principal revoke <old-id>`.

Rotate before the expiry date. An expired principal is refused.

## Errors

Every refusal is audited. The body is `{"error": "<code>"}`.

| Status | Code | Meaning | Fix |
|---|---|---|---|
| 401 | `invalid_principal` | Credential is unknown, pending, revoked, expired, malformed, replayed, or has a bad signature. Arc gives one code for all of these. The audit log has the reason | Check the token and its status. At federal, sign the request. Fix the clock. Use a fresh nonce |
| 403 | `route_not_allowed` | The route is not in the app table, or it is hard-denied | Use a supported route. Apps cannot reach admin routes |
| 403 | `scope_denied` | The app lacks the scope, or the agent is not bound to it | Check `arc principal show`. Issue a new principal with the scope |
| 403 | `on_behalf_of_denied` | The person is unknown, disabled, or not in the allowlist | Add the person with `--subject` on a new principal |
| 403 | `delegation_required` | Federal. No live delegation for this app and scope | Ask the person to create one in their session |
| 403 | `operator_account_required` | A decision arrived with no named person where the tier needs one | Use a named account, or send `X-Arc-On-Behalf-Of` |
| 403 | `second_operator_required` | Federal. The approver is the requester | A different operator approves |
| 403 | `named_requester_required` | Federal. The request was not made by a live operator account | Issue again in the dashboard, signed in |
| 429 | `rate_limited` | Over `rate_per_minute` | Slow down, or raise `[ui.principals] rate_per_minute` |
| 503 | `account_authority_unavailable` | Arc cannot reach the account authority. It fails closed | Restore Vault or OpenBao. Retry |
