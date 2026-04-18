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
│  5 Primitives · 10K+ Agents · Zero External Deps    │
╰──────────────────────────────────────────────────────╯
```

**The collaboration backbone for autonomous agent organizations.** ArcTeam provides five primitives that mirror how humans collaborate — messaging, tasks, knowledge, files, and team memory — built for machine consumption with human oversight.

Agents communicate via async messaging, coordinate work through structured tasks, share institutional knowledge through a bidirectionally-linked knowledge base, produce organized file artifacts, and build shared team memory with graph-based search. Every operation is audited. Every entity is addressable via typed URIs. Every subsystem scales independently.

```python
from arcteam import TeamContext

ctx = TeamContext(root="~/.arc/team", identity="agent://procurement-01")

# Drain inbox on wake-up
messages = await ctx.messaging.drain_inbox()

# Read assigned tasks
tasks = await ctx.tasks.list(status="assigned", assigned_to="agent://procurement-01")

# Search team memory for context
vendors = await ctx.memory.search("cmmc vendor qualification")

# Search knowledge base for context
vendors = await ctx.kb.search(tags=["cmmc", "vendor"])

# Produce file artifacts
await ctx.files.add("projects/alpha/analysis.xlsx", tags=["vendor", "cmmc"])
```

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Lines of Code](https://img.shields.io/badge/lines-~3,500-brightgreen.svg)]()

---

## Why ArcTeam

Individual agents operate effectively in isolation. But they cannot communicate, share work, build on each other's outputs, or coordinate complex multi-step workflows. Without a collaboration layer, operators must manually orchestrate every interaction between agents.

ArcTeam solves this with the minimal set of collaboration primitives that let agents self-organize, delegate, report, and build shared institutional knowledge — the same way a human team operates, optimized for machine participants.

- **Agent-native** — Structured envelopes over natural language parsing. Explicit action types over inference. Programmatic refs over keyword search. Agents parse metadata directly — no LLM calls for routing decisions.
- **Zero external dependencies** — Python 3.12+ standard library plus Pydantic. No databases, no message brokers, no infrastructure. Deploy on an air-gapped laptop or a classified network.
- **Audited by default** — Every write operation across every subsystem emits an append-only audit entry. Actor, action, target, timestamp. NIST 800-53 AU-2, AU-3, AU-6, AU-9 compliant out of the box.
- **Team memory** — BM25 search with wiki-link graph traversal. Agents build shared knowledge that grows smarter over time. Promotion gates ensure quality before agent-local knowledge becomes team-shared.
- **Swappable storage** — Start on flat files. Move to SQLite when you hit 100 agents. Move to PostgreSQL at 10,000. Zero consumer code changes. The StorageBackend protocol abstracts everything.
- **Universal addressing** — `agent://`, `task://`, `kb://`, `file://`, `msg://`, `channel://`, `role://` — typed URIs connect every entity across every subsystem. Full bidirectional reference traversal.

---

## The Five Primitives

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

**Lifecycle tracking:** `sent` -> `delivered` -> `read` -> `acted`. Per-recipient status. Action-required messages tracked separately so agents can prioritize.

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
pending --> assigned --> in_progress --> review --> complete
  |            |              |              |
  |            |              |              └--> in_progress (rework)
  └--> cancelled └--> blocked -┘--> blocked
```

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
    summary="Acme Corp is a cleared defense manufacturer based in Huntsville, AL.",
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

**Bidirectional linking:** When Entry A links to Entry B, a backlink from B to A is automatically created. Agents traverse the knowledge graph in both directions.

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
```

---

### 5. Team Memory — Shared Intelligence (New in 0.2.0)

Persistent team-level knowledge management with graph-based search. Agents build shared understanding that grows smarter over time.

```python
from arcteam import TeamMemoryService, TeamMemoryConfig

config = TeamMemoryConfig(root=Path("~/.arc/team"))
memory = TeamMemoryService(config)

# Search team memory with BM25 + wiki-link traversal
results = await memory.search("cmmc vendor qualification", max_results=20)

for result in results:
    print(f"{result.entity_id} (score={result.score:.3f}, hops={result.hops})")
    print(f"  {result.snippet[:80]}")
```

**Key capabilities:**

