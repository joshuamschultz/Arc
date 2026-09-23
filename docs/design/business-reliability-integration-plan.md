# Business reliability integration plan

Status: approved design decisions and open acceptance work. This plan does not claim the target composition or its acceptance evidence is complete. Implementation status remains in the [business reliability execution ledger](business-reliability-execution.md).

## Purpose and boundaries

Integrate durable accepted work, hosted queue operations, signed skill revisions, deployment trust, and paid provisioning through the existing public package seams. Keep `arc-cloud` separate from Arc. Keep durable run ownership in ArcAgent, persistence/blob mechanics in ArcStore, model-call admission in ArcLLM, loop execution in ArcRun, cryptographic custody in ArcTrust, and presentation/control in ArcUI and ArcCLI.

The implementation must preserve the package dependency direction. ArcAgent consumes ArcRun; ArcRun consumes ArcLLM; ArcStore and ArcTrust remain public adjacent seams. Higher-level composition supplies capabilities through factories before the agent lifecycle or model construction needs them.

## Approved run-intent and accepted-work design

### Ownership, identity, and states

ArcAgent owns a typed `RunIntent` for each accepted run. It binds the request/run ID, tenant, agent DID, session, owner epoch, monotonic version, deadline, and signed authorization evidence. The authorization evidence is re-evaluated when a previously accepted run is resumed; persisted acceptance does not grant permanent authority.

The durable lifecycle is:

`staging → accepted → executing → completed | failed | outcome_unknown`

State transitions are versioned and fenced by the owner epoch. A run is `failed` only when failure is known. A crash after execution begins leaves the outcome uncertain; recovery records `outcome_unknown` and never blindly replays provider or tool effects.

### Storage and authenticated enumeration

The request and result bodies are encrypted, content-addressed blobs written through a public typed ArcStore contract. ArcStore owns persistence and blob lifecycle; ArcAgent owns run intent, authorization, execution, and state transitions. The contract must not require consumers to reach into ArcStore tables or private modules. The repository currently has `RunStore` mutable-plane primitives and operational `SpoolRecord` APIs, but no public typed accepted-run/blob contract at the root `arcstore` facade. Defining and exporting that adjacent seam is an integration checkpoint, not an existing capability.

Each agent has one bounded externally anchored manifest authenticating enumeration of that agent's reservations and intents. The manifest makes a missing database job detectable from its anchored record. Manifest size and entry limits are explicit, and terminal retention is bounded by bytes. Unfinished intents are never evicted. Quota accounting includes staging reservations, encrypted blobs, terminal retention, and crash-window orphans.

### Ordered transition protocol and recovery

1. Reserve staging capacity and record its authenticated reservation in the external anchor **before writing any request or result blob**. The reservation counts against all relevant capacity limits.
2. Write the encrypted content-addressed request blob through the ArcStore public contract, read it back, and verify its identity and content integrity.
3. Seal the intent and advance the external anchor to `accepted` before the database compare-and-set (CAS). Persist the corresponding intent transition by CAS.
4. Before any provider request or tool effect, advance the external anchor to `executing`, then persist that transition by database CAS.
5. Record known completion/failure and result reference through the same anchored-before-CAS order. If the process crashes after entering `executing`, recover to `outcome_unknown`; do not replay effects.

For every external-anchor advance followed by a lost response or database-CAS response, recovery reads the anchor and database and resolves exactly one transition: the prior state or the anchored final state. It may apply the single anchored final transition when the database is at the matching prior version. Divergence, a multi-transition gap, invalid intent, missing required content, or an unavailable anchor is a typed unavailable/fail-closed result. No path rolls the external anchor backward or treats a rollback as recovery authority.

Orphan collection and terminal retention share the same quota model. Collection may remove only blobs proven unreferenced by the anchored manifest, and unfinished reservations/intents remain protected. The implementation must define and test bounded cleanup behavior across each crash point; it must not infer that an unlisted local row is disposable when anchor state is unavailable.

