# Fleet layering and removable composition

ArcTeam is the optional fleet layer. It composes multiple standalone ArcAgents;
it is not part of an individual agent's nucleus. This page records the landed
alpha composition contract and its remaining AgentMail delivery limits.

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

AgentMail P0 is a public ArcTeam Python seam: it creates a signed envelope,
persists it to an injected `MailOutbox`, reports `sent` or `pending`, and drains
claimed entries with bounded retry. It is not yet a supervised production worker
service, nor is it final UI or CLI mail integration. Those gaps remain explicit
until their remediation lands.

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
and ArcMemory stays optional collection mechanics. AgentMail P0's outbox path
is also landed, but production worker supervision and final UI/CLI mail paths
are not. Do not represent the latter as operationally complete.

For the landed components, validate the seams with:

```bash
uv run pytest packages/arcteam/tests/unit packages/arcteam/tests/integration
uv run pytest packages/arcmemory/tests/architecture packages/arcmemory/tests/security
uv run pytest packages/arcagent/tests/architecture
uv run python scripts/run_adversarial_tests.py
```

Run the tests relevant to a changed implementation. Documentation changes can
be checked with `uv run mkdocs build --strict` and `git diff --check`.
