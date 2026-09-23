# Arc Cloud recovery research

## Scope and evidence status

This is a read-only source audit of the interrupted `arc-cloud` source, checked against the [approved integration plan](../business-reliability-integration-plan.md). It was completed **before** isolated candidate `0380dc0` existed; findings below describe the audited source, not that later candidate. Candidate `0380dc0` has 29 local tests plus lint, type, and site-build checks reported complete. Independent review is now complete with **HOLD MERGE**; fixes are assigned and not yet claimed landed here. This is not deployment or launch evidence.

Evidence labels distinguish code visible in the audited source, provider documentation, and inferences that still need fault injection or provider evidence. No provider account was queried during the source audit.

### Later candidate review: `0380dc0`

The independent review identified these open acceptance gaps in the candidate. They supplement, and do not change the temporal scope of, the earlier interrupted-source audit above:

- A completed checkout can lack the expected webhook/reconciliation path.
- Cancellation or past-due events arriving before local checkout-session attachment can be unmatched.
- Generic readiness cannot establish signed order, machine identity, or customer setup authority.
- The complete boot chain is not yet verified as immutable.
- Billing loss needs explicit admission control and lease revocation behavior.
- Uncertain provider-create outcomes need a bounded review state and resolution path.

The disposition is **HOLD MERGE** with fixes assigned. No fix completion, deployment, or launch is asserted.

## Findings and recovery checkpoints

### Checkout creation and order-name recovery

**Code finding:** `arc-cloud/provisioner/src/arccloud/main.py` persists an order, creates a Stripe Checkout Session using an idempotency key, then stores the returned Session ID. If the create succeeds remotely but the response is lost before that ID is stored, a completed-session webhook is rejected unless the stored ID already matches; the order may remain pending. Subdomain uniqueness has no pending-order expiry/release transition in `provisioner/src/arccloud/store.py`.