### Resumption rules

Only an `accepted` run proven not to have entered `executing` can resume. Resume requires fresh identity, policy, signed-authorization freshness, delegation, tenant/agent scope, owner-epoch, and deadline checks. These checks authorize continuing accepted work; they do not convert an `executing` or `outcome_unknown` intent into permission to repeat provider/tool effects.

## Hosted queue composition and operator controls

ArcLLM's existing `CallQueueCoordinator`, `CallQueueStore`, `QueueJournal`, and `QueueLimits` provide provider-call admission and encrypted queue metadata/CAS foundations. The journal does not persist request/result bodies and does not own accepted agent runs. It therefore cannot substitute for the RunIntent lifecycle above.

Hosted startup requires a real durable queue composition. The ArcAgent factory must receive the initialized coordinator and trusted tenant before startup or model construction; the same initialized coordinator instance must back agent model calls and the authorized ArcUI/API/CLI control surfaces. Hosted mode fails startup/readiness if that required composition is absent. It must not fall back to `MemoryQueueStore` or silently create a second coordinator. Standalone ArcLLM may continue to use its standalone in-memory implementation.

ArcAgent currently exposes constructor injection and `set_queue_coordinator()` before cached model construction. ArcUI's `create_app()` accepts a queue coordinator, but the hosted agent factory and complete authorized API/CLI journey remain integration work. The exact public control contract, operator-visible states and configuration bounds must be specified before queue priority/budget controls are implemented; this plan does not approve their semantics.

Acceptance must show that a real hosted ArcAgent created through each composition path shares one durable coordinator with ArcUI/API/CLI; calls carry the actual tenant/agent/session/run/owner-epoch identity; restart recovers metadata without replaying uncertain effects; and control operations are authorized, audited, version-checked, and idempotent. Exercise queue saturation, cancellation requested versus confirmed, paused admission, stale owner, lost anchor/DB responses, and missing durable capability. Unit tests of the queue coordinator alone do not meet this gate.

## Signed skill activation

Skill activation uses a factory supplied before ArcAgent capability loading/lifecycle begins. The resolver produces immutable, complete signed bundles; activation records an externally anchored version and previous digest. The bytes verified at activation are the same bytes the runtime consumes. Runtime reload re-verifies the content and confirms the active anchor; it cannot silently load a mutable workspace replacement.

Rollback is a new authorized signed activation referring to a prior verified bundle, not deletion or rewind of the external anchor. The operation records actor, scope, new version, previous digest, target digest, and outcome in the audit path.

The current source has ArcTrust monotonic-anchor contracts and an ArcUI `skill_revision_anchor_factory` injection point; the anchored revision resolver/editor work is incomplete. Factory wiring through startup, signed full-bundle storage, activation/rollback, same-bytes consumption, and customer recovery remain acceptance gaps. Verify import, edit, reload, stale-anchor refusal, rollback, and crash windows in the actual agent factory, not only through route or resolver tests.

## Nonexportable account custody and deployment composition

Issuer signing, ciphertext sealing, and independent monotonic anchoring are separate injected capabilities. Account factories return scoped signing/cipher/anchor capabilities backed by nonexportable custody; callers do not receive reusable Vault tokens or raw private keys. ArcTrust already defines signer, `RecordCipher`, and monotonic-anchor seams, including Vault-backed cipher/anchor implementations. The application factories that compose the complete issuer + cipher + independent anchor set still need integration and real-provider evidence.

Two independent compositions are required:

1. **Self-hosted DGX:** build the operator/account capabilities from the actual self-hosted Vault configuration, wire them through ArcAgent/ArcUI/ArcCLI startup, and prove the resulting run, skill, and audit paths work after restart. Mocked Vault HTTP tests are useful contract tests but do not establish real deployment configuration, policy, or custody behavior.
2. **New Arc Cloud deployment:** provision the same capability set for each new machine from the cloud trust protocol below. Do not assume the self-hosted factory is implicitly available in a fresh hosted image.

