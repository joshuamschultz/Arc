# Fleet layering and removable composition

ArcTeam is the optional fleet layer. It composes multiple standalone ArcAgents;
it is not part of an individual agent's nucleus. This page records the landed
alpha composition contract, including durable AgentMail delivery.

## The direction

```text
arcteam ──> arcagent     fleet lifecycle, inbox delivery, fleet tool/skill composition
    │
    └────> arcmemory     shared-knowledge governance through public knowledge seams

arcagent  -X-> arcteam
arcmemory -X-> arcteam
```

ArcTeam may use only each package's public, typed facade or an explicitly
published extension contract. It may not open an agent workspace, query an
ArcMemory SQLite file, import private modules, or retain an agent private key.
An ArcAgent remains runnable when ArcTeam is physically absent: membership,
mail, fleet tools, and shared-knowledge features report unavailable; identity,
local tools, skills, sessions, and local memory continue to work.

## Ownership at the seam

| Concern | ArcMemory | ArcTeam |
|---|---|---|
| Canonical documents, OKF validation, indexes, embedding, search, provenance, and revocation contracts | Owns generic mechanics and typed ports | Supplies a fleet-scoped backend only through those ports |
| Personal knowledge | Owns the local, agent-scoped implementation | Does not read or make it global |
| Shared knowledge | Validates the requested operation, provenance, classification, and result shape | Owns membership, promotion authorization, lifecycle, and the shared collection policy |
| Agents | Knows no agent framework | Owns the composition of independently runnable agents, not their LLM loop or private state |
| Tools and skills | Knows no fleet | Owns fleet-level installation, grants, routing, and teardown; each agent still verifies and authorizes its own capability use |
| Tasks, runs, and workflow coordination | Does not own them | Coordinates them over the ArcStore adapter; it does not create another task or index engine |

`arcmemory.adapters.shared_knowledge.SharedKnowledgeAdapter` is the generic
collection seam: save, read, search, and revoke remain explicit operations with
an explicit access record. ArcTeam now provides the fleet backend and promotion
service; roster and lifecycle policy do not enter ArcMemory.

## Composition lifecycle and shared tools

`FleetSharedKnowledgeComposition` owns the lifecycle for already-started
agents. `start()` attaches the `arcteam.shared_knowledge` extension only after
checking team membership, `reload()` atomically replaces one member's attachment,
and `stop()` detaches every tool the extension registered. The attachment is
bridged through ArcAgent's governed tool registry, so its tools still receive
schema validation, policy, classification/trifecta checks, and audit.

The attached fleet surface is deliberately small:

- `shared_knowledge_promote`
- `shared_knowledge_retrieve`
- `shared_knowledge_search`
- `shared_knowledge_revoke`

If ArcMemory's optional collection mechanics are absent, the fleet service
returns `SharedKnowledgeUnavailableError`; it does not synthesize a local
shared store. Without ArcTeam, none of these tools are attached and the agent's
local memory, tools, skills, identity, and sessions remain usable.

## Sessions, mail, and adapters

ArcGateway owns operator and external-channel sessions. A Telegram, Slack,
web, or operator-facing dashboard turn enters ArcGateway and is bound to that
surface's session identity. ArcTeam owns signed agent-to-agent mail. These are
separate planes: agent mail is delivered to an agent inbox, verified and
replay-checked before it can wake or steer a run, and has a sender-scoped agent
session key. Mail must never be treated as an operator session or a bypass of
the gateway authentication boundary.

`arcteam.StorageBackend` is the typed transport/persistence seam shared by
ArcTeam's registry, audit trail, and messenger. `NatsBackend` is its NATS
JetStream implementation; `MemoryBackend` is an in-process test/local
implementation. NATS is an adapter for signed fleet mail, not a store of
agent-private memory or an authorization source.

ArcStore is a separate adapter role: it is the durable task/run substrate used
for coordination and observability, and AgentMail can use its durable outbox
before NATS delivery. It is not ArcMemory's generic index/search engine. The
durable store is authoritative for a task transition; mail is a wakeup/narration
signal and cannot make a durable state change appear to have happened.

AgentMail is a public ArcTeam seam: it creates and signs an envelope, then uses
the ArcStore atomic inbox/outbox operation so participant inbox copies and the
transport record commit together. The PostgreSQL outbox is leased with
`SKIP LOCKED`, recovers expired leases after a crash, retries with bounded
backoff, and moves exhausted entries to a durable dead-letter state. ArcUI
starts the supervised `MailDeliveryWorker` for the application lifetime; a
send reports `sent` only after transport acknowledgement and `pending` when
durable delivery remains queued.

