# System Design Document: DID Identity Unification

## Validation Checklist

- [x] Every PRD requirement has a corresponding design component
- [x] All interfaces defined with types
- [x] Data models complete with field types
- [x] Error handling strategy defined
- [x] No contradictions with arcteam or arcagent CLAUDE.md

---

## Architecture Overview

```
                     arcagent
                        │
            ┌───────────┼───────────┐
            │           │           │
     AgentIdentity   Agent.py   MessagingModule
     (DID, Ed25519)     │        (tools, prompt)
            │           │           │
            └───────────┼───────────┘
                        │
                   DID passed to
                        │
                     arcteam
                        │
            ┌───────────┼───────────┐
            │           │           │
     EntityRegistry  Messenger  AuditLogger
     (DID→name map)  (DID-based  (DID actor_id)
                      addressing)
```

**Key change**: The DID flows DOWN from arcagent identity into arcteam messaging. arcteam never constructs identities — it receives them.

---

## Component Changes

### 1. arcteam/types.py — Identity Resolution

**Current:**
```python
_URI_PATTERN = re.compile(r"^(agent|user|channel|role)://([a-zA-Z0-9_-]+)$")
VALID_SCHEMES = frozenset({"agent", "user", "channel", "role"})
```

**New — add DID pattern and resolution:**
```python
# DID pattern: did:arc:{org}:{type}/{hash}
_DID_PATTERN = re.compile(r"^did:arc:([a-zA-Z0-9_-]+):([a-zA-Z0-9_-]+)/([a-zA-Z0-9]+)$")

# URI patterns for non-entity targets (channels, roles)
_URI_PATTERN = re.compile(r"^(channel|role)://([a-zA-Z0-9_-]+)$")

# Legacy agent/user URIs (alias resolution)
_ALIAS_PATTERN = re.compile(r"^(agent|user)://([a-zA-Z0-9_-]+)$")


def parse_did(did: str) -> tuple[str, str, str]:
    """Parse a DID into (org, type, hash).

    Example: did:arc:default:executor/a3b7c9e1 → ("default", "executor", "a3b7c9e1")
    """
    match = _DID_PATTERN.match(did)
    if not match:
        raise ValueError(f"Invalid DID: {did!r}")
    return match.group(1), match.group(2), match.group(3)


def did_hash(did: str) -> str:
    """Extract hash segment from DID for stream naming.

    did:arc:default:executor/a3b7c9e1 → "a3b7c9e1"
    """
    _, _, h = parse_did(did)
    return h


def is_did(value: str) -> bool:
    """Check if a string is a DID."""
    return _DID_PATTERN.match(value) is not None


def is_alias(value: str) -> bool:
    """Check if a string is a legacy agent:// or user:// alias."""
    return _ALIAS_PATTERN.match(value) is not None
```

**LOC impact**: +30 LOC, -5 LOC (removing agent/user from VALID_SCHEMES) = net +25

### 2. arcteam/registry.py — Name Index

**Current:** Entities keyed by `id` (URI like `agent://brad`).

**New:** Entities keyed by DID. Name→DID index maintained separately.

```python
class EntityRegistry:
    # Existing: get(entity_id) — now entity_id is a DID

    async def get_by_name(self, name: str) -> Entity | None:
        """Look up entity by human-friendly name. Returns None if not found."""
        # Read name index: names/{name}.json → {"did": "did:arc:..."}
        data = await self._backend.read("names", name)
        if data is None:
            return None
        return await self.get(data["did"])

    async def resolve(self, identifier: str) -> Entity | None:
        """Resolve a DID, alias URI, or name to an Entity.

        Handles:
        - DID: did:arc:default:executor/abc → direct lookup
        - Alias: agent://brad_agent → name lookup → DID lookup
        - Name: brad_agent → name lookup → DID lookup
        """
        if is_did(identifier):
            return await self.get(identifier)
        if is_alias(identifier):
            _, name = parse_uri_legacy(identifier)
            return await self.get_by_name(name)
        # Try as bare name
        return await self.get_by_name(identifier)

    async def register(self, entity: Entity) -> None:
        """Register entity. Also writes name→DID index."""
        # ... existing registration logic ...
        # NEW: write name index
        await self._backend.write("names", entity.name, {"did": entity.id})
```

**LOC impact**: +40 LOC

### 3. arcteam/messenger.py — DID-Based Routing

**Current:**
```python
def _stream_name_from_uri(uri: str) -> str:
    scheme, name = parse_uri(uri)
    if scheme in ("agent", "user"):
        return f"arc.agent.{name}"
    return f"arc.{scheme}.{name}"
```

**New:**
```python
def _stream_name_from_identity(target: str, registry: EntityRegistry | None = None) -> str:
    """Convert target to stream name.

    DID: did:arc:default:executor/a3b7c9e1 → arc.agent.a3b7c9e1
    Channel: channel://ops → arc.channel.ops
    Role: role://executor → arc.role.executor
    Alias: agent://brad → resolved via registry → DID → arc.agent.{hash}
    """
    if is_did(target):
        return f"arc.agent.{did_hash(target)}"
    scheme, name = parse_uri(target)  # Works for channel:// and role://
    return f"arc.{scheme}.{name}"
```

**send() changes:**
- `message.sender` must be a DID (validated)
- `message.to` entries: DIDs for agents/users, URIs for channels/roles
- Alias resolution happens BEFORE send (at the tool layer or caller)

**poll_all() changes:**
- `entity_id` is a DID
- DM inbox stream: `arc.agent.{did_hash(entity_id)}`
- Role streams: unchanged (role names, not DIDs)

**LOC impact**: ~+20 LOC, -15 LOC = net +5

### 4. arcagent/modules/messaging/__init__.py — DID Injection