For each composition, record the exact Vault policies/key metadata, public factory inputs and outputs, lifecycle owner, capability absence behavior, and recovery behavior. Acceptance evidence includes real Vault transit signing/sealing and KV CAS anchor operations, key nonexportability and denial of key export/delete, credential-free userdata, restart recovery, and proof that audit/signing failure refuses protected transitions. Destructive provider faults remain isolated to synthetic deployments.

## Arc Cloud trust and provisioning

Keep the website and provisioner in the separate `arc-cloud` repository. Preserve the $20/month hosted offer with bring-your-own-key (BYOK) model credentials. The product does not include unlimited model inference.

### Machine identity and first claim

The pinned, signed Arc image contains the issuer public key. On first boot, the machine generates a fresh ephemeral private key and presents a bounded challenge over HTTPS on port 443. The provisioner validates the paid order, provider machine, and requested domain, then validates the challenge using bounded timeouts, no redirects, and no SSRF-capable destination handling. The challenge binds the machine's ephemeral key to the order and domain. Email-verified customer initial claim is an independent proof; neither proof substitutes for the other.

After both validations, the Vault-held issuer signs a purpose-limited grant bound to a nonce, expiry, deployment digest, machine identity, customer identity, and paid order. No reusable Vault token is placed in cloud-init/userdata. The initial email claim may establish a narrowly scoped renewable deployment identity for unattended reboot. Renewal fences the prior epoch and cannot create operator identities or grant arbitrary operator signing authority. This is an application-level machine identity protocol; it is not remote attestation and must not be described as one.

Acceptance requires replay, stale nonce, expiry, order/customer/machine/domain substitution, DNS rebinding/private-address, redirect, oversized/slow challenge response, image-key substitution, grant-scope escalation, old-epoch renewal, and stolen userdata tests. Tests must establish which party validates each binding and what is audited when validation fails.

### Orders, workers, readiness, and cancellation

Arc Cloud remains responsible for checkout idempotency, payment webhook reconciliation, provider create reconciliation, DNS, email claim, and deployment status. A lost checkout/provider response is an uncertain operation to reconcile by stable order/attempt identity before retrying; it must not create a second charge or machine. Keep the bounded leased worker pool and explicit lease fencing.

Represent `awaiting_setup` separately from infrastructure/capability readiness. A successful `/api/ready` probe means required service capabilities pass; it does not mean the customer has claimed/configured the account, supplied a model credential, or completed the first-use journey. Report both states independently to the customer and provisioner.

Cancellation stops future billing/provisioning actions according to the subscription state but retains customer data under an explicit retention and access policy. The present Arc Cloud README describes deleting the server and hostname on cancellation; the approved design supersedes that behavior. Specify data retention duration, customer export/recovery path, and final deletion action before implementing cancellation. Do not claim retention guarantees until exercised against provider storage and backups.

Arc Cloud already has order-claim leases, create-attempt reconciliation, bounded worker batches, customer email challenges, and capability-readiness polling in `provisioner/src/arccloud/{store,provision,main}.py` and `site/src/pages/ready.astro`. Those are integration points, not evidence for the complete trust, setup-state, retention, or reconciliation acceptance above. Verify source and tests in the separate repository; changes here do not update Arc Cloud.

## Integration checkpoints and required evidence

