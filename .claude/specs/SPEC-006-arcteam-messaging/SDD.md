# System Design Document: ArcTeam Messaging Subsystem

## Validation Checklist

- [x] Every PRD requirement has a corresponding design component
- [x] All interfaces defined with types
- [x] Data models complete with field types
- [x] Storage layout specified
- [x] Error handling strategy defined
- [x] No contradictions with arcteam CLAUDE.md
- [x] LOC estimates per component

---

## Architecture Overview

```
                    CLI (arc-team)
                         │
                   MessagingService
                    │         │
         ┌──────────┤         ├──────────┐
         │          │         │          │
    EntityRegistry  │    AuditLogger    │
         │          │         │          │
         └──────────┤─────────┤──────────┘
                    │         │
               StorageBackend (Protocol)
                    │
               FileBackend
                    │
              Filesystem (JSONL + JSON)
```

All components depend on `StorageBackend` for persistence. `AuditLogger` wraps all write operations. `MessagingService` is the primary API surface.

---

## Component Design

### 1. Types (`types.py`) — ~80 LOC

Shared Pydantic models and type definitions.

```python
from pydantic import BaseModel, Field
from enum import Enum
from typing import Any

class EntityType(str, Enum):
    AGENT = "agent"
    USER = "user"

class MsgType(str, Enum):
    INFO = "info"
    REQUEST = "request"
    TASK = "task"
    RESULT = "result"
    ALERT = "alert"
    ACK = "ack"

class Priority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"

class Message(BaseModel):
    """15-field message envelope. Maps to NATS JetStream message."""
    seq: int = 0                                # Auto-assigned per stream
    id: str = ""                                # Auto: msg_{timestamp}_{hash}
    ts: str = ""                                # Auto: ISO 8601 UTC
    sender: str                                 # URI: agent://x or user://x
    to: list[str]                               # Target URIs
    reply_to: str | None = None                 # Message ID this replies to
    thread_id: str | None = None                # Auto: root message ID
    msg_type: MsgType = MsgType.INFO
    priority: Priority = Priority.NORMAL
    action_required: bool = False
    subject: str = ""                           # Short summary for triage
    body: str                                   # Content (max 64KB)
    refs: list[str] = Field(default_factory=list)  # Cross-references
    status: str = "sent"                        # sent|delivered|read|acted
    meta: dict[str, Any] = Field(default_factory=dict)

class Entity(BaseModel):
    """Registered agent or user."""
    id: str                                     # agent://x or user://x
    name: str
    type: EntityType
    roles: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    created: str = ""                           # ISO 8601
    status: str = "active"

class Channel(BaseModel):
    """Channel definition."""
    name: str
    description: str = ""
    members: list[str] = Field(default_factory=list)  # Entity URIs
    created: str = ""

class Cursor(BaseModel):
    """Per-entity read position in a stream."""
    consumer: str                               # Entity ID
    stream: str                                 # Stream name
    seq: int = 0                                # Last read sequence number
    byte_pos: int = 0                           # Byte offset for fast seeking
    updated_at: str = ""                        # ISO 8601

class AuditRecord(BaseModel):
    """Tamper-evident audit entry."""
    audit_seq: int
    event_type: str                             # message.sent, entity.registered, etc.
    stream: str = ""
    msg_seq: int | None = None
    subject: str                                # NATS-style subject
    actor_id: str
    target_id: str | None = None
    classification: str = "UNCLASSIFIED"
    timestamp_utc: str
    detail: str
    hmac_sha256: str = ""                       # Chained HMAC
```

### 2. Storage Backend (`storage.py`) — ~200 LOC