**Current:**
```python
entity_id = self._config.entity_id
if not entity_id:
    agent_name = ctx.config.agent.name
    entity_id = f"agent://{agent_name}"
```

**New:**
```python
entity_id = self._config.entity_id
if not entity_id:
    # Use agent's DID from identity module
    entity_id = ctx.identity.did if ctx.identity else ""
if not entity_id:
    raise ConfigError("No entity_id configured and no agent identity available")
```

**ModuleContext change** (arcagent/core/module_bus.py):
- Add `identity: AgentIdentity | None` to ModuleContext
- Agent.startup() passes identity when creating ModuleContext

**LOC impact**: +5 LOC in module_bus.py, +3 LOC in __init__.py

### 5. arcagent/modules/messaging/tools.py — Alias Resolution in Tools

The tool layer is where human-friendly aliases get resolved to DIDs:

```python
async def _handle_send(to: str = "", body: str = "", ...):
    # Resolve aliases before sending
    targets = [t.strip() for t in to.split(",") if t.strip()]
    resolved_targets = []
    for t in targets:
        if is_alias(t):
            entity = await registry.resolve(t)
            if entity:
                resolved_targets.append(entity.id)  # DID
            else:
                return json.dumps({"error": f"Unknown entity: {t}"})
        else:
            resolved_targets.append(t)  # DID or channel/role URI

    msg = Message(sender=entity_id, to=resolved_targets, ...)
```

**LOC impact**: +15 LOC

### 6. arcteam/types.py — URI Function Updates

Keep `parse_uri()` for channel:// and role:// only. Add `parse_uri_legacy()` for backward-compatible alias parsing:

```python
# Strict: only channel:// and role://
def parse_uri(uri: str) -> tuple[str, str]:
    match = _URI_PATTERN.match(uri)
    if not match:
        raise ValueError(f"Invalid URI: {uri!r}")
    return match.group(1), match.group(2)

# Legacy: agent:// and user:// aliases (for migration)
def parse_uri_legacy(uri: str) -> tuple[str, str]:
    match = _ALIAS_PATTERN.match(uri)
    if not match:
        raise ValueError(f"Invalid alias URI: {uri!r}")
    return match.group(1), match.group(2)
```

---

## Data Model Changes

### Entity (types.py)

```python
class Entity(BaseModel):
    id: str          # DID: did:arc:default:executor/a3b7c9e1 (was: agent://brad)
    name: str        # Human-friendly: brad_agent (unchanged)
    type: EntityType # agent | user (unchanged)
    roles: list[str]
    capabilities: list[str]
    created: str
    status: str
```

### Storage Layout Change

```
Before:                              After:
registry/                            registry/
  agent_brad_agent.json                did_arc_default_executor_a3b7c9e1.json
  user_josh.json                       did_arc_default_user_f4e8d2c1.json
                                     names/
                                       brad_agent.json  → {"did": "did:arc:..."}
                                       josh.json        → {"did": "did:arc:..."}

streams/                             streams/
  arc.agent.brad_agent/                arc.agent.a3b7c9e1/
    00000000.log                         00000000.log
  arc.agent.josh/                      arc.agent.f4e8d2c1/
    00000000.log                         00000000.log
```

Channel and role streams are unchanged.

### Message Fields

```python
# Before
Message(sender="agent://brad_agent", to=["agent://my_agent"], ...)

# After
Message(sender="did:arc:default:executor/a3b7c9e1", to=["did:arc:default:executor/b5f9e3d2"], ...)

# Channels/roles remain URI-based
Message(sender="did:arc:...", to=["channel://ops"], ...)
```

---

## Migration Strategy

### Phase 1: Dual Support (This Spec)

1. Entity registry accepts DID as `id` field
2. Name→DID index maintained on registration
3. Alias resolution converts `agent://name` → DID on the fly
4. New messages use DID sender/to fields
5. Stream reading checks both old (`arc.agent.{name}`) and new (`arc.agent.{hash}`) paths
6. CLI `migrate-identity` command copies data from old to new paths

### Phase 2: Deprecation (Future)

1. Emit deprecation warnings for `agent://` URIs in sender/to fields
2. Remove dual-path reading
3. Remove `parse_uri_legacy()`

---

## Error Handling

| Error | Handling |
|-------|---------|
| Invalid DID format | Reject with `ValueError` |
| Alias resolution fails (name not found) | Return error JSON in tool response |
| Both old and new stream paths exist | Read from new path (takes precedence) |
| DID not registered | DLQ with reason `sender_unauthorized` |
| Channel/role target with DID format | Reject (channels/roles are not DIDs) |

---

## LOC Budget Impact

| Component | Change | LOC Delta |
|-----------|--------|-----------|
| types.py | DID patterns, parse_did, is_did, is_alias | +25 |
| registry.py | get_by_name, resolve, name index | +40 |
| messenger.py | _stream_name_from_identity | +5 |
| messaging/__init__.py | DID from ctx.identity | +3 |
| messaging/tools.py | Alias resolution in tools | +15 |
| module_bus.py | identity field on ModuleContext | +5 |
| **Total** | | **+93** |

Both packages remain well within LOC budgets (arcteam < 2,000, arcagent < 3,500).

---

## Testing Strategy

| Layer | Scope |
|-------|-------|
| Unit | DID parsing, alias resolution, name index, stream naming |
| Integration | Full send/poll cycle with DID identity, alias→DID flow |
| Migration | Old URI data readable after upgrade, CLI migrate command |

---

## Dependencies

No new dependencies. Uses existing:
- `re` (stdlib) for DID pattern matching
- Ed25519 via `nacl` (already in arcagent)
- Pydantic 2.x (already everywhere)
