# Implementation Plan: ArcTeam Messaging Subsystem

## Status: PENDING

**Spec**: SPEC-006-arcteam-messaging
**Package**: `packages/arcteam`
**Branch**: `feature/arcTeam-messaging`
**Estimated LOC**: ~1,150 (production) + ~800 (tests)

---

## Build Order

Dependency chain determines phase order. Each phase is independently testable.

---

## Phase 1: Types + Config (Foundation)

**Files**: `src/arcteam/types.py`, `src/arcteam/config.py`
**Depends on**: Nothing
**LOC**: ~130

### Tasks

- [ ] 1.1 Define `Message` Pydantic model (15 fields: seq, id, ts, sender, to, reply_to, thread_id, msg_type, priority, action_required, subject, body, refs, status, meta)
- [ ] 1.2 Define enums: `EntityType`, `MsgType`, `Priority`
- [ ] 1.3 Define `Entity` model (id, name, type, roles, capabilities, created, status)
- [ ] 1.4 Define `Channel` model (name, description, members, created)
- [ ] 1.5 Define `Cursor` model (consumer, stream, seq, byte_pos, updated_at)
- [ ] 1.6 Define `AuditRecord` model (audit_seq, event_type, stream, msg_seq, subject, actor_id, target_id, classification, timestamp_utc, detail, hmac_sha256)
- [ ] 1.7 Define `TeamConfig` model with defaults (root, hmac_key_env, inactive_threshold, max_body_bytes, default_poll_limit, checkpoint_frequency)
- [ ] 1.8 Add body size validation to `Message` (max 64KB)
- [ ] 1.9 Add URI validation helpers (parse `agent://`, `user://`, `channel://`, `role://`)

### Tests

- [ ] 1.T1 Message model validation (valid/invalid bodies, field defaults, body size limit)
- [ ] 1.T2 URI parsing (all 4 schemes + invalid URIs)
- [ ] 1.T3 Enum serialization roundtrip
- [ ] 1.T4 Config defaults and override

### Verification

```bash
cd packages/arcteam && python -m pytest tests/unit/test_types.py tests/unit/test_config.py -v
ruff check src/arcteam/types.py src/arcteam/config.py
mypy src/arcteam/types.py src/arcteam/config.py --strict
```

---

## Phase 2: Storage Backend + FileBackend

**Files**: `src/arcteam/storage.py`
**Depends on**: Phase 1 (types)
**LOC**: ~200

### Tasks

- [ ] 2.1 Define `StorageBackend` Protocol (8 methods: read, write, delete, append, read_stream, query, list_keys, exists)
- [ ] 2.2 Implement `FileBackend.__init__` (root directory, ensure dirs)
- [ ] 2.3 Implement `read()` — JSON file read by collection/key
- [ ] 2.4 Implement `write()` — atomic write (tempfile + `os.replace()`)
- [ ] 2.5 Implement `delete()` — remove file, return existed
- [ ] 2.6 Implement `append()` — JSONL append with `fcntl.flock(LOCK_EX)`, returns byte offset
- [ ] 2.7 Implement `read_stream()` — seek to byte_pos, return records after after_seq, up to limit. Skip malformed lines.
- [ ] 2.8 Implement `query()` — scan collection directory, filter by field matches or key prefix
- [ ] 2.9 Implement `list_keys()` — list files in collection directory
- [ ] 2.10 Implement `exists()` — check file existence
- [ ] 2.11 Implement `MemoryBackend` (dict-backed, for unit tests)

### Tests

- [ ] 2.T1 Atomic write: verify temp file + rename pattern (read back after write)
- [ ] 2.T2 Append: verify JSONL format, file locking, byte offset return
- [ ] 2.T3 read_stream: seek from byte_pos, after_seq filter, limit
- [ ] 2.T4 read_stream: skip malformed trailing lines (crash recovery)
- [ ] 2.T5 Concurrent appends: 10 writers to same stream, no corruption
- [ ] 2.T6 query: field filtering, prefix filtering
- [ ] 2.T7 MemoryBackend: same test suite passes against both backends

### Verification

```bash
cd packages/arcteam && python -m pytest tests/unit/test_storage.py -v
ruff check src/arcteam/storage.py
mypy src/arcteam/storage.py --strict
```

---

## Phase 3: Audit Logger

**Files**: `src/arcteam/audit.py`
**Depends on**: Phase 2 (StorageBackend)
**LOC**: ~100

### Tasks

- [ ] 3.1 Implement `AuditLogger.__init__` (backend, hmac_key, load last seq + hmac)
- [ ] 3.2 Implement `log()` — build AuditRecord, compute chained HMAC, append to audit stream
- [ ] 3.3 Implement chained HMAC computation (`prev_hmac + json.dumps(record, sort_keys=True)`)
- [ ] 3.4 Implement `verify_chain()` — read full audit stream, verify each HMAC against prev
- [ ] 3.5 Load HMAC key from environment variable (config.hmac_key_env)
- [ ] 3.6 Handle missing HMAC key gracefully (log warning, use deterministic default for dev)