The envelope's `conversation_id` is the canonical cross-inbox identity. Each
participant still receives an access-controlled local thread/message copy, so
one participant cannot use another participant's local IDs to bypass policy.
The CLI and ArcUI use the same durable service, with sender signing resolved
from the selected agent identity. ArcUI mutations are operator-only and
audited; the operator signer is not accepted as an agent sender.

The durable path — sign first, then commit the inbox and the outbox in one
transaction, then let a leased worker deliver — is what makes a network blip
recoverable rather than a lost message:

```mermaid
sequenceDiagram
    autonumber
    participant S as "Sender agent"
    participant M as "AgentMailService.send"
    participant DB as "ArcStore (inbox + PostgresMailOutbox)"
    participant W as "MailDeliveryWorker"
    participant T as "NATS / memory backend"

    S->>M: MailSendRequest(to, body, conversation_id)
    M->>M: _sign_envelope (sign BEFORE persistence)
    M->>DB: atomic commit — participant inbox copies + outbox row
    Note over DB: row leased with SKIP LOCKED;<br/>expired leases reclaimed after a crash
    W->>DB: lease next outbox row
    W->>T: deliver signed envelope
    alt transport ack
        T-->>W: ack
        W->>DB: mark sent
    else transport failure
        W->>DB: keep pending (bounded backoff → dead-letter)
    end
```

Signing happens **before** persistence so an edit to a stored outbox row can
never become new, validly-signed mail; a send reports `sent` only after
transport acknowledgement, and `pending` while durable delivery is still queued.

## Zero-trust rules

Every fleet boundary keeps the four pillars intact:

- A sender, recipient, operator, and backend operation have scoped DIDs.
- Mail and loaded artifacts are verified before use; a message body is
  untrusted data, never fleet control-plane instruction.
- Membership, clearance, tool grants, promotion, and delivery are authorized
  at their relevant seam, with deny on failures.
- Send, verify, delivery, promotion, revoke, and denial emit audit evidence
  without persisting secrets or raw private keys.

An unreachable NATS server may make the local adapter unavailable or select the
documented in-memory local path; it must never turn an unsigned message into a
trusted one. A failed shared-knowledge authorization must not fall back to a
workspace or lower-clearance store.

## Alpha status and verification

Shared-knowledge composition is landed: ArcAgent exposes public extension
attach/detach operations, ArcTeam owns attachment lifecycle and fleet policy,
and ArcMemory stays optional collection mechanics. AgentMail's production
worker, PostgreSQL outbox/DLQ, ArcUI controls, and CLI parity are also landed.
Do not conflate these durable agent-mail records with ArcGateway session
history: they are separate planes and have separate authorization boundaries.

For the landed components, validate the seams with:

```bash
uv run pytest packages/arcteam/tests/unit packages/arcteam/tests/integration
uv run pytest packages/arcmemory/tests/architecture packages/arcmemory/tests/security
uv run pytest packages/arcagent/tests/architecture
uv run python scripts/run_adversarial_tests.py
```

Run the tests relevant to a changed implementation. Documentation changes can
be checked with `uv run mkdocs build --strict` and `git diff --check`.

---

## Flow footer — decision & anchors

The six-field record for the **inter-agent message** flow, shared verbatim with
the shared *Decision Index* catalog (`docs/concepts/decision-index.md`). Line
numbers drift; the **symbol name** is the durable anchor. Full text for each
`D-NNN` lives in
[`.claude/decisions-log.md`](https://github.com/joshuamschultz/Arc/blob/main/.claude/decisions-log.md).

| Field | This flow |
|---|---|
| **Where it lives** | arcteam `mail` → arcstore outbox |
| **What calls what** | `AgentMailService.send` → `_sign_envelope` → atomic inbox + outbox commit → leased delivery worker → NATS / memory backend |
| **What passes — where / when / to** | a signed envelope + `conversation_id` → the atomic commit; the outbox row leased with `SKIP LOCKED`; `sent` only after transport ack, else `pending`; one access-controlled copy per participant |
| **Security / modularity reason** | a mail body is untrusted data, never fleet control-plane; the durable store is authoritative and mail is only a wakeup; every sender, recipient, and backend op carries a scoped DID |
| **`D-NNN` / ADR** | D-538, D-539, D-510, D-647 · ADR-007 |
| **Code anchor** | `arcteam/mail.py:213,342,188` (`send`, `_sign_envelope`, `AgentMailService`) · `arcstore/mail_outbox.py:199` (`PostgresMailOutbox`, `SKIP LOCKED`) |

**Set it up:** the Track 1 counterpart is
[Build a fleet](../runbooks/operate/teams.md) — registering agents and turning
on team mail. The turn that a delivered message can wake is
[Anatomy of a turn](../walkthrough/03-anatomy-of-a-turn.md).
