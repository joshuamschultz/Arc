# arcteam

Multi-agent team messaging and coordination layer. Entity registry, channels, direct messages, and team memory with HMAC-signed audit trail.

## Layer position

arcteam depends on arctrust (for audit) and Pydantic. arcagent depends on arcteam for team messaging capabilities. arccli depends on arcteam for the `arc team` commands. arcteam never imports from arcagent or arccli.

## What it provides

- `EntityRegistry` — registers and lists agents and users; entities typed as `agent` or `user` with roles
- `MessagingService` — send and receive structured messages across channels and DMs; message types: `info`, `task`, `result`, `alert`, `ack`, `broadcast`; priorities: `low`, `normal`, `high`, `critical`
- `AuditLogger` — HMAC-signed append-only audit stream for all team operations
- `TeamConfig` — Pydantic config for team data root directory
- `TeamFileStore` — file-backed team workspace
- `TeamMemoryService`, `TeamMemoryConfig` — entity memory index with dirty tracking
- `StorageBackend`, `FileBackend`, `MemoryBackend` — pluggable storage adapters
- `Entity`, `EntityType`, `Channel`, `Message`, `MsgType`, `Priority`, `Cursor`, `AuditRecord` — core types

## Quick example

```python
from arcteam import MessagingService, EntityRegistry
from arcteam.storage import FileBackend
from arcteam.audit import AuditLogger
from arcteam.types import Entity, EntityType

backend = FileBackend("/var/arc/team")
audit = AuditLogger(backend, hmac_key=AuditLogger.load_hmac_key())
await audit.initialize()
registry = EntityRegistry(backend, audit)
svc = MessagingService(backend, registry, audit)

await registry.register(Entity(id="agent-1", name="Analyst", type=EntityType("agent")))
await svc.send(sender="agent-1", to=["agent-2"], body="Report ready in workspace/reports/")
```

## Architecture references

- ADR-019: Four Pillars Universal — all team operations audited via arctrust.audit

## Status

- Tests: 307 (run with `uv run --no-sync pytest packages/arcteam/tests`)
- ruff + mypy --strict: clean