Protocol + FileBackend implementation.

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class StorageBackend(Protocol):
    """Swappable storage abstraction. Phase 1: files. Phase 2: SQLite. Phase 4: Postgres."""

    async def read(self, collection: str, key: str) -> dict | None:
        """Read a single JSON record."""
        ...

    async def write(self, collection: str, key: str, data: dict) -> None:
        """Write/overwrite a single JSON record. Atomic (write-to-tmp + rename)."""
        ...

    async def delete(self, collection: str, key: str) -> bool:
        """Delete a record. Returns True if it existed."""
        ...

    async def append(self, collection: str, key: str, entry: dict) -> int:
        """Append to a JSONL stream. Returns byte offset. File-locked."""
        ...

    async def read_stream(
        self, collection: str, key: str,
        after_seq: int = 0, byte_pos: int = 0, limit: int = 100,
    ) -> list[dict]:
        """Read entries from a JSONL stream starting after `after_seq`.
        Uses `byte_pos` for fast seeking when available."""
        ...

    async def query(self, collection: str, filters: dict | None = None, prefix: str | None = None) -> list[dict]:
        """Query records by field match or key prefix."""
        ...

    async def list_keys(self, collection: str, prefix: str | None = None) -> list[str]:
        """List all keys in a collection."""
        ...

    async def exists(self, collection: str, key: str) -> bool:
        """Check if a record exists."""
        ...
```

**FileBackend specifics:**
- Root directory: configurable, default `~/.arc/team`
- Records: `{root}/{collection}/{key}.json`
- Streams: `{root}/{collection}/{key}/00000000.log`
- Atomic writes: `tempfile.NamedTemporaryFile` + `os.replace()`
- Stream appends: `fcntl.flock(LOCK_EX)` around write + flush + fsync
- Readers: no lock, skip incomplete trailing lines (partial write recovery)
- `read_stream`: seeks to `byte_pos`, scans for records with `seq > after_seq`

### 3. Audit Logger (`audit.py`) — ~100 LOC

Wraps all write operations with tamper-evident logging.

```python
class AuditLogger:
    """Append-only audit trail with chained HMACs. NIST 800-53 AU-2/AU-9."""

    def __init__(self, backend: StorageBackend, hmac_key: bytes) -> None: ...

    async def log(
        self, event_type: str, subject: str, actor_id: str,
        detail: str, stream: str = "", msg_seq: int | None = None,
        target_id: str | None = None, classification: str = "UNCLASSIFIED",
    ) -> None:
        """Append an audit record with chained HMAC."""
        ...

    async def verify_chain(self) -> tuple[bool, int]:
        """Verify HMAC chain integrity. Returns (valid, last_verified_seq)."""
        ...
```

**Chain computation:**
```python
hmac_input = prev_hmac + json.dumps(record_without_hmac, sort_keys=True)
record.hmac_sha256 = hmac.new(key, hmac_input.encode(), hashlib.sha256).hexdigest()
```

### 4. Entity Registry (`registry.py`) — ~120 LOC

```python
class EntityRegistry:
    """Agent and user registration with role-based queries."""

    def __init__(self, backend: StorageBackend, audit: AuditLogger) -> None: ...

    async def register(self, entity: Entity) -> None:
        """Register a new entity. Rejects duplicates."""
        ...

    async def get(self, entity_id: str) -> Entity | None: ...

    async def list_entities(self, role: str | None = None) -> list[Entity]: ...

    async def by_role(self, role: str) -> list[Entity]:
        """All entities with this role. Used for role-based addressing."""
        ...

    async def update_status(self, entity_id: str, status: str) -> None: ...