- **BM25 text search** — Fast, accurate full-text search across all team entities.
- **Wiki-link graph traversal** — Results expand through linked entities. Multi-hop discovery surfaces contextually relevant knowledge the query didn't directly match.
- **Promotion gates** — Quality thresholds control what agent-local knowledge gets promoted to team-shared. Confidence scoring, deduplication, and optional review workflows.
- **Data classification** — Entity-level classification (CUI/FOUO/Unclassified) for compartmented knowledge access.
- **Index management** — Incremental index updates with dirty-state tracking and full rebuild capability.
- **Standalone CLI** — `arc-memory` entry point for independent team memory management.

```bash
# Via arccmd (the `arc` CLI)
arc team memory status              # entity count, index health
arc team memory search "vendors"    # BM25 search
arc team memory entities --type entity  # list by type
arc team memory entity vendor-001   # show entity details
arc team memory rebuild-index       # force index rebuild
```

---

## Cross-System References

The glue that connects all five primitives. Every entity across ArcTeam is addressable via typed URIs.

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
4. Agent searches team memory for related knowledge
5. Agent now has full context: the task, the relevant knowledge, the applicable process, and team intelligence

---

## Security Architecture

Built for federal deployment environments with strict security requirements. No custom cryptography. Defense in depth at every layer.

### NIST 800-53 Control Mapping

| Control Family | Controls | Implementation |
|----------------|----------|----------------|
| **AU (Audit)** | AU-2, AU-3, AU-6, AU-9, AU-12 | Append-only `audit.jsonl`; every write operation logged with actor, action, target, timestamp |
| **AC (Access Control)** | AC-2, AC-3, AC-6 | Entity registry with roles; role-based channel access; path-scoped permissions |
| **IA (Identification)** | IA-2, IA-4, IA-8 | Unique entity IDs; all actions attributed to specific entities |
| **SC (System/Comms)** | SC-8, SC-13, SC-28 | TLS for distributed agents; OS-level encryption at rest |
| **SI (System Integrity)** | SI-3, SI-4, SI-10 | Input sanitization on all writes; schema validation for messages and tasks |
| **CM (Configuration)** | CM-2, CM-6, CM-8 | All configuration in version-controlled JSON |

### Input Sanitization

All agent-generated content passes through validation before storage:

- **Messages:** Stripped of control characters, length-limited, no embedded instructions
- **KB entries:** Frontmatter validated against schema, Markdown body scanned for injection patterns
- **Tasks:** Schema-validated, enum fields checked against allowed values
- **Files:** Filenames sanitized (no path traversal), descriptions length-limited
- **Memory entities:** NFKC normalization, zero-width character stripping, control character removal

### Threat Mitigations

| Threat | Mitigation |
|--------|------------|
| Agent prompt injection via message | Messages stored as data, not executed; routing uses structured fields |
| Unauthorized KB modification | RBAC restricts write access by role and path; all changes audited |
| Audit log tampering | Append-only file; OS file permissions; integrity verification |
| File system path traversal | All paths sanitized and resolved relative to store root |
| Denial of service (message flood) | Per-entity rate limiting; inbox size limits |
| Data exfiltration | Role-based read restrictions; classification-tagged entries; audit on all reads |
| Memory poisoning | NFKC normalization; promotion gates; confidence thresholds |

---

## How It Fits in the Arc Stack

```
┌─────────────────────────────────────────────────┐
│  arccmd                                          │
│  Human interface to all Arc subsystems           │
├─────────────────────────────────────────────────┤
│  ArcTeam                           <-- here     │
│  Messaging · Tasks · KB · Files · Memory        │
│  Multi-agent collaboration at scale             │
├─────────────────────────────────────────────────┤
│  ArcAgent                                       │
│  Identity · Config · Tools · Memory · Modules   │
│  The agent nucleus                              │
├─────────────────────────────────────────────────┤
│  ArcRun                                         │
│  Execution loop · Sandbox · Events · Strategies │
│  model + tools + task --> result                 │
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

**Requirements:** Python 3.12+. Minimal dependencies (Pydantic, python-frontmatter, rank-bm25).

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

This project is licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).

Copyright (c) 2025-2026 BlackArc Systems.
