# arcteam - Multi-Agent Coordination

> **Building with Arc**  ·  Build  ·  page 19 of 27  
> **For** Engineers writing code against Arc  
> [← arcskill](arcskill.md)  ·  [Docs home](../../README.md)  ·  [arcgateway →](arcgateway.md)

---

## Overview

`arcteam` enables **multi-agent coordination** through:
- **Entity registry** - Agents and humans with roles
- **Messaging service** - Signed, audited messages
- **Task distribution** - Durable task coordination
- **Pluggable backends** - NATS, in-memory, custom

> **Alpha boundary:** ArcTeam is the outer composition layer for independently
> runnable ArcAgents and ArcMemory's public shared-knowledge seam. It attaches,
> reloads, and removes the fleet extension on authorized started members; ArcMemory
> stays generic collection mechanics. Solo agents have no fleet requirement.
> AgentMail is the durable agent-to-agent mail path: signed envelopes are
> atomically projected to participant inboxes and a PostgreSQL leased outbox,
> with supervised retry/dead-letter delivery. ArcUI and `arc team` consume the
> same authorized service. See [fleet layering](../../concepts/fleet-layering.md).

```mermaid
flowchart TB
    classDef team fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef backend fill:#002550,stroke:#001A38,color:#FFFFFF

    arcteam[arcteam<br/>Multi-Agent]:::team --> NATS[NATS Backend]:::backend
    arcteam --> Memory[Memory Backend]:::backend
    arcteam --> Custom[Custom Backend]:::backend
    
    arcagent[arcagent] --> arcteam
    arccli[arccli] --> arcteam
```

---

## Entity Registry

### Entity Types

```mermaid
erDiagram
    ENTITY ||--o{ ROLE : has
    ENTITY {
        string id PK
        string name
        string type
        string[] roles
        string did
    }
    ROLE {
        string name
        string[] permissions
    }
```

### Entity Registration

```python
from arcteam import EntityRegistry, Entity, EntityType

registry = EntityRegistry(backend, audit)

# Register an agent
await registry.register(Entity(
    id="analyst-1",
    name="Senior Analyst",
    type=EntityType.AGENT,
    roles=["analyst", "reviewer"],
    did="did:key:z6Mk..."
))

# Register a human
await registry.register(Entity(
    id="alice",
    name="Alice Smith",
    type=EntityType.USER,
    roles=["operator"]
))
```

### Entity Types

| Type | Description |
|------|-------------|
| `EntityType.AGENT` | Arc agent with DID |
| `EntityType.USER` | Human operator |
| `EntityType.SYSTEM` | System service |

---

## Signed transport and AgentMail

`MessagingService` is the signed NATS transport used by ArcTeam. The durable
AgentMail facade composes it with ArcStore and is the contract used by agents,
ArcUI, and the CLI when a message must appear in an inbox or survive a
restart. Gateway sessions do not use this path.

### Message Flow

```mermaid
sequenceDiagram
    participant A as Agent 1
    participant Team as arcteam
    participant Backend as NATS
    participant B as Agent 2
    participant Audit as Audit

    A->>Team: send(sender, to, body, type, priority)
    Team->>Audit: sign(message)
    Team->>Backend: publish(channel, signed_message)
    Backend->>B: deliver(message)
    B->>Team: ack(message_id)
    Team->>Audit: log(ack)
```

### Sending Messages

```python
from arcteam import MessagingService, MsgType, Priority

svc = MessagingService(backend, registry, audit)

# Send direct message
await svc.send(
    sender="analyst-1",
    to=["executor-1"],
    body="Please analyze the Q4 data",
    msg_type=MsgType.TASK,
    priority=Priority.HIGH
)

# Broadcast to channel
await svc.send(
    sender="coordinator-1",
    to=["channel:general"],
    body="System maintenance at 5PM",
    msg_type=MsgType.INFO,
    priority=Priority.NORMAL
)
```

### Durable AgentMail

Use `AgentMailService` for direct agent-to-agent mail and inbox operations:

```python
from arcteam.mail import AgentMailService, MailSendRequest

# ``mail`` is the application-composed AgentMailService.
result = await mail.send(
    MailSendRequest(
        sender="agent://analyst-1",
        sender_did=analyst_did,
        to=("agent://executor-1",),
        body="Please analyze the Q4 data",
        idempotency_key="q4-analysis-1",
    )
)
# result.status is "sent" or "pending"; both states are durable.
```

The service signs the envelope before calling ArcStore's atomic
`record_event_with_outbox` seam. The PostgreSQL implementation writes every
participant's inbox copy and the outbox row in one transaction, then a
supervised `MailDeliveryWorker` claims rows with leases, retries transient
transport failures, and records exhausted entries in the dead-letter state.
The conversation ID is canonical across participant copies; local thread and
message IDs are owner-scoped for authorization. Use the same idempotency key
when retrying a `pending` result.

### Message Types

| Type | Purpose | Expected Response |
|------|---------|-------------------|
| `info` | Information | None |
| `request` | Request requiring reply | `reply` |
| `task` | Work assignment | `result` |
| `task_assigned` | Durable task assigned | `ack` |
| `result` | Task completion | None |
| `alert` | Urgent notification | `ack` |
| `ack` | Acknowledgement | None |

### Priorities

| Priority | Behavior |
|----------|----------|
| `low` | Background, can wait |
| `normal` | Default priority |
| `high` | Time-sensitive |
| `critical` | Interrupt current work |

---

## Task Coordination

### Task Store Integration

```mermaid
flowchart LR
    classDef store fill:#0073FE,stroke:#0055BC,color:#FFFFFF
    classDef team fill:#002550,stroke:#001A38,color:#FFFFFF

    arcstore[arcstore<br/>tasks/]:::store <--> arcteam[arcteam<br/>messaging]:::team
```

### Task Assignment

```python
from arcteam import MessagingService

# Assign task
task = store.assign("task_123", "executor-1")

# Notify owner
await svc.send(
    sender="coordinator-1",
    to=["executor-1"],
    body=f"New task assigned: {task.description}",
    msg_type=MsgType.TASK_ASSIGNED,
    priority=Priority.HIGH
)
```

### Auto-Routing

```python
from arcteam import MessagingService

# Route to best agent
task = store.route(task, capability_registry)

# Notify assigned agent
await svc.send(
    sender="coordinator-1",
    to=[task.owner],
    body=f"Task routed to you: {task.description}",
    msg_type=MsgType.TASK_ASSIGNED
)
```

---

## Storage Backends

### NATS Backend

```python
from arcteam.backends.nats import NatsBackend

backend = await NatsBackend.connect("nats://127.0.0.1:4222")
await backend.initialize()

# Features
# - Durable streams
# - KV records
# - Durable consumers
# - JetStream persistence
```

### Memory Backend

```python
from arcteam.storage import MemoryBackend

backend = MemoryBackend()

# Features
# - In-memory only
# - No persistence
# - For testing only
```

### Custom Backend

```python
from arcteam.storage import StorageBackend

class MyBackend(StorageBackend):
    async def publish(self, channel: str, message: dict) -> None:
        # Custom publish logic
        ...
    
    async def subscribe(self, channel: str) -> list[dict]:
        # Custom subscribe logic
        ...
    
    async def ack(self, message_id: str) -> None:
        # Custom ack logic
        ...
```

---

## Team-memory compatibility surface

`arcteam.memory` remains a landed public compatibility surface. New shared
knowledge uses ArcTeam's `shared_knowledge` lifecycle and ArcMemory's generic
collection seam. Generic store/index/embed/search/provenance/revoke/OKF
mechanics stay in ArcMemory; ArcTeam owns fleet authorization, membership,
promotion policy, lifecycle, and the shared backend.

```python
from arcteam.memory import TeamMemoryService

memory = TeamMemoryService(backend, audit)

# Per-entity memory
await memory.index(
    entity_id="analyst-1",
    content="Analyzed Q4 sales data",
    metadata={"task_id": "task_123"}
)

# Query entity memory
results = await memory.search(
    entity_id="analyst-1",
    query="sales data"
)
```

---

## CLI Commands

```bash
# Team initialization
arc team init [--root PATH]
arc team init --root /var/arc/team

# Entity management
arc team register ID [--name NAME] [--type TYPE] [--roles role1,role2]
arc team entities [--role ROLE]
arc team channels

# Status
arc team status
arc team config --json
arc team memory-status
```