```

### 5. Messaging Service (`messenger.py`) — ~350 LOC

The primary API surface. Stateless — all state lives in StorageBackend.

```python
class MessagingService:
    """Pull-based messaging. Zero arcagent dependency. Standalone service."""

    def __init__(
        self, backend: StorageBackend, registry: EntityRegistry,
        audit: AuditLogger,
    ) -> None: ...

    # --- Send ---

    async def send(self, message: Message) -> Message:
        """Send a message. Routes to appropriate stream(s) based on `to` URIs.
        Auto-assigns seq, id, ts, thread_id. Validates body size.
        Writes audit record. Returns the sent message with populated fields.
        """
        ...

    # --- Poll ---

    async def poll(
        self, stream: str, entity_id: str, max_messages: int = 10,
    ) -> list[Message]:
        """Pull unread messages from a stream for this entity.
        Reads from cursor position forward. Does NOT advance cursor.
        """
        ...

    async def poll_all(
        self, entity_id: str, max_per_stream: int = 10,
    ) -> dict[str, list[Message]]:
        """Poll all subscribed streams (inbox + channels + roles).
        Returns {stream_name: [messages]}.
        """
        ...

    # --- Cursor ---

    async def ack(self, stream: str, entity_id: str, seq: int, byte_pos: int) -> None:
        """Advance cursor after successful processing. Forward-only."""
        ...

    async def get_cursor(self, stream: str, entity_id: str) -> Cursor | None:
        """Get current cursor position."""
        ...

    # --- Channels ---

    async def create_channel(self, channel: Channel) -> None:
        """Create a channel and its stream directory."""
        ...

    async def join_channel(self, channel_name: str, entity_id: str) -> None:
        """Add entity to channel membership."""
        ...

    async def leave_channel(self, channel_name: str, entity_id: str) -> None: ...

    async def list_channels(self) -> list[Channel]: ...

    # --- Threads ---

    async def get_thread(self, stream: str, thread_id: str) -> list[Message]:
        """All messages in a thread, chronologically."""
        ...

    # --- DLQ ---

    async def dlq_list(self, limit: int = 50) -> list[dict]:
        """List Dead Letter Queue entries."""
        ...

    # --- Subscriptions ---

    def resolve_subscriptions(self, entity: Entity) -> list[str]:
        """Resolve all streams an entity should poll:
        - arc.agent.{id} (DM inbox, always)
        - arc.channel.{name} (for each channel membership)
        - arc.role.{role} (for each role in entity.roles)
        """
        ...
```

**Routing logic in `send()`:**
```python
for uri in message.to:
    if uri.startswith("channel://"):
        stream = f"arc.channel.{parse_name(uri)}"
    elif uri.startswith("role://"):
        stream = f"arc.role.{parse_name(uri)}"
    elif uri.startswith("agent://") or uri.startswith("user://"):
        stream = f"arc.agent.{parse_name(uri)}"
    else:
        # DLQ with reason: invalid_address
```

### 6. CLI (`cli.py`) — ~250 LOC

Uses `argparse` (stdlib). No click/typer dependency.

```
arc-team register <entity_id> --name <name> --type agent|user --roles r1,r2
arc-team entities [--role <role>]
arc-team channel <name> --members e1,e2 --description "..."
arc-team join <channel> <entity_id>
arc-team channels
arc-team send --to <uri> --body <text> [--subject] [--type] [--priority] [--action] [--refs] [--reply-to]
arc-team inbox --as <entity_id> [--limit N]
arc-team read --channel <name> | --dm <entity_id> [--limit N]
arc-team thread <thread_id> --stream <stream>
arc-team dlq [--limit N]
arc-team audit [--limit N] [--verify]

Global: --root PATH (default ~/.arc/team), --as ENTITY_URI
```

### 7. Configuration (`config.py`) — ~50 LOC

```python
class TeamConfig(BaseModel):
    """ArcTeam configuration."""
    root: Path = Path.home() / ".arc" / "team"
    hmac_key_env: str = "ARCTEAM_HMAC_KEY"       # Env var for audit HMAC key
    inactive_threshold_hours: int = 24             # Stale cursor cleanup
    max_body_bytes: int = 65536                    # 64KB
    default_poll_limit: int = 10
    checkpoint_frequency: int = 10                 # Cursor save every N messages