### Tests

- [ ] 3.T1 Single audit record: correct fields, HMAC present
- [ ] 3.T2 Chain integrity: 10 records, verify_chain returns True
- [ ] 3.T3 Tamper detection: modify a record, verify_chain returns False
- [ ] 3.T4 Gap detection: delete a record, verify_chain detects gap in audit_seq
- [ ] 3.T5 HMAC key from env var

### Verification

```bash
cd packages/arcteam && python -m pytest tests/unit/test_audit.py -v
ruff check src/arcteam/audit.py
mypy src/arcteam/audit.py --strict
```

---

## Phase 4: Entity Registry

**Files**: `src/arcteam/registry.py`
**Depends on**: Phase 2 (StorageBackend), Phase 3 (AuditLogger)
**LOC**: ~120

### Tasks

- [ ] 4.1 Implement `EntityRegistry.__init__` (backend, audit)
- [ ] 4.2 Implement `register()` — validate entity, check duplicate, write, audit
- [ ] 4.3 Implement `get()` — read entity by ID
- [ ] 4.4 Implement `list_entities()` — all entities, optional role filter
- [ ] 4.5 Implement `by_role()` — query entities with matching role
- [ ] 4.6 Implement `update_status()` — update entity status, audit

### Tests

- [ ] 4.T1 Register agent, get it back
- [ ] 4.T2 Reject duplicate ID
- [ ] 4.T3 Filter by role
- [ ] 4.T4 All operations generate audit records
- [ ] 4.T5 Update status

### Verification

```bash
cd packages/arcteam && python -m pytest tests/unit/test_registry.py -v
ruff check src/arcteam/registry.py
mypy src/arcteam/registry.py --strict
```

---

## Phase 5: Messaging Service

**Files**: `src/arcteam/messenger.py`
**Depends on**: Phase 1-4 (all prior)
**LOC**: ~350

### Tasks