---

## API Reference

### Classes

```python
class EntityRegistry:
    async def register(self, entity: Entity) -> None: ...
    async def lookup(self, entity_id: str) -> Entity: ...
    async def list_entities(self) -> list[Entity]: ...
    async def update_roles(self, entity_id: str, roles: list[str]) -> Entity: ...

class MessagingService:
    async def send(
        self,
        sender: str,
        to: list[str],
        body: str,
        msg_type: MsgType,
        priority: Priority = "normal"
    ) -> Message: ...
    
    async def receive(
        self,
        recipient: str,
        cursor: Cursor = None
    ) -> list[Message]: ...
    
    async def ack(self, message_id: str) -> None: ...

class TeamMemoryService:
    async def index(
        self,
        entity_id: str,
        content: str,
        metadata: dict = None
    ) -> str: ...
    
    async def search(
        self,
        entity_id: str,
        query: str,
        k: int = 5
    ) -> list[MemoryHit]: ...
```

### Types

```python
class Entity(TypedDict):
    id: str
    name: str
    type: EntityType  # "agent" | "user" | "system"
    roles: list[str]
    did: str = None

class Message(TypedDict):
    id: str
    timestamp: str
    sender: str
    recipients: list[str]
    body: str
    msg_type: MsgType
    priority: Priority
    signature: str

class MsgType(Enum):
    info = "info"
    request = "request"
    task = "task"
    task_assigned = "task_assigned"
    result = "result"
    alert = "alert"
    ack = "ack"

class Priority(Enum):
    low = "low"
    normal = "normal"
    high = "high"
    critical = "critical"
```

---

## Security Architecture

### Operator-Signed Audit Chain

```mermaid
flowchart LR
    classDef audit fill:#002550,stroke:#001A38,color:#FFFFFF

    Op[Operator Key]:::audit --> Sign1[Sign Record 1]:::audit
    Sign1 --> Sign2[Sign Record 2]:::audit
    Sign2 --> Sign3[Sign Record 3]:::audit
```

Every operation is signed by the **operator's key**, not the agent's, ensuring non-repudiation.

---

## Next Steps

- [API Reference](../../reference/api.md) - Complete API documentation
- [Package Index](../package-index.md) - All Arc packages

---

## Verified public surface

> Introspected from the installed package on the current commit. Every name
> below is importable exactly as shown; full signatures are in the
> [API reference](../../reference/api.md#arcteam).

### Classes

| Class | Purpose |
|---|---|
| `AuditLogger` | Append-only audit trail with a chained per-record signature. AU-2/AU-9/AU-10. |
| `AuditRecord` | Tamper-evident audit entry. |
| `Channel` | Channel definition. |
| `Cursor` | Per-entity read position in a stream. |
| `Entity` | Registered agent or user. |
| `EntityRegistry` | DID-keyed agent and user registration with role-based queries. |
| `EntityStatus` | Registration state of an entity. A registered entity is ``active``. |
| `EntityType` | Type of registered entity. |
| `MemoryBackend` | In-memory storage backend for unit tests. Dict-backed, no filesystem. |
| `Message` | Message envelope. Maps to a NATS JetStream message. |
| `MessagingService` | Push + pull messaging. Zero arcagent dependency. Standalone service. |
| `MsgType` | Message classification type. |
| `NatsBackend` | StorageBackend backed by NATS JetStream (records via KV, streams via JS). |
| `Priority` | Message priority level. |
| `RetryableDeliveryError` | Signal from a subscribe handler that delivery hit transient backpressure. |
| `StorageBackend` | Swappable storage abstraction shared by the messenger, registry, and audit. |
| `Team` | A named group of member entities coordinated together. |
| `TeamConfig` | ArcTeam configuration with sensible defaults. |
| `TeamFileStore` | Store and retrieve files in the team's shared directory. |
| `TeamMemoryConfig` | Team memory configuration. All fields have defaults. |
| `TeamMemoryService` | Shared team knowledge graph. |
| `TeamStore` | Persist and mutate teams on a :class:`StorageBackend`, auditing each op. |