```

---

## Data Directory Structure

```
{root}/                                    # Default: ~/.arc/team
├── messages/
│   ├── channels/                          # Channel definitions (JSON)
│   │   ├── project-alpha.json
│   │   └── ops-alerts.json
│   ├── streams/                           # Message content (JSONL)
│   │   ├── arc.channel.project-alpha/
│   │   │   └── 00000000.log
│   │   ├── arc.channel.ops-alerts/
│   │   │   └── 00000000.log
│   │   ├── arc.role.procurement/
│   │   │   └── 00000000.log
│   │   ├── arc.agent.procurement-01/
│   │   │   └── 00000000.log
│   │   └── arc.agent.josh/
│   │       └── 00000000.log
│   ├── cursors/                           # Per-entity read positions
│   │   ├── arc.channel.project-alpha/
│   │   │   ├── agent_procurement-01.cursor
│   │   │   └── user_josh.cursor
│   │   ├── arc.role.procurement/
│   │   │   └── agent_procurement-01.cursor
│   │   └── arc.agent.procurement-01/
│   │       └── agent_procurement-01.cursor
│   └── registry/                          # Entity definitions
│       ├── agent_procurement-01.json
│       └── user_josh.json
├── audit/
│   └── 00000000.log                       # Append-only, chained HMACs
├── dlq/
│   └── 00000000.log                       # Failed deliveries
└── security/
    └── acl.json                           # RBAC (future)
```

---

## NATS Migration Map

Every file concept maps 1:1 to a NATS JetStream concept:

| File | NATS |
|------|------|
| `streams/arc.channel.X/00000000.log` | JetStream Stream on subject `arc.channel.X.>` |
| `seq` field per record | `msg.Metadata().Sequence.Stream` |
| `cursors/stream/entity.cursor` | Durable consumer with named cursor |
| `cursor.seq` | `DeliverByStartSequence` |
| `poll(max_messages=N)` | Pull consumer `fetch(batch=N)` |
| `ack(seq)` | `msg.ack()` |
| Multiple cursors on role stream | Multiple consumers on one stream |
| `audit/00000000.log` | Dedicated audit stream with `RetentionPolicy.LIMITS` |
| `dlq/00000000.log` | DLQ stream with republish on NAK |

---

## Error Handling

| Error | Handling |
|-------|---------|
| Message body > 64KB | Reject with `ValidationError`. DLQ with reason `body_too_large`. |
| Invalid `to` URI | Reject. DLQ with reason `invalid_address`. |
| Stream directory missing | Auto-create on first write. |
| Cursor file missing | Start from seq=0 (beginning of stream). |
| Incomplete JSONL line (crash during write) | Readers skip lines that fail JSON parse. |
| `flock` timeout | Retry once after 100ms. Log warning. DLQ on second failure. |
| Backward cursor advance | Reject silently. Log warning. |
| Unregistered sender | Reject. DLQ with reason `sender_unauthorized`. |

---

## LOC Budget

| Component | File | Estimated LOC |
|-----------|------|--------------|
| Types | `types.py` | 80 |
| Storage | `storage.py` | 200 |
| Audit | `audit.py` | 100 |
| Registry | `registry.py` | 120 |
| Messenger | `messenger.py` | 350 |
| CLI | `cli.py` | 250 |
| Config | `config.py` | 50 |
| **Total** | | **1,150** |

Under the 2,000 LOC budget with ~850 LOC headroom for the other ArcTeam subsystems to share the storage layer.

---

## Testing Strategy

| Layer | Scope | Tools |
|-------|-------|-------|
| Unit (70%) | Each component in isolation, mock StorageBackend | pytest, in-memory backend |
| Integration (20%) | Full service with FileBackend on temp dirs | pytest, `tmp_path` fixture |
| E2E (10%) | CLI commands end-to-end | subprocess, temp dirs |
| Benchmarks | Append/poll/cursor latency | pytest-benchmark |

**In-memory StorageBackend** for unit tests: dict-backed implementation of the protocol. No filesystem access.

---

## Dependencies

| Library | Purpose | Phase 1 |
|---------|---------|---------|
| Pydantic 2.x | Data validation, all models | Yes |
| OpenTelemetry API | Traces, metrics (optional) | Yes (API only, no SDK) |
| Python stdlib | `asyncio`, `json`, `fcntl`, `hashlib`, `hmac`, `argparse`, `pathlib`, `tempfile` | Yes |
