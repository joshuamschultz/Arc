```
╭──────────────────────────────────────────────────────╮
│                                                      │
│   ▄▀█ █▀█ █▀▀ ▀█▀ █▀▀ ▄▀█ █▀▄▀█                   │
│   █▀█ █▀▄ █▄▄  █  ██▄ █▀█ █ ▀ █                   │
│                                                      │
│   Multi-Agent Collaboration Layer                    │
│   for Autonomous Agents at Scale                     │
│                                                      │
├──────────────────────────────────────────────────────┤
│  4 Primitives · 10K+ Agents · Zero External Deps    │
╰──────────────────────────────────────────────────────╯
```

**The collaboration backbone for autonomous agent organizations.** ArcTeam provides four primitives that mirror how humans collaborate — messaging, tasks, knowledge, and files — built for machine consumption with human oversight.

Agents communicate via async messaging, coordinate work through structured tasks, share institutional knowledge through a bidirectionally-linked knowledge base, and produce organized file artifacts. Every operation is audited. Every entity is addressable via typed URIs. Every subsystem scales independently.

```python
from arcteam import TeamContext

ctx = TeamContext(root="~/.arc/team", identity="agent://procurement-01")

# Drain inbox on wake-up
messages = await ctx.messaging.drain_inbox()

# Read assigned tasks
tasks = await ctx.tasks.list(status="assigned", assigned_to="agent://procurement-01")

# Search knowledge base for context
vendors = await ctx.kb.search(tags=["cmmc", "vendor"])

# Produce file artifacts
await ctx.files.add("projects/alpha/analysis.xlsx", tags=["vendor", "cmmc"])
```

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: CC BY-ND 4.0](https://img.shields.io/badge/license-CC%20BY--ND%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nd/4.0/)
[![Lines of Code](https://img.shields.io/badge/lines-~3,000-brightgreen.svg)]()
[![Zero External Deps](https://img.shields.io/badge/dependencies-stdlib%20only-green.svg)]()

---

## Why ArcTeam

Individual agents operate effectively in isolation. But they cannot communicate, share work, build on each other's outputs, or coordinate complex multi-step workflows. Without a collaboration layer, operators must manually orchestrate every interaction between agents.

ArcTeam solves this with the minimal set of collaboration primitives that let agents self-organize, delegate, report, and build shared institutional knowledge — the same way a human team operates, optimized for machine participants.

- **Agent-native** — Structured envelopes over natural language parsing. Explicit action types over inference. Programmatic refs over keyword search. Agents parse metadata directly — no LLM calls for routing decisions.
- **Zero external dependencies** — Python 3.11+ standard library only. No databases, no message brokers, no infrastructure. Deploy on an air-gapped laptop or a classified network.
- **Audited by default** — Every write operation across every subsystem emits an append-only audit entry. Actor, action, target, timestamp. NIST 800-53 AU-2, AU-3, AU-6, AU-9 compliant out of the box.
- **Swappable storage** — Start on flat files. Move to SQLite when you hit 100 agents. Move to PostgreSQL at 10,000. Zero consumer code changes. The StorageBackend protocol abstracts everything.
- **Universal addressing** — `agent://`, `task://`, `kb://`, `file://`, `msg://`, `channel://`, `role://` — typed URIs connect every entity across every subsystem. Full bidirectional reference traversal.

---

## The Four Primitives

### 1. Messaging — Email for Agents

Asynchronous, persistent, structured messaging. Like email, but designed for machines.

```python
# Send a high-priority task message with cross-references
await ctx.messaging.send(
    to=["channel://project-alpha"],
    msg_type="task",
    priority="high",
    subject="Analyze CMMC-qualified vendors",
    body="Evaluate all vendors in KB tagged cmmc for current certification status.",
    action_required=True,
    refs=["task://task_042", "kb://vendors/cmmc-qualified"],
)

# Agent wakes up, drains inbox, triages by priority
entries = await ctx.messaging.drain_inbox()
for entry in entries:
    if entry.priority == "critical" or entry.action_required:
        message = await ctx.messaging.get(entry.message_id)
        # Process, then mark acted
        await ctx.messaging.mark_acted(entry.message_id)
```

**Addressing model:**

| Scheme | Behavior | Example |
|--------|----------|---------|
| Direct | Point-to-point to a specific entity's inbox | `agent://procurement-01` |
| Channel | Delivered to all channel members | `channel://project-alpha` |
| Role | Broadcast to all entities with matching role | `role://procurement` |

**Message types:** `info`, `request`, `task`, `result`, `alert`, `ack` — each with explicit expected-response semantics. No ambiguity about what the sender wants.

**Threading:** Every message belongs to a thread. Replies inherit `thread_id`. Load a full conversation with `ctx.messaging.thread(thread_id)`. Threads are a query filter, not a separate data structure — zero storage overhead.

**Lifecycle tracking:** `sent` → `delivered` → `read` → `acted`. Per-recipient status. Action-required messages tracked separately so agents can prioritize.

---

### 2. Task Engine — Structured Work Management

Every piece of work has an owner, a status, and traceability to its outputs. Tasks bridge messaging (how work is requested) and the KB/file store (where results land).

```python
# Create a task with subtask decomposition
task = await ctx.tasks.create(
    title="Vendor CMMC Compliance Audit",
    description="Evaluate all vendors for current CMMC Level 2 certification.",
    assigned_to=["agent://procurement-01"],
    priority="high",
    due="2026-03-01T00:00:00Z",
    refs=["kb://reference/nist-800-53-mapping"],
)

# Decompose into subtasks and delegate
await ctx.tasks.add_subtask(task.id, title="Pull vendor list", assigned_to="agent://analyst-01")
await ctx.tasks.add_subtask(task.id, title="Verify certifications", assigned_to="agent://analyst-02")
await ctx.tasks.add_subtask(task.id, title="Compile report", assigned_to="agent://procurement-01")

# Complete with outputs linked
await ctx.tasks.update(
    task.id,
    status="review",
    outputs=["kb://entities/vendors/cmmc-audit-2026", "file://reports/cmmc-audit-2026.xlsx"],
)
```

**Task lifecycle:**

```
pending ──► assigned ──► in_progress ──► review ──► complete
  │            │              │              │
  │            │              │              └──► in_progress (rework)
  └──► cancelled └──► blocked ─┘──► blocked
```

**Subtask delegation:** Agents break complex tasks into subtasks and assign them to other agents. Assignments generate messaging notifications. Receiving agents see it as a `task`-type message with a ref to the parent task. Fully traceable delegation chain.

**Task-message integration:** Task status changes notify watchers via inbox. Comments notify assignees and watchers. Task outputs are cross-referenced via URIs — an agent completing a task links the KB entry and file it produced.

---

### 3. Knowledge Base — Institutional Memory for Agents

Not a wiki for humans. Structured institutional memory designed for how agents discover, consume, and contribute knowledge.

```python
# Write a structured KB entry
await ctx.kb.add(
    path="entities/vendors/acme-corp",
    title="Vendor Profile - Acme Corp",
    entry_type="entity",
    tags=["vendor", "cmmc", "manufacturing", "cleared"],
    summary="Acme Corp is a cleared defense manufacturer based in Huntsville, AL. "
            "CMMC Level 2 certified (exp. 2027-03). Annual revenue $45M. "
            "Supplies precision machined components for missile defense systems.",
    confidence=0.9,
    links=["kb://processes/vendor-onboarding", "kb://decisions/cmmc-vendor-selection"],
    refs=["task://task_042", "file://vendors/acme-corp-profile.xlsx"],
    body="# Vendor Profile: Acme Corp\n\n## Overview\n..."
)

# Search by tags and type
results = await ctx.kb.search(tags=["cmmc", "vendor"], entry_type="entity")

# Traverse bidirectional links
entry = await ctx.kb.read("entities/vendors/acme-corp")
backlinks = entry.backlinks  # What references this entry?
```

| Aspect | Human KB | ArcTeam KB |
|--------|----------|------------|
| Discovery | Browse, keyword search | Query by tags, type, metadata; traverse backlinks |
| Consumption | Read prose | Parse frontmatter for facts; inject summary into context |
| Contribution | Edit sections | Write structured entries with confidence, sources, verification dates |
| Currency | Manual review | `last_verified` field; confidence decays; agents flag stale entries |
| Linking | One-directional | Bidirectional: every link creates a backlink automatically |

**Entry types:** `fact`, `process`, `entity`, `decision`, `template`, `reference` — each serving a distinct role in the knowledge graph.

**Agent-optimized summaries:** Max 200 words. Factual statements only. Key entities, dates, and numbers included. Written as if injecting into an LLM context window — every word earns its place.

**Bidirectional linking:** When Entry A links to Entry B, a backlink from B to A is automatically created. Agents traverse the knowledge graph in both directions. Starting from a vendor entry, find all decisions that reference it, all processes that involve it, and all tasks related to it.

---

### 4. File Store — Organized Artifact Storage

Agent-produced and user-uploaded files with a searchable manifest.

```python
# Store a deliverable with full metadata
await ctx.files.add(
    path="projects/alpha/vendor-analysis.xlsx",
    description="CMMC vendor compliance matrix - Q1 2026",
    tags=["vendor", "cmmc", "compliance"],
    project="project-alpha",
    refs=["task://task_042", "kb://entities/vendors/cmmc-audit-2026"],
)

# Search by project and tags
files = await ctx.files.search(project="project-alpha", tags=["cmmc"])

# List project structure
tree = await ctx.files.tree(depth=2)
```

The manifest provides structured metadata for discovery. Cross-references via URIs connect files to the tasks that produced them, the KB entries that describe them, and the messages that requested them.

---

## Cross-System References

The glue that connects all four primitives. Every entity across ArcTeam is addressable via typed URIs.

| Scheme | Resolves To | Example |
|--------|-------------|---------|
| `agent://` | Entity registry record | `agent://procurement-01` |
| `user://` | Entity registry record | `user://josh` |
| `channel://` | Channel definition + stream | `channel://project-alpha` |
| `role://` | Set of entities with role | `role://procurement` |
| `msg://` | Specific message | `msg://msg_20260216_abc123` |
| `task://` | Task record | `task://task_042` |
| `kb://` | Knowledge base entry | `kb://vendors/acme-corp` |
| `file://` | File in file store | `file://projects/alpha/analysis.xlsx` |

**Reference traversal** — how agents build context:

1. Agent receives task message with `refs: ["task://task_042"]`
2. Agent loads task_042, finds `refs: ["kb://vendors/cmmc-qualified"]`
3. Agent loads KB entry, finds `links: ["kb://processes/vendor-onboarding"]`
4. Agent now has full context: the task, the relevant knowledge, and the applicable process

This graph traversal replaces what would traditionally require a complex briefing document or manual context transfer between agents.

**Backlink resolution** — given any URI, the system answers "what references this?":
- "What tasks reference this KB entry?"
- "What messages mention this task?"
- "What KB entries link to this vendor?"

---

## ARC Agent Integration

ArcTeam integrates with [ArcAgent](../arcagent/) via a plugin that hooks into the agent lifecycle and exposes subsystem operations as LLM-callable tools.

### Lifecycle Hooks

| Hook | Trigger | Action |
|------|---------|--------|
| `on_agent_start` | Agent initialization | Drain inbox, load assigned tasks, inject into context |
| `on_agent_idle` | Between task executions | Check for new messages and task assignments |
| `on_task_complete` | Agent finishes task | Update status, send result to watchers, link outputs |
| `on_agent_shutdown` | Agent shutting down | Send status message, update in-progress tasks |

### Context Injection

On agent start, the plugin injects a structured context block:

```
## Your Current Context

### Unread Messages (3)
- [HIGH/task] from user://josh: "Analyze CMMC vendors" (action required)
- [NORMAL/info] from agent://ops-lead: "Weekly ops summary attached"
- [NORMAL/result] from agent://analyst-01: "Vendor deep dive complete"

### Active Tasks (2)
- task_042: "Analyze CMMC-qualified vendors" [in_progress] due 2026-02-17
- task_045: "Update vendor onboarding process" [assigned] due 2026-02-20
```

The agent's LLM then decides autonomously how to proceed — which messages to process first, which tasks to work on, what KB entries to consult.

### LLM Tools Exposed

| Tool | Parameters | Returns |
|------|-----------|---------|
| `send_message` | to, body, msg_type, priority, refs | Message ID |
| `read_inbox` | unread_only, limit | List of InboxEntry |
| `reply_to_message` | message_id, body, msg_type | Reply Message ID |
| `search_kb` | query, tags, type | List of KB summaries |
| `read_kb` | entry_id | Full KB entry |
| `write_kb` | path, title, type, tags, body | Entry ID |
| `list_tasks` | status, assigned_to | Task summaries |
| `update_task` | task_id, status, outputs | Updated task |
| `create_task` | title, description, assigned_to | Task ID |
| `list_files` | project, tags | File manifest entries |
| `save_file` | path, content, description, tags | File path |

---

## Security Architecture

Built for federal deployment environments with strict security requirements. No custom cryptography. Defense in depth at every layer.

### NIST 800-53 Control Mapping

| Control Family | Controls | Implementation |
|----------------|----------|----------------|
| **AU (Audit)** | AU-2, AU-3, AU-6, AU-9, AU-12 | Append-only `audit.jsonl`; every write operation logged with actor, action, target, timestamp; audit file never modified through normal operations |
| **AC (Access Control)** | AC-2, AC-3, AC-6 | Entity registry with roles; role-based channel access; path-scoped permissions per subsystem |
| **IA (Identification)** | IA-2, IA-4, IA-8 | Unique entity IDs (`agent://`, `user://`); all actions attributed to specific entities; ARC Agent identity carries forward |
| **SC (System/Comms)** | SC-8, SC-13, SC-28 | TLS for distributed agents; OS-level encryption at rest (LUKS/BitLocker); no custom crypto |
| **SI (System Integrity)** | SI-3, SI-4, SI-10 | Input sanitization on all writes; schema validation for messages and tasks; content inspection for injection attempts |
| **CM (Configuration)** | CM-2, CM-6, CM-8 | All configuration in version-controlled JSON; git integration for change tracking; system inventory via entity registry |

### Access Control

Role-based access control stored in `.arc/team/security/acl.json`:

```json
{
  "roles": {
    "admin": {
      "messaging": ["*"],
      "tasks": ["*"],
      "kb": ["*"],
      "files": ["*"]
    },
    "procurement": {
      "messaging": ["send", "read", "drain"],
      "tasks": ["read", "update", "comment"],
      "kb": ["read", "write:vendors/*", "write:processes/*"],
      "files": ["read", "write:projects/*"]
    }
  }
}
```

### Input Sanitization

All agent-generated content passes through validation before storage:

- **Messages:** Stripped of control characters, length-limited, no embedded instructions that could confuse downstream LLMs
- **KB entries:** Frontmatter validated against schema, Markdown body scanned for injection patterns
- **Tasks:** Schema-validated, enum fields checked against allowed values
- **Files:** Filenames sanitized (no path traversal), descriptions length-limited

### Threat Mitigations

| Threat | Mitigation |
|--------|------------|
| Agent prompt injection via message | Messages stored as data, not executed; routing decisions use structured fields, not body text |
| Unauthorized KB modification | RBAC restricts write access by role and path; all changes audited with actor attribution |
| Audit log tampering | Append-only file; OS file permissions; integrity verification via checksum chain |
| File system path traversal | All paths sanitized and resolved relative to store root; no absolute paths or `..` allowed |
| Denial of service (message flood) | Per-entity rate limiting; inbox size limits with oldest-first eviction |
| Data exfiltration | Role-based read restrictions; sensitive KB entries tagged with classification; audit log tracks all reads |

---

## Storage Architecture

ArcTeam sits on a storage abstraction layer. Each subsystem interacts with data through a `StorageBackend` protocol. This is the single most important architectural decision — the entire system starts on flat files and migrates to databases without changing any business logic.

### StorageBackend Interface

| Method | Signature | Description |
|--------|-----------|-------------|
| `read` | `read(collection, key) -> dict` | Read a single record |
| `write` | `write(collection, key, data) -> None` | Atomic write (tmp + rename) |
| `delete` | `delete(collection, key) -> bool` | Delete a record |
| `append` | `append(collection, key, entry) -> None` | Append to JSONL stream (file-locked) |
| `read_stream` | `read_stream(collection, key, after?, limit?) -> list[dict]` | Read stream with time filter |
| `query` | `query(collection, filters?, prefix?) -> list[dict]` | Query by field match or prefix |
| `list_keys` | `list_keys(collection, prefix?) -> list[str]` | List all keys in a collection |
| `exists` | `exists(collection, key) -> bool` | Check record existence |

### Backend Implementations

| Backend | Agent Capacity | Dependencies | Deployment |
|---------|---------------|--------------|------------|
| **FileBackend** | 1-50 agents | stdlib only | Air-gapped, single node |
| **SQLiteBackend** | 50-500 agents | sqlite3 (stdlib) | Single node, FTS5 search |
| **PostgresBackend** | 2,000-10,000+ agents | PostgreSQL | Multi-node, horizontal scale |

Swap backends with a single config change. Zero consumer code changes.

### Data Directory Structure

```
.arc/team/
├── messages/
│   ├── channels/                    # Channel definitions
│   │   └── {channel-name}.json
│   ├── streams/                     # Message content (JSONL)
│   │   ├── channel/{name}.jsonl
│   │   ├── direct/{a}__{b}.jsonl
│   │   └── role/{role}.jsonl
│   ├── inboxes/                     # Per-entity inbox queues
│   │   └── {entity_id}.jsonl
│   └── registry/                    # Entity definitions
│       └── {entity_id}.json
├── tasks/
│   ├── _board.json                  # Task index
│   ├── active/{task_id}.json
│   ├── completed/{task_id}.json
│   └── templates/{name}.json
├── kb/
│   ├── _index.json                  # Full tree + metadata index
│   ├── _backlinks.json              # Backlink index
│   ├── processes/*.md
│   ├── entities/**/*.md
│   ├── decisions/*.md
│   └── reference/*.md
├── files/
│   ├── _manifest.json               # File registry
│   ├── projects/**/*
│   ├── templates/*
│   └── reports/*
├── security/
│   └── acl.json                     # Role-based permissions
└── audit/
    └── audit.jsonl                  # Append-only audit trail
```

---

## CLI Reference

All commands via `arc-team`. Global options: `--root PATH` (data directory), `--as ENTITY_ID` (act as entity).

### Messaging

| Command | Description |
|---------|-------------|
| `arc-team register ID` | Register an agent or user (`--roles`, `--name`) |
| `arc-team send` | Send a message (`--to`, `--body`, `--type`, `--priority`, `--action`, `--refs`) |
| `arc-team inbox` | Check inbox (`--all` includes read) |
| `arc-team drain` | Drain inbox, mark all read |
| `arc-team read` | Read channel/DM history (`--channel`, `--dm`) |
| `arc-team thread ID` | View message thread |
| `arc-team actions` | View pending action items |

### Tasks

| Command | Description |
|---------|-------------|
| `arc-team task list` | Show task board (`--mine`, `--status`, `--priority`) |
| `arc-team task show ID` | View task detail |
| `arc-team task create` | Create task (`--title`, `--assign`, `--priority`, `--due`) |
| `arc-team task update ID` | Update status (`--status`, `--output`) |
| `arc-team task comment ID` | Add comment (`--body`) |

### Knowledge Base

| Command | Description |
|---------|-------------|
| `arc-team kb tree` | Show KB structure (`--depth`) |
| `arc-team kb search QUERY` | Search by tags/content (`--type`, `--tags`) |
| `arc-team kb read PATH` | Read entry (`--summary-only`) |
| `arc-team kb add` | Create entry (`--path`, `--title`, `--type`, `--tags`) |
| `arc-team kb link FROM TO` | Create bidirectional link |
| `arc-team kb backlinks PATH` | Show what links to this entry |

### File Store

| Command | Description |
|---------|-------------|
| `arc-team files tree` | Show file structure |
| `arc-team files search QUERY` | Search manifest (`--project`, `--tags`) |
| `arc-team files add` | Add file (`--path`, `--file`, `--tags`, `--project`) |
| `arc-team files info PATH` | Show file metadata |

---

## Performance

| Metric | Target | Phase 1 |
|--------|--------|---------|
| Concurrent agents | 10,000+ (Phase 4) | 1-50 |
| Message delivery latency (local) | < 10ms | < 5ms |
| Inbox drain (100 messages) | < 100ms | < 50ms |
| Storage backend swap | Zero consumer code changes | FileBackend |
| Audit trail completeness | 100% of write operations | 100% |
| Recovery from unclean shutdown | No data loss on committed writes | Atomic writes |

---

## Scaling Roadmap

| Phase | Agents | Storage | Key Capability |
|-------|--------|---------|----------------|
| **1. Flat Files** | 1-50 | JSON/JSONL/Markdown | Air-gapped, zero dependencies, single node |
| **2. SQLite** | 50-500 | SQLite WAL + Markdown KB | Full-text search (FTS5), concurrent reads |
| **3. SQLite + Sharding** | 500-2,000 | Sharded SQLite, ZMQ notifications | Real-time presence, agent status |
| **4. Distributed** | 2,000-10,000+ | PostgreSQL, NATS/Redis Streams | Multi-node, horizontal scale |

Each phase is a StorageBackend swap. Your agent code, CLI commands, and business logic remain unchanged.

---

## How It Fits in the ARC Stack

```
┌─────────────────────────────────────────────────┐
│  ARC CLI                                        │
│  Human interface to all ARC subsystems          │
├─────────────────────────────────────────────────┤
│  ArcTeam                           ◄── here     │
│  Messaging · Tasks · KB · Files                 │
│  Multi-agent collaboration at scale             │
├─────────────────────────────────────────────────┤
│  ArcAgent                                       │
│  Identity · Config · Tools · Memory · Modules   │
│  The agent nucleus                              │
├─────────────────────────────────────────────────┤
│  ArcRun                                         │
│  Execution loop · Sandbox · Events · Strategies │
│  model + tools + task ──► result                │
├─────────────────────────────────────────────────┤
│  ArcLLM                                         │
│  Provider abstraction · Security · Telemetry    │
│  11 providers, 2 dependencies, <1ms overhead    │
└─────────────────────────────────────────────────┘
```

ArcTeam orchestrates multiple ArcAgent instances. It does not replace or duplicate agent internals. ArcTeam owns collaboration. ArcAgent owns identity, tools, and memory. ArcRun owns execution. ArcLLM owns LLM transport.

---

## Compliance

ArcTeam supports authorization under:

- **FedRAMP** — Federal Risk and Authorization Management
- **NIST 800-53** — Security and Privacy Controls (AU, AC, IA, SC, SI, CM families mapped)
- **CMMC** — Cybersecurity Maturity Model Certification
- **OWASP LLM Top 10 (2025)** — Prompt injection, excessive agency, unbounded consumption mitigations
- **OWASP Agentic AI Top 10 (2026)** — Rogue agents, inter-agent comms, cascading failure mitigations

Air-gapped deployment. On-premises storage. No external dependencies. No data leaves your network.

---

## Install

```bash
pip install -e "."
```

With dev tools:

```bash
pip install -e ".[dev]"
```

**Requirements:** Python 3.11+. Standard library only — no external dependencies for Phase 1.

---

## Development

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Tests
pytest -v
pytest --cov=arcteam

# Type checking
mypy src/arcteam --strict

# Linting
ruff check src/arcteam
ruff format src/arcteam
```

### Quality Thresholds

| Metric | Target |
|--------|--------|
| Core LOC | < 2,000 |
| Test coverage | >= 80% |
| Core component coverage | >= 90% |
| Cyclomatic complexity | <= 10 per function |
| Critical vulnerabilities | 0 |
| mypy strict | 0 errors |
| Ruff | 0 errors |

---

## License

This project is licensed under the [Creative Commons Attribution-NoDerivatives 4.0 International License (CC BY-ND 4.0)](https://creativecommons.org/licenses/by-nd/4.0/).

You are free to use and share this software, provided you give appropriate credit. You may not distribute modified versions.

Copyright (c) 2025 BlackArc Systems / CTG Federal.