- [ ] 5.1 Implement `MessagingService.__init__` (backend, registry, audit)
- [ ] 5.2 Implement URI routing helper: `_resolve_stream(uri) -> str` (channel://, role://, agent://, user://)
- [ ] 5.3 Implement `send()` — validate message, auto-assign seq/id/ts/thread_id, route to stream(s), audit
- [ ] 5.4 Implement seq counter management (per-stream monotonic, load from last record on init)
- [ ] 5.5 Implement `poll()` — read cursor, read_stream from cursor forward, return messages
- [ ] 5.6 Implement `poll_all()` — resolve subscriptions, poll each, return dict
- [ ] 5.7 Implement `ack()` — validate forward-only, write cursor atomically
- [ ] 5.8 Implement `get_cursor()` — read cursor file
- [ ] 5.9 Implement `resolve_subscriptions()` — DM inbox + channel memberships + role streams
- [ ] 5.10 Implement `create_channel()` — write channel definition, create stream dir
- [ ] 5.11 Implement `join_channel()` / `leave_channel()` — update channel members
- [ ] 5.12 Implement `list_channels()` — query channel definitions
- [ ] 5.13 Implement `get_thread()` — scan stream for matching thread_id
- [ ] 5.14 Implement DLQ: on send failure, append to `dlq/00000000.log` with reason in meta
- [ ] 5.15 Implement `dlq_list()` — read DLQ stream
- [ ] 5.16 Add body size validation (64KB limit)
- [ ] 5.17 Add sender verification (must be registered entity)

### Tests

- [ ] 5.T1 Send to channel: message appears in channel stream with correct seq
- [ ] 5.T2 Send to role: message appears in role stream
- [ ] 5.T3 Send to agent (DM): message appears in agent inbox stream
- [ ] 5.T4 Send to multiple targets: message appears in all target streams
- [ ] 5.T5 Auto-assign: seq is monotonic, id is unique, ts is set, thread_id auto-set
- [ ] 5.T6 Threading: reply_to sets thread_id to original message's thread_id
- [ ] 5.T7 Poll: returns messages after cursor, respects limit
- [ ] 5.T8 Poll with no cursor: returns from beginning
- [ ] 5.T9 Ack: cursor advances, subsequent poll starts from new position
- [ ] 5.T10 Ack: rejects backward cursor movement
- [ ] 5.T11 poll_all: returns messages from all subscribed streams
- [ ] 5.T12 Channel create/join/leave: membership updates
- [ ] 5.T13 Body too large: rejected, DLQ entry created
- [ ] 5.T14 Invalid URI: rejected, DLQ entry created
- [ ] 5.T15 Unregistered sender: rejected, DLQ entry created
- [ ] 5.T16 All send operations generate audit records
- [ ] 5.T17 get_thread: returns all messages in thread chronologically

### Verification

```bash
cd packages/arcteam && python -m pytest tests/unit/test_messenger.py -v
ruff check src/arcteam/messenger.py
mypy src/arcteam/messenger.py --strict
```

---

## Phase 6: CLI

**Files**: `src/arcteam/cli.py`
**Depends on**: Phase 1-5 (all prior)
**LOC**: ~250

### Tasks

- [ ] 6.1 Implement argument parser with subcommands and global options (`--root`, `--as`)
- [ ] 6.2 Implement `register` command
- [ ] 6.3 Implement `entities` command with `--role` filter
- [ ] 6.4 Implement `channel` command (create with `--members`, `--description`)
- [ ] 6.5 Implement `join` command
- [ ] 6.6 Implement `channels` command (list)
- [ ] 6.7 Implement `send` command (all flags: --to, --body, --subject, --type, --priority, --action, --refs, --reply-to)
- [ ] 6.8 Implement `inbox` command (poll all subscribed streams, display formatted)
- [ ] 6.9 Implement `read` command (--channel or --dm, --limit)
- [ ] 6.10 Implement `thread` command (thread_id, --stream)
- [ ] 6.11 Implement `dlq` command (list DLQ entries)
- [ ] 6.12 Implement `audit` command (list recent audit entries, --verify for chain check)
- [ ] 6.13 Add `arc-team` entry point to pyproject.toml
- [ ] 6.14 Output formatting: human-readable table for inbox, JSON for programmatic use (`--json` flag)

### Tests

- [ ] 6.T1 E2E: register → channel → join → send → inbox flow
- [ ] 6.T2 E2E: send DM → read → thread
- [ ] 6.T3 E2E: send to role → inbox shows role messages
- [ ] 6.T4 --json flag outputs valid JSON
- [ ] 6.T5 Invalid arguments produce helpful error messages

### Verification

```bash
cd packages/arcteam && python -m pytest tests/e2e/test_cli.py -v
ruff check src/arcteam/cli.py
mypy src/arcteam/cli.py --strict
```

---

## Phase 7: Integration Tests + Benchmarks

**Files**: `tests/integration/test_messaging_flow.py`, `tests/integration/test_benchmarks.py`
**Depends on**: Phase 1-6
**LOC**: ~200 (tests)

### Tasks

- [ ] 7.1 Integration test: full agent workflow (register → create channel → join → send → poll → ack → poll returns empty)
- [ ] 7.2 Integration test: multi-agent scenario (3 agents, 2 channels, role broadcast, DMs)
- [ ] 7.3 Integration test: cursor crash recovery (write message, don't ack, restart, poll returns same message)
- [ ] 7.4 Integration test: audit chain verification after full workflow
- [ ] 7.5 Integration test: DLQ captures all failure types
- [ ] 7.6 Benchmark: message append latency (target < 5ms p99)
- [ ] 7.7 Benchmark: stream poll latency for 100 messages (target < 50ms p99)
- [ ] 7.8 Benchmark: cursor advance latency (target < 1ms p99)

### Verification

```bash
cd packages/arcteam && python -m pytest tests/integration/ -v
ruff check src/arcteam/
mypy src/arcteam/ --strict
python -m pytest tests/ --cov=arcteam --cov-report=term-missing
```

---

## Phase 8: Package Finalization

**Files**: `pyproject.toml`, `src/arcteam/__init__.py`
**Depends on**: Phase 1-7

### Tasks

- [ ] 8.1 Update `__init__.py` with public exports (MessagingService, StorageBackend, FileBackend, Entity, Message, Channel, etc.)
- [ ] 8.2 Add `arc-team` entry point to pyproject.toml `[project.scripts]`
- [ ] 8.3 Verify `pip install -e .` works
- [ ] 8.4 Run full quality gate: ruff + mypy + pytest + coverage
- [ ] 8.5 Verify LOC < 2,000

### Final Verification

```bash
cd packages/arcteam
ruff check src/arcteam/
ruff format --check src/arcteam/
mypy src/arcteam/ --strict
python -m pytest tests/ --cov=arcteam --cov-report=term-missing -v
wc -l src/arcteam/*.py  # Must be < 2000
```

---

## Summary

| Phase | Component | LOC | Tests | Depends On |
|-------|-----------|-----|-------|------------|
| 1 | Types + Config | 130 | 4 | - |
| 2 | Storage Backend | 200 | 7 | Phase 1 |
| 3 | Audit Logger | 100 | 5 | Phase 2 |
| 4 | Entity Registry | 120 | 5 | Phase 2, 3 |
| 5 | Messaging Service | 350 | 17 | Phase 1-4 |
| 6 | CLI | 250 | 5 | Phase 1-5 |
| 7 | Integration + Benchmarks | - | 8 | Phase 1-6 |
| 8 | Package Finalization | - | - | Phase 1-7 |
| **Total** | | **1,150** | **51** | |

**Completion**: 51 tests, 8 phases, ~1,150 production LOC, under 2,000 LOC budget.