**Provider documentation:** Stripe returns the first result for a repeated request with the same idempotency key and parameters, including a `500`; keys may be pruned after they are at least 24 hours old. Checkout Session listing does not expose a `client_reference_id` filter. An open Session can be expired, after which it cannot complete. These points are documented at [Stripe idempotent requests](https://docs.stripe.com/api/idempotent_requests), [list Checkout Sessions](https://docs.stripe.com/api/checkout/sessions/list), and [expire a Checkout Session](https://docs.stripe.com/api/checkout/sessions/expire).

**Inference to test:** Stripe may have accepted checkout while the local order remains pending, leaving a chargeable Session and a reserved name. Test lost response, webhook-before-local-attachment, duplicate webhook, expiry, and recovery after the idempotency-key retention window. Recovery needs a durable uncertain checkout intent, reconciliation against verified order/payment/customer/session facts, and atomic release of a name only after no payable Session or subscription can claim it. A failed replay after key expiry is not proof that no Session exists.

### Hetzner machine creation and search

**Code finding:** `arc-cloud/provisioner/src/arccloud/provision.py` records a create attempt before the server POST and searches before retry. A missing machine for an uncertain create is parked as uncertain, avoiding an automatic second POST but potentially indefinitely. `provisioner/src/arccloud/hetzner.py` reads only one server-list page and does not verify the exact name while reconciling.

**Provider documentation:** The official [Hetzner Python server client](https://github.com/hetznercloud/hcloud-python/blob/main/hcloud/servers/client.py) documents project-unique names, preliminary server/action data from create, and list filters including name and label selector. Its pagination helper iterates pages. The official [Hetzner Go server API](https://github.com/hetznercloud/hcloud-go/blob/main/hcloud/server.go) and [client pagination helpers](https://github.com/hetznercloud/hcloud-go/blob/main/hcloud/hcloud.go) likewise distinguish paged list calls from all-page helpers.

The reviewed sources do **not** document a create idempotency key or a maximum delay before an accepted machine appears in listings. Project-scoped name uniqueness is not evidence of idempotent create responses or immediate list visibility.

**Inference to test:** A provider may accept create while the client loses the response and an early or partial listing finds nothing. Test delayed visibility, all-page search, wrong name/labels/attempt, uniqueness conflict, and crash after provider acceptance. Keep an inconclusive attempt uncertain; require exact stable-name and immutable-label reconciliation before any replacement create.

### Worker leases and external side effects

**Code finding:** `provision.py` runs bounded batches of four and renews leases separately from the per-order task. If renewal returns false, the task can continue. Store writes are fenced, but already-submitted provider operations cannot be undone by a later database lease check. The lease is checked before DNS, not as provider-side fencing for every external action.

**Inference to test:** A lease may expire while a provider request or DNS write is in flight, allowing a replacement worker to act while the previous worker is still active. Couple lease loss to cancellation and awaiting of that order task; isolate one order's failure from sibling work; recheck ownership before each new external mutation; reconcile effects already in flight by stable attempt identity. Test false/raising renewal, cancellation during create, late provider response, DNS races, and sibling survival. Do not treat local lease fencing as provider fencing.

### Customer claim, boot trust, readiness, and setup

**Code finding:** The email challenge in `provisioner/src/arccloud/store.py` authorizes order viewing; `site/src/pages/ready.astro` then links to the machine's `/setup`. The inspected path has no signed deployment grant binding the customer claim to machine, domain, image, and order. Provisioning marks one `ready` state after `/api/ready`; subscription updates can also set `ready` without that probe. The Arc image is digest-pinned, while the audited cloud-init still references mutable provider OS, Caddy, and Docker-install inputs.

**Approved design:** Follow the [integration plan](../business-reliability-integration-plan.md): pinned signed Arc image with issuer public key; fresh first-boot machine key; bounded HTTPS challenge; independent email-verified customer claim; Vault-issuer-signed scoped grant bound to nonce, expiry, deployment digest, machine, customer, and order; no reusable Vault token in userdata; narrow renewable deployment identity with epoch fencing; and distinct `awaiting_setup` and capability-readiness states. This is an application machine-identity protocol, not remote attestation.

**Provider guarantee and limit:** Vault documents that renewal fails for nonrenewable, revoked, or max-TTL tokens and that response-wrapped credentials are one-use. See [token renewal](https://developer.hashicorp.com/vault/docs/commands/token/renew), [response wrapping](https://developer.hashicorp.com/vault/docs/concepts/response-wrapping), and [Vault Agent AppRole pattern](https://developer.hashicorp.com/validated-patterns/vault/vault-agent-approle). These documents do not implement Arc's signed-grant protocol or prove its deployment composition.

**Inference to test:** A verified email link alone may expose setup for a machine not cryptographically bound to that paid order. Exercise nonce replay/expiry, order/customer/machine/domain/image substitution, redirects, private-address and rebinding destinations, slow/oversized challenge responses, old-epoch renewal, and subscription updates that arrive before readiness. Verify denial and audit outcomes. Pin and verify the complete boot chain, not only the Arc container reference.

## Separate Vault and DGX evidence

- Isolated auth branch `204ff830`: reported evidence is **two actual-provider Vault tests** and **24 seam tests**. This is branch-level test evidence; it does not establish a configured DGX Vault integration or production custody.
- DGX inspection was read-only. No `vault`, `p11tool`, or `pkcs11-tool` binaries and no `Vault`, `VaultAgent`, `tpm2-abrmd`, or `pcscd` units were found. `tpm2_getcap`, `systemd-creds`, and `systemd-cryptenroll` commands exist. Their presence does not prove that machine identity is configured, TPM-backed, or connected to an external Vault.
- No secrets or configuration contents were read or reproduced, and no runtime state was mutated.

## Next evidence to collect

1. Complete the assigned candidate fixes for checkout/webhook reconciliation, unmatched subscription events, readiness authority, immutable boot inputs, billing-loss admission/lease fencing, and bounded uncertain-create review; preserve the HOLD MERGE disposition until independently reviewed.
2. Add deterministic tests around checkout and machine-create response loss, webhook ordering, full paginated provider reconciliation, lease loss during external effects, and per-order worker isolation.
3. Verify machine/customer grant binding and restart renewal against the actual issuer/Vault composition, with replay, expiry, epoch, scope, and destination-abuse cases.
4. Test separate billing, infrastructure/readiness, and `awaiting_setup` transitions, including subscription events that arrive out of order; verify the approved data-retention behavior on cancellation.
5. Keep local checks, independent review, deployment, and launch as separate statuses. The research audit itself performed no deployment or provider mutation.
