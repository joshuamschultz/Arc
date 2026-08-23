# Fleet layering and removable composition

ArcTeam is the optional fleet layer. It composes multiple standalone ArcAgents;
it is not part of an individual agent's nucleus. This page records the alpha
architecture direction and distinguishes it from the code that is already
present so operators do not infer a capability from a diagram.

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

The current `SharedKnowledgeAdapter` and `SharedKnowledgeBackend` in ArcMemory
are the relevant mechanics seam: save, read, search, and revoke are explicit
operations with an explicit access record. ArcTeam is the correct future
provider of the fleet policy/backend, not a reason to put roster or promotion
policy into ArcMemory.

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
for coordination and observability. It is not an inbox transport, and it is
not ArcMemory's generic index/search engine. The durable store is authoritative
for a task transition; mail is a wakeup/narration signal and cannot make a
durable state change appear to have happened.

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

The direction above is agreed architecture, but it is not yet fully reflected
in the current package metadata and imports. At this commit, ArcTeam ships
standalone transport/registry/audit/workflow components with no ArcAgent or
ArcMemory dependency, while ArcAgent's optional messaging integration lazily
imports ArcTeam. That is a transitional reverse integration, not proof that
ArcTeam composition has landed. The change that inverts the runtime dependency,
adds the public composition contracts, and moves fleet governance has not been
verified by this documentation slice.

For the landed components, validate the seams with:

```bash
uv run pytest packages/arcteam/tests/unit packages/arcteam/tests/integration
uv run pytest packages/arcmemory/tests/architecture packages/arcmemory/tests/security
uv run pytest packages/arcagent/tests/architecture
uv run python scripts/run_adversarial_tests.py
```

Run the tests relevant to a changed implementation; the command list is not a
claim that the missing composition layer is implemented. Documentation changes
can be checked with `uv run mkdocs build --strict` and `git diff --check`.