| Checkpoint | Integration seam/source | Evidence required to close |
|---|---|---|
| Typed RunIntent and public content blobs | ArcAgent `core/agent.py`; ArcStore root `__init__.py`, mutable `runs.py`, backend contract | Public typed contract tests; request/result encryption and content integrity; cross-agent/tenant isolation; accepted work enumerated from one bounded anchored manifest; physically absent optional storage produces typed unavailable behavior. |
| Anchor-before-CAS protocol | ArcTrust `monotonic.py`, `vault_anchor.py`; ArcStore CAS backend | Fault injection after every anchor advance, DB CAS, blob write and response loss; prove exactly one prior/final recovery, detect missing jobs and multi-step divergence, and prove no rollback path. |
| Reserve, retention, and orphan quota | RunIntent store and blob seam | Capacity tests count reservations before writes, all incomplete/terminal bytes and crash-window orphans; unfinished work never evicted; bounded GC only reclaims provably unreferenced data. |
| Execution effect boundary | ArcAgent run entry and ArcRun model/tool dispatch | Persist `executing` before every provider/tool effect. Crash after each boundary yields `outcome_unknown` without blind replay; only proven-unexecuted accepted runs resume after fresh checks. |
| Hosted queue factory and controls | ArcAgent constructor/setter and lazy model creation; ArcUI `create_app`; ArcCLI UI/server construction; ArcLLM queue facade | Self-hosted and cloud agent-factory tests show one initialized durable coordinator shared with model/UI/API/CLI; required hosted dependency fails closed; no memory fallback; authorized/audited controls and uncertain cancellation states verified. |
| Skill activation lifecycle | ArcAgent skill resolver/load lifecycle; ArcTrust anchor factory; ArcUI revision routes | Factory is present before lifecycle; full immutable bundle signature and anchor version checked; runtime consumes verified bytes; edit/reload/stale-digest/authorized rollback-as-new-activation crash tests pass. |
| Account custody composition | ArcTrust signer/cipher/anchor; ArcAgent, ArcUI, ArcCLI account factories | Real self-hosted DGX composition and a separate Arc Cloud fresh-machine composition use nonexportable issuer, cipher, independent anchor; real-provider operations, denial paths, restarts and secret-free boot metadata demonstrated. |
| Cloud trust protocol | Arc Cloud `provisioner/src/arccloud/{main,store,provision,cloudinit}.py`; `site/src/pages/ready.astro` | Paid order + provider machine/domain + HTTPS challenge + independent customer email claim bind to signed scoped grant; all abuse cases above refused/audited; no remote-attestation claim. |
| Paid lifecycle | Arc Cloud checkout/webhook/store/worker, readiness and customer status UI | Idempotent uncertain payment/create reconciliation; $20/BYOK behavior; bounded leased workers; distinct `awaiting_setup` and readiness; cancellation preserves data per approved retention policy; customer completes fresh setup journey. |

The program is accepted only with evidence from both actual deployment compositions and customer entry points. Library tests, mocked Vault calls, a 200 health response, an ArcStore row, or a signed bundle in isolation do not close the corresponding checkpoint. Record the exact command/build, dependencies, test data class, failure injection point, observed state/audit evidence, and remaining limits in the execution ledger. Keep sustained deployment/soak proof separate from local code/test evidence.

## Existing seams and status references

- [Execution ledger](business-reliability-execution.md) — owners, current implementation state, verification, and outstanding deployment/soak work.
- [Business reliability program](business-reliability-program.md) — product outcome, repair ordering, security/recovery principles, and broad acceptance context.
- ArcLLM queue foundation: `packages/arcllm/src/arcllm/queue_control.py`, `queue_journal.py`, and public lazy exports in `packages/arcllm/src/arcllm/__init__.py`.
- Agent composition/run entry: `packages/arcagent/src/arcagent/core/agent.py`; ArcRun public queue facade: `packages/arcrun/src/arcrun`.
- ArcStore current persistence examples: `packages/arcstore/src/arcstore/runs.py`, `records.py`, and public exports in `__init__.py`.
- ArcTrust custody seams: `packages/arctrust/src/arctrust/monotonic.py`, `vault_anchor.py`, and `vault_cipher.py`.
- ArcUI composition hooks: `packages/arcui/src/arcui/server.py` and `routes/agent_detail/skills.py`; CLI server construction: `packages/arccli/src/arccli/commands/ui.py`.
- Separate cloud implementation references: `arc-cloud/provisioner/src/arccloud/` and `arc-cloud/site/src/pages/ready.astro`.
