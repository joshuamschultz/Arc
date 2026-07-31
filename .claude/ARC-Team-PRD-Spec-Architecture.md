# ARC TEAM

## Agentic Collaboration Framework

**Product Requirements • Technical Specification • Architecture • Roadmap**

Version 1.0 | February 2026 | BlackArc Systems

**CONFIDENTIAL**

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Product Requirements](#2-product-requirements)
3. [System Architecture](#3-system-architecture)
4. [Messaging Subsystem — Detailed Specification](#4-messaging-subsystem--detailed-specification)
5. [Task Engine — Detailed Specification](#5-task-engine--detailed-specification)
6. [Knowledge Base — Detailed Specification](#6-knowledge-base--detailed-specification)
7. [File Store — Detailed Specification](#7-file-store--detailed-specification)
8. [Cross-System Reference Architecture](#8-cross-system-reference-architecture)
9. [Security Architecture](#9-security-architecture)
10. [ARC Agent Integration](#10-arc-agent-integration)
11. [CLI Reference](#11-cli-reference)
12. [Scaling Roadmap](#12-scaling-roadmap)
13. [Open Questions and Design Decisions](#13-open-questions-and-design-decisions)
14. [Appendix A: Complete Data Directory Structure](#appendix-a-complete-data-directory-structure)
15. [Appendix B: Message Flow Examples](#appendix-b-message-flow-examples)

---

## 1. Executive Summary

ARC Team is the collaboration layer for the ARC platform, enabling thousands of autonomous agents to work together as an organization. It provides four core primitives that mirror how humans collaborate—messaging, tasks, knowledge, and files—built for agent consumption with human oversight.

The system operates as a thin coordination backbone, not a monolithic platform. Each subsystem is minimal, file-backed, and independently scalable. Agents communicate via an email-like async messaging system, coordinate work through structured tasks, share institutional knowledge through a bidirectionally-linked markdown knowledge base, and produce organized file artifacts.

ARC Team is designed for enterprise and federal deployment environments with strict security requirements. It uses local, on-premises storage with no external dependencies, adheres to NIST 800-53, OWASP, and CMMC guidelines, and integrates with the existing ARC LLM, ARC Run, and ARC Agent libraries.

### Design Principles

**Simplicity:** Minimal code, minimal dependencies. Standard library Python with flat-file storage. No databases, message brokers, or infrastructure required for initial deployment.

**Security:** NIST 800-53 compliant audit trails, role-based access control, append-only logging, input sanitization, and encryption-at-rest via OS-level tools. Built for FedRAMP and CMMC environments.

**Scalability:** Storage abstraction allows transparent backend swaps from flat files to SQLite to PostgreSQL. Architecture supports 10,000+ concurrent agents through sharding and optional message brokers.

**Agent-Native:** Every interface is designed for machine consumption first. Structured envelopes over natural language parsing. Explicit action types over inference. Programmatic refs over keyword search.

---

## 2. Product Requirements

### Problem Statement

Individual ARC agents operate effectively in isolation but cannot collaborate. There is no mechanism for agents to communicate, share work, build on each other's outputs, or coordinate complex multi-step workflows. Users must manually orchestrate agent interactions, transfer context between agents, and track the state of distributed work.

ARC Team solves this by providing the minimal set of collaboration primitives that allow agents to self-organize, delegate, report, and build shared institutional knowledge—the same way a human team operates, but optimized for machine participants.

### Target Users

| User Type | Description | Primary Interactions |
|---|---|---|
| ARC Agents | Autonomous agents executing tasks, producing deliverables, and collaborating with other agents | Send/receive messages, create/complete tasks, read/write KB, produce files |
| Human Operators | Engineers and administrators overseeing agent operations | Assign tasks, review outputs, manage KB, configure channels and roles |
| Human Executives | Leadership reviewing agent-produced work and making decisions | Read reports, approve deliverables, assign high-level directives |
| System Administrators | IT/security staff managing the ARC Team deployment | Configure ACLs, review audit logs, manage entity registry |

### Functional Requirements

#### FR-1: Messaging System

An asynchronous, persistent, email-like messaging system for agents and humans.

| ID | Requirement | Priority |
|---|---|---|
| FR-1.1 | Agents and users can send messages to specific entities (direct), named channels, or role-based groups | P0 |
| FR-1.2 | Each entity has a persistent inbox that accumulates messages when offline | P0 |
| FR-1.3 | Messages carry structured metadata: type, priority, action_required flag, and cross-references | P0 |
| FR-1.4 | Agents can drain their inbox on wake-up or cron schedule, receiving all unread messages | P0 |
| FR-1.5 | Messages support threading via reply_to and thread_id for conversation continuity | P0 |
| FR-1.6 | Role-based broadcast delivers to all entities with a matching role without knowing specific IDs | P0 |
| FR-1.7 | Action-required messages are tracked separately; agents can mark them as acted-upon | P1 |
| FR-1.8 | Message lifecycle: sent → delivered → read → acted (each state tracked per recipient) | P1 |
| FR-1.9 | Messages can reference tasks, KB entries, files, and other messages via typed URIs | P0 |
| FR-1.10 | CLI provides send, inbox, drain, read (channel/DM), thread, and action-tracking commands | P0 |

#### FR-2: Task Engine

Structured task management for assigning, tracking, and completing work across agents and humans.

| ID | Requirement | Priority |
|---|---|---|
| FR-2.1 | Tasks can be created by any entity (agent or user) with title, description, assignees, and due dates | P0 |
| FR-2.2 | Tasks support subtask decomposition; agents can break tasks into smaller units and assign to other agents | P0 |
| FR-2.3 | Task lifecycle: pending → assigned → in_progress → review → complete \| blocked \| cancelled | P0 |
| FR-2.4 | Tasks carry an outputs field linking to produced deliverables (KB entries, files) | P0 |
| FR-2.5 | Task comments provide a threaded discussion attached to each task | P0 |
| FR-2.6 | Watchers receive inbox notifications on task status changes and new comments | P1 |
| FR-2.7 | Tasks support simple dependency tracking (list of prerequisite task IDs) | P1 |
| FR-2.8 | CLI provides list, show, create, update, comment, and assign commands | P0 |
| FR-2.9 | Task assignments generate messaging notifications to assignees | P0 |
| FR-2.10 | Tasks can be created directly from messages (msg_type=task generates a task record) | P1 |

#### FR-3: Knowledge Base

A bidirectionally-linked markdown knowledge base optimized for agent consumption. This is not a wiki for humans—it is structured institutional memory designed for how agents discover, consume, and contribute knowledge.

| ID | Requirement | Priority |
|---|---|---|
| FR-3.1 | KB entries are Markdown files with YAML frontmatter containing structured metadata | P0 |
| FR-3.2 | Entries support bidirectional linking: any entry can link to others, and backlinks are automatically tracked | P0 |
| FR-3.3 | Entries are organized in a hierarchical directory tree with index files at each level | P0 |
| FR-3.4 | Tags provide cross-cutting categorization independent of directory structure | P0 |
| FR-3.5 | Each entry carries agent-oriented metadata: confidence level, last_verified date, source references, and change history | P0 |
| FR-3.6 | Entries have a structured summary field optimized for agent context injection (concise, factual, no prose) | P0 |
| FR-3.7 | KB supports entry types: fact, process, entity, decision, template, and reference | P1 |
| FR-3.8 | Agents can create, update, and deprecate KB entries programmatically | P0 |
| FR-3.9 | CLI provides tree, search, read, edit, add, and link commands | P0 |
| FR-3.10 | KB changes generate audit log entries and optional channel notifications | P1 |

#### FR-4: File Store

Organized artifact storage for agent-produced and user-uploaded files.

| ID | Requirement | Priority |
|---|---|---|
| FR-4.1 | Files are stored in a project-oriented directory hierarchy with a manifest index | P0 |
| FR-4.2 | Manifest tracks metadata: path, type, creator, tags, description, project, and related entities | P0 |
| FR-4.3 | Agents and users can add, retrieve, and list files programmatically and via CLI | P0 |
| FR-4.4 | Files can be referenced from messages, tasks, and KB entries via file:// URIs | P0 |
| FR-4.5 | Template files can be stored and copied for standardized deliverables | P1 |
| FR-4.6 | CLI provides tree, search, open, add, and info commands | P0 |

#### FR-5: Cross-System References

A universal reference system connecting all subsystems via typed URIs.

| ID | Requirement | Priority |
|---|---|---|
| FR-5.1 | All entities across subsystems are addressable via typed URIs (msg://, task://, kb://, file://, agent://, user://, channel://, role://) | P0 |
| FR-5.2 | Messages, tasks, KB entries, and file manifests all carry a refs field for cross-linking | P0 |
| FR-5.3 | Backlink resolution: given any URI, the system can find all entities that reference it | P1 |
| FR-5.4 | Agents can traverse references programmatically to build context for their work | P0 |

#### FR-6: Entity Registry

Central registry of all agents, users, and their roles.

| ID | Requirement | Priority |
|---|---|---|
| FR-6.1 | All agents and users are registered with unique IDs, roles, and display names | P0 |
| FR-6.2 | Roles support role-based addressing (role://procurement) and access control | P0 |
| FR-6.3 | Entity metadata is extensible (capabilities, status, specializations) | P1 |
| FR-6.4 | CLI provides register, list, and role-query commands | P0 |

### Non-Functional Requirements

| ID | Requirement | Target | Priority |
|---|---|---|---|
| NFR-1 | Concurrent agents supported | 10,000+ (Phase 4) | P0 |
| NFR-2 | Message delivery latency (local) | < 10ms | P0 |
| NFR-3 | Inbox drain time (100 messages) | < 100ms | P0 |
| NFR-4 | Storage backend swappable without consumer changes | Yes | P0 |
| NFR-5 | External dependencies (Phase 1) | Zero (stdlib only) | P0 |
| NFR-6 | Audit trail completeness | 100% of write operations | P0 |
| NFR-7 | Data at rest encryption support | OS-level (LUKS/BitLocker) | P0 |
| NFR-8 | Deployment target | Air-gapped on-prem Linux | P0 |
| NFR-9 | Python version | 3.11+ | P0 |
| NFR-10 | Recovery from unclean shutdown | No data loss on committed writes | P1 |

---

## 3. System Architecture

### Architecture Overview

ARC Team is structured as four independent subsystems sitting on a shared storage abstraction layer. Each subsystem is a Python module exposing a service class and a set of CLI commands. The storage layer provides a clean interface that can be backed by flat files, SQLite, or PostgreSQL without changing any consuming code.

The architecture follows these principles:

- Each subsystem is independently deployable and testable
- All subsystems share a single StorageBackend instance for consistency
- Cross-system references use typed URIs, not foreign keys or object references
- The audit logger wraps all write operations across all subsystems
- ARC Agent integration is via a plugin that exposes subsystem operations as LLM tools

### System Diagram

```
┌───────────────────────────────────────────────────────────────────┐
│                       ARC CLI / ARC Agent Plugin                  │
│  arc team msg | arc team task | arc team kb | arc team files      │
└─────────────────────────────────┬─────────────────────────────────┘
                                  │
┌─────────────────────────────────┼─────────────────────────────────┐
│                         ARC Team Core                             │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ │
│  │  Messaging  │ │    Tasks    │ │  Knowledge  │ │    Files    │ │
│  │   Service   │ │   Engine    │ │    Base     │ │    Store    │ │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ │
│       └────────────────┴────────────────┴──────────────┘          │
│                          │                                        │
│               ┌──────────┴─────────────────┐                      │
│               │  Storage Abstraction       │                      │
│               │  + Audit Logger            │                      │
│               └──────────┬─────────────────┘                      │
└──────────────────────────┼────────────────────────────────────────┘
                           │
              ┌────────────┴───────────┐
              │ FileBackend /          │
              │ SQLiteBackend /        │
              │ PostgresBackend        │
              └────────────────────────┘
```

### Storage Abstraction Layer

All subsystems interact with data through a StorageBackend protocol. This is the single most important architectural decision—it allows the entire system to start on flat files and migrate to databases without changing any business logic.

#### StorageBackend Interface

| Method | Signature | Description |
|---|---|---|
| read | `read(collection, key) → dict \| None` | Read a single record by collection and key |
| write | `write(collection, key, data) → None` | Write/overwrite a single record (atomic) |
| delete | `delete(collection, key) → bool` | Delete a record; returns True if it existed |
| append | `append(collection, key, entry) → None` | Append an entry to a JSONL stream (file-locked) |
| read_stream | `read_stream(collection, key, after?, limit?) → list[dict]` | Read entries from a stream with optional time filter |
| query | `query(collection, filters?, prefix?) → list[dict]` | Query records by field match or key prefix |
| list_keys | `list_keys(collection, prefix?) → list[str]` | List all keys in a collection |
| exists | `exists(collection, key) → bool` | Check if a record exists |

#### FileBackend Implementation

Phase 1 storage backend using the local filesystem:

- Single records stored as JSON files (`{collection}/{key}.json`)
- Streams stored as JSONL files (`{collection}/{key}.jsonl`)—one JSON object per line
- Writes are atomic: write to `.tmp` file then `os.replace()` for rename
- Stream appends use `fcntl` file locking for concurrency safety
- Nested keys supported (e.g., `streams/channel/project-alpha`)
- Index files prefixed with underscore (`_index.json`) are excluded from queries

#### Audit Logger

Wraps all write operations across all subsystems. Append-only JSONL file that is never modified or deleted through normal operations.

Every audit entry contains: timestamp, action (e.g., `messaging.send`, `task.create`), actor (who), target (what), detail (human-readable summary), and extensible metadata.

This satisfies NIST 800-53 AU-2 (Audit Events), AU-3 (Content of Audit Records), AU-6 (Audit Review), and AU-9 (Protection of Audit Information).

---

## 4. Messaging Subsystem — Detailed Specification

### Concept: Email for Agents

The messaging system is designed around the mental model of email, optimized for autonomous agents. Like email, messages are asynchronous, persistent, self-contained, and addressable. Unlike email, the envelope carries structured metadata that agents parse directly without LLM calls, action types are explicit rather than inferred, and cross-references link to other subsystems programmatically.

**What agents need that humans don't:**

- Structured envelope metadata (type, priority, action_required) parsed without NLP
- Explicit action types: info, request, task, result, alert, ack
- Typed refs linking to tasks, KB entries, files, and other messages
- Deterministic inbox drain pattern compatible with cron scheduling

**What agents don't need:**

- Rich formatting, HTML bodies, or attachments (refs replace attachments)
- CC/BCC complexity (role-based addressing replaces distribution lists)
- Spam filtering or read receipts

### Addressing Model

| Scheme | Format | Behavior | Example |
|---|---|---|---|
| Direct | `agent://id` or `user://id` | Point-to-point delivery to a specific entity's inbox | `agent://procurement-01` |
| Channel | `channel://name` | Delivered to all channel members' inboxes (except sender) | `channel://project-alpha` |
| Role | `role://name` | Broadcast to all entities with matching role (except sender) | `role://procurement` |

Role-based addressing is critical for scale. When you have 200 procurement agents, you don't need to know their IDs—you address `role://procurement` and the system handles delivery. As agents are added or removed, addressing continues to work.

### Message Schema

| Field | Type | Required | Description |
|---|---|---|---|
| id | string | Auto | Unique message ID (`msg_{timestamp}_{hash}`) |
| ts | ISO 8601 | Auto | UTC timestamp of creation |
| sender | URI | Yes | Sender identity (`agent://x` or `user://x`) |
| to | list[URI] | Yes | Target addresses (can mix types) |
| reply_to | string \| null | No | Message ID this replies to |
| thread_id | string \| null | Auto | Root message ID of thread (auto-set) |
| msg_type | enum | Yes | `info` \| `request` \| `task` \| `result` \| `alert` \| `ack` |
| priority | enum | Default | `low` \| `normal` \| `high` \| `critical` |
| action_required | bool | Default | Whether recipient must act (false) |
| subject | string | No | Short summary for inbox triage |
| body | string | Yes | Natural language message content |
| refs | list[URI] | No | Cross-references (`task://`, `kb://`, `file://`, `msg://`) |
| status | string | Auto | `sent` \| `delivered` \| `read` \| `acted` |
| meta | dict | No | Extensible metadata for custom use |

### Message Types

| Type | Meaning | Expected Response | Use Case |
|---|---|---|---|
| info | FYI, no action needed | None | Status updates, notifications, logging |
| request | Expects a response message | Reply message | Questions, data requests, approvals |
| task | Task assignment | Task completion + result message | Work delegation (can auto-create task record) |
| result | Deliverable / work product | Ack (optional) | Completed analysis, reports, data |
| alert | Urgent, surfaces first in inbox | Varies | System alerts, critical updates, blockers |
| ack | Acknowledgment | None | Confirming receipt or action completion |

### Inbox Pattern

Each entity has exactly one inbox. All messages targeting that entity—regardless of whether they arrive via direct, channel, or role addressing—land in the inbox as lightweight InboxEntry records. The inbox is the single interface an agent uses to discover new information.

#### InboxEntry Schema

| Field | Type | Description |
|---|---|---|
| message_id | string | Reference to full message |
| ts | ISO 8601 | Message timestamp (for sort) |
| sender | URI | Who sent it |
| subject | string | For triage without loading full message |
| msg_type | string | Message type (for priority sort) |
| priority | string | Priority level |
| action_required | bool | Whether this needs action |
| channel | URI | Origin (which channel/DM/role) |
| thread_id | string \| null | Thread reference |
| read | bool | Whether entity has read this |
| acted | bool | Whether entity has acted on this |

#### Agent Wake-Up Flow

This is the canonical agent lifecycle interaction with messaging:

1. Agent wakes up (triggered by cron, event, or manual invocation)
2. Agent calls `drain_inbox()` which returns all unread entries and marks them read
3. Agent triages by priority: critical and high-priority first, then alerts, then normal
4. For each `action_required` message, agent processes and eventually calls `mark_acted()`
5. Agent may send reply messages, create tasks, update KB, or produce files
6. Agent sleeps (or enters idle loop checking for new messages)

### Threading Model

Every message belongs to a thread. New messages start a thread (`thread_id` = own id). Replies inherit the `thread_id` of the original message. An agent loading a thread gets the complete conversation history in chronological order.

Threads are not a separate data structure—they are a query filter on the stream. This means no additional storage overhead and no synchronization complexity.

### Storage Layout

```
.arc/team/messages/
├── channels/                    # channel definitions
│   ├── project-alpha.json
│   └── ops-alerts.json
├── streams/                     # message content (JSONL)
│   ├── channel/
│   │   ├── project-alpha.jsonl
│   │   └── ops-alerts.jsonl
│   ├── direct/
│   │   └── agent_proc-01__user_josh.jsonl
│   └── role/
│       └── procurement.jsonl
├── inboxes/                     # per-entity inbox queues
│   ├── agent_procurement-01.jsonl
│   └── user_josh.jsonl
└── registry/                    # entity definitions
    ├── agent_procurement-01.json
    └── user_josh.json
```

---

## 5. Task Engine — Detailed Specification

### Concept

The task engine provides structured work management for agents. Tasks are the unit of accountability—every piece of work has an owner, a status, and traceability to its outputs. Tasks bridge messaging (how work is requested) and the KB/file store (where results land).

### Task Schema

| Field | Type | Required | Description |
|---|---|---|---|
| id | string | Auto | Unique task ID (`task_{timestamp}_{hash}`) |
| title | string | Yes | Short task title |
| description | string | Yes | Full task description with context |
| status | enum | Auto | `pending` \| `assigned` \| `in_progress` \| `review` \| `complete` \| `blocked` \| `cancelled` |
| priority | enum | Default | `low` \| `normal` \| `high` \| `critical` |
| created_by | URI | Auto | Who created the task |
| assigned_to | list[URI] | Yes | Assigned agents/users |
| watchers | list[URI] | No | Entities notified on changes |
| created | ISO 8601 | Auto | Creation timestamp |
| due | ISO 8601 \| null | No | Due date/time |
| subtasks | list[Subtask] | No | Decomposed sub-work items |
| comments | list[Comment] | No | Threaded discussion on the task |
| outputs | list[URI] | No | Produced deliverables (`kb://`, `file://`) |
| deps | list[string] | No | Prerequisite task IDs |
| refs | list[URI] | No | Related messages, KB entries, files |
| meta | dict | No | Extensible metadata |

### Task Lifecycle

```
pending ───▶ assigned ───▶ in_progress ───▶ review ───▶ complete
  │            │              │              │
  │            │              │              └──▶ in_progress (rework)
  │            │              │
  └─▶ cancelled └─▶ blocked ──┘─▶ blocked
```

**Status transitions and their triggers:**

- `pending → assigned`: Assignee accepts or is auto-assigned
- `assigned → in_progress`: Agent begins work
- `in_progress → review`: Agent completes work, outputs attached
- `review → complete`: Reviewer (human or lead agent) approves
- `review → in_progress`: Rework requested with comment
- `Any → blocked`: Dependencies unmet or external blocker identified
- `Any → cancelled`: Task no longer needed

### Subtask Decomposition

Agents can break complex tasks into subtasks and assign them to other agents. This is the primary mechanism for multi-agent collaboration on complex work. Each subtask is a lightweight record within the parent task:

| Field | Type | Description |
|---|---|---|
| id | string | Subtask ID (`st_{sequence}`) |
| title | string | Short description |
| status | enum | `pending` \| `in_progress` \| `complete` \| `blocked` |
| assigned_to | URI \| null | Assigned entity |
| outputs | list[URI] | Deliverables produced |

When a subtask is assigned to a different agent, the system generates a messaging notification. The receiving agent sees it as a task-type message with a ref to the parent task. This creates a natural delegation chain that's fully traceable.

### Task-Message Integration

Tasks and messages are tightly coupled:

- When a message with `msg_type=task` is sent, the system can auto-create a corresponding task record (FR-2.10)
- Task status changes generate inbox notifications to watchers
- Task comments generate inbox notifications to assignees and watchers
- Task outputs are cross-referenced via URIs—an agent completing a task links the KB entry and file it produced

### Storage Layout

```
.arc/team/tasks/
├── _board.json         # task index (lightweight: id, title, status, assigned_to)
├── active/
│   ├── task_042.json   # full task records
│   └── task_043.json
├── completed/          # archived completed tasks
│   └── task_041.json
└── templates/          # reusable task templates
    └── rfi-response.json
```

---

## 6. Knowledge Base — Detailed Specification

### Design Philosophy: Built for Agents

The KB is not a wiki for humans. It is structured institutional memory designed for how agents discover, consume, and contribute knowledge. The key differences from a human-oriented KB:

| Aspect | Human KB (wiki-style) | Agent KB (ARC Team) |
|---|---|---|
| Discovery | Browse, search by keyword, follow links | Query by tags, type, and structured metadata; traverse backlinks programmatically |
| Consumption | Read prose, interpret context | Parse frontmatter for facts, inject summary into context window, follow refs for detail |
| Organization | Hierarchical categories, subjective placement | Type-based classification + tag-based cross-cutting + bidirectional links |
| Contribution | Edit prose, add sections | Write structured entries with confidence levels, sources, and verification dates |
| Currency | Manual review, stale content accumulates | `last_verified` field; agents flag stale entries; confidence decays over time |
| Linking | One-directional hyperlinks | Bidirectional: every link creates a backlink; agents traverse the graph |

### KB Entry Schema (Frontmatter)

Every KB entry is a Markdown file with YAML frontmatter. The frontmatter is the agent's primary interface—it provides structured data that agents parse directly. The Markdown body provides detail for deeper reading.

| Field | Type | Required | Description |
|---|---|---|---|
| id | string | Auto | Unique entry ID (`kb_{path_hash}`) |
| title | string | Yes | Entry title |
| type | enum | Yes | `fact` \| `process` \| `entity` \| `decision` \| `template` \| `reference` |
| tags | list[string] | Yes | Cross-cutting categorization |
| created | ISO 8601 | Auto | Creation date |
| modified | ISO 8601 | Auto | Last modification date |
| author | URI | Auto | Who created/last modified |
| summary | string | Yes | Agent-optimized summary (concise, factual, no prose; max 200 words) |
| confidence | float | Default | 0.0–1.0 confidence in accuracy (default 0.8) |
| last_verified | ISO 8601 | No | When this was last confirmed accurate |
| sources | list[URI] | No | Where this information came from |
| links | list[string] | No | Forward links to other KB entries (by ID or path) |
| backlinks | list[string] | Auto | Auto-populated reverse links |
| refs | list[URI] | No | References to tasks, files, messages |
| deprecated | bool | Default | Whether this entry is superseded (false) |
| superseded_by | string \| null | No | ID of entry that replaces this one |

### Entry Types

| Type | Purpose | Example |
|---|---|---|
| fact | A verified piece of information | "Acme Corp has CMMC Level 2 certification" |
| process | A documented procedure or workflow | "RFI Response Process" |
| entity | Information about a person, company, system, or concept | "Customer: DOE NNSA" |
| decision | A recorded decision with rationale and context | "Selected Vendor X for contract Y because..." |
| template | A reusable pattern or boilerplate | "Standard RFI response structure" |
| reference | External information captured for internal use | "NIST 800-53 control mapping" |

### Bidirectional Linking

This is the most important feature of the KB for agent use. When Entry A links to Entry B, a backlink from B to A is automatically created. This means agents can traverse the knowledge graph in both directions:

- **Forward:** "What does this entry reference?" → Follow `links` field
- **Backward:** "What references this entry?" → Follow `backlinks` field
- This enables agents to build rich context: starting from a vendor entry, find all decisions that reference it, all processes that involve it, and all tasks related to it

**Implementation:** On write, the KB service scans the `links` field, then updates the `backlinks` field of each target entry. On delete/deprecate, backlinks are cleaned up. The backlink index is also maintained in a separate `_backlinks.json` file for fast lookup without scanning all entries.

### Agent-Oriented Summary Field

The summary field is specifically designed for context injection. When an agent needs to understand what a KB entry contains without reading the full Markdown body, the summary provides a concise, factual description. Rules for summaries:

- Maximum 200 words
- Factual statements only, no prose or hedging
- Key entities, dates, and numbers included
- Written as if injecting into an LLM context window (every word earns its place)

### KB Entry Example

```yaml
---
id: kb_vendors_acme_corp
title: Vendor Profile - Acme Corp
type: entity
tags: [vendor, cmmc, manufacturing, cleared]
created: 2026-02-16
modified: 2026-02-16
author: agent://procurement-01
summary: >
  Acme Corp is a cleared defense manufacturer based in Huntsville, AL.
  CMMC Level 2 certified (exp. 2027-03). Primary contact: Jane Smith
  (jane@acme.com). Annual revenue $45M. Supplies precision machined
  components for missile defense systems. Current contract: W911QX-24-C-0042.
  Rated 4.2/5.0 on delivery performance (last 12 months).
confidence: 0.9
last_verified: 2026-02-16
sources: ["file://vendors/acme-corp-profile.xlsx", "msg://msg_20260215_abc123"]
links: ["kb_processes_vendor_onboarding", "kb_decisions_cmmc_vendor_selection"]
backlinks: ["kb_projects_missile_defense_supply_chain"]
refs: ["task://task_042", "file://vendors/acme-corp-profile.xlsx"]
deprecated: false
---

# Vendor Profile: Acme Corp

## Overview
Acme Corp is a precision manufacturing company specializing in...
```

### Storage Layout

```
.arc/team/kb/
├── _index.json              # full tree + metadata index
├── _backlinks.json          # backlink index for fast lookup
├── processes/
│   ├── _index.json
│   ├── vendor-onboarding.md
│   └── rfi-response.md
├── entities/
│   ├── customers/
│   │   ├── doe-nnsa.md
│   │   └── dod-dtra.md
│   └── vendors/
│       ├── acme-corp.md
│       └── cmmc-qualified.md
├── decisions/
│   └── cmmc-vendor-selection.md
└── reference/
    └── nist-800-53-mapping.md
```

---

## 7. File Store — Detailed Specification

### Concept

The file store provides organized artifact storage for agent-produced and user-uploaded files. It is intentionally simple: a directory tree with a manifest that provides searchable metadata. The system does not attempt to parse or index file contents—the manifest metadata and cross-references provide the discovery layer.

### Manifest Entry Schema

| Field | Type | Description |
|---|---|---|
| path | string | Relative path within file store |
| filename | string | Original filename |
| type | string | MIME type or extension |
| created | ISO 8601 | When the file was added |
| created_by | URI | Who created/uploaded the file |
| description | string | Human/agent-readable description |
| tags | list[string] | Categorization tags |
| project | string \| null | Associated project name |
| refs | list[URI] | Cross-references to tasks, KB entries, messages |
| size_bytes | int | File size |

### Storage Layout

```
.arc/team/files/
├── _manifest.json          # file registry
├── projects/
│   ├── project-alpha/
│   │   ├── vendor-analysis.xlsx
│   │   └── proposal-draft.docx
│   └── sunet-modernization/
│       └── technical-approach.pptx
├── templates/
│   └── rfi-response-template.docx
└── reports/
    └── monthly-ops-2026-02.xlsx
```

---

## 8. Cross-System Reference Architecture

### URI Scheme

All entities across ARC Team are addressable via typed URIs. This is the glue that connects subsystems without tight coupling.

| Scheme | Format | Resolves To |
|---|---|---|
| `agent://` | `agent://procurement-01` | Entity registry record |
| `user://` | `user://josh` | Entity registry record |
| `channel://` | `channel://project-alpha` | Channel definition + message stream |
| `role://` | `role://procurement` | Set of entities with matching role |
| `msg://` | `msg://msg_20260216_abc123` | Specific message in a stream |
| `task://` | `task://task_042` | Task record |
| `kb://` | `kb://vendors/acme-corp` | Knowledge base entry |
| `file://` | `file://projects/alpha/vendor-analysis.xlsx` | File in the file store |

### Reference Traversal

Agents use refs to build context. A typical traversal pattern:

1. Agent receives a task message with `refs: ["task://task_042"]`
2. Agent loads task_042, finds `refs: ["kb://vendors/cmmc-qualified"]`
3. Agent loads KB entry, finds `links: ["kb_processes_vendor_onboarding"]`
4. Agent now has full context: the task, the relevant knowledge, and the applicable process

This graph traversal replaces what would traditionally require a complex briefing document or manual context transfer between agents. Each ref is a pointer that agents follow programmatically.

### Backlink Resolution

Given any URI, the system can answer: "What references this?" This enables powerful reverse lookups:

- "What tasks reference this KB entry?" → Find all tasks with this KB URI in refs
- "What messages mention this task?" → Find all messages with this task URI in refs
- "What KB entries link to this vendor?" → Check the entry's backlinks field

Phase 1 implements this via brute-force scan. Phase 2 introduces a dedicated reference index for O(1) lookups.

---

## 9. Security Architecture

### Security Design Principles

- Defense in depth: OS-level encryption + application ACLs + audit logging
- Least privilege: Agents have only the permissions their role requires
- Assume breach: Append-only audit trail survives even if an agent is compromised
- Input validation: All agent-generated content is treated as untrusted
- No custom cryptography: Leverage OS and standard library crypto only

### NIST 800-53 Control Mapping

| Control Family | Controls | ARC Team Implementation |
|---|---|---|
| AU (Audit) | AU-2, AU-3, AU-6, AU-9, AU-12 | Append-only `audit.jsonl`; every write operation logged with actor, action, target, timestamp; audit file is never modified |
| AC (Access Control) | AC-2, AC-3, AC-6 | Entity registry with roles; role-based channel access; ACL manifest for fine-grained permissions |
| IA (Identification) | IA-2, IA-4, IA-8 | Unique entity IDs (`agent://` and `user://`); ARC Agent auth carries forward; all actions attributed to specific entity |
| SC (System/Comms) | SC-8, SC-13, SC-28 | TLS for distributed agents; OS-level encryption at rest (LUKS/BitLocker); no custom crypto |
| SI (System Integrity) | SI-3, SI-4, SI-10 | Input sanitization on all writes; schema validation for messages and tasks; content inspection for injection attempts |
| CM (Configuration) | CM-2, CM-6, CM-8 | All configuration in version-controlled JSON files; git integration for change tracking; system inventory via entity registry |

### Access Control Model

ARC Team uses a role-based access control (RBAC) model stored in `.arc/team/security/acl.json`:

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

All agent-generated content passes through sanitization before storage:

- **Message bodies:** Stripped of control characters; length-limited; no embedded instructions that could confuse downstream LLMs
- **KB entries:** Frontmatter validated against schema; Markdown body scanned for injection patterns
- **Task fields:** Schema-validated; string fields length-limited; enum fields validated against allowed values
- **File metadata:** Filenames sanitized (no path traversal); descriptions length-limited

### Threat Mitigations

| Threat | Mitigation |
|---|---|
| Agent prompt injection via message | Messages are stored as data, not executed; consuming agents parse structured fields, not raw body text, for routing decisions |
| Unauthorized KB modification | RBAC restricts write access by role and path; all changes audited with actor attribution |
| Audit log tampering | Append-only file; OS file permissions restrict write access; integrity verification via checksum chain |
| File system path traversal | All paths sanitized and resolved relative to store root; no absolute paths or `..` traversal allowed |
| Denial of service (message flood) | Per-entity rate limiting at the service layer; inbox size limits with oldest-first eviction |
| Data exfiltration | Role-based read restrictions; sensitive KB entries tagged with classification; audit log tracks all reads |

---

## 10. ARC Agent Integration

### Plugin Architecture

ARC Team integrates with ARC Agent via a plugin that hooks into the agent lifecycle and exposes subsystem operations as LLM-callable tools. The plugin is loaded when an agent is configured for team collaboration.

### Lifecycle Hooks

| Hook | Trigger | Action |
|---|---|---|
| on_agent_start | Agent initialization | Drain inbox, load assigned tasks, inject into agent context |
| on_agent_idle | Between task executions | Check inbox for new messages, check for new task assignments |
| on_task_complete | Agent finishes a task | Update task status, send result message to watchers, link outputs |
| on_agent_shutdown | Agent shutting down | Send status message to watchers, update any in-progress tasks |

### LLM Tools Exposed

The following tools are made available to the agent's LLM for autonomous decision-making:

| Tool | Parameters | Returns |
|---|---|---|
| send_message | to, body, msg_type, priority, refs, action_required | Message ID |
| read_inbox | unread_only, limit | List of InboxEntry |
| get_message | message_id | Full Message |
| reply_to_message | message_id, body, msg_type, refs | Reply Message ID |
| search_kb | query, tags, type | List of KB entry summaries |
| read_kb | entry_id | Full KB entry (frontmatter + body) |
| write_kb | path, title, type, tags, body, links | Entry ID |
| list_tasks | status, assigned_to | List of task summaries |
| get_task | task_id | Full task record |
| update_task | task_id, status, comment, outputs | Updated task |
| create_task | title, description, assigned_to, priority | Task ID |
| list_files | project, tags | List of file manifest entries |
| save_file | path, content, description, tags, project | File path |

### Context Injection Pattern

On agent start, the plugin injects a structured context block into the agent's system prompt:

```
## Your Current Context

### Unread Messages (3)
- [HIGH/task] from user://josh: "Analyze CMMC vendors" (action required)
- [NORMAL/info] from agent://ops-lead: "Weekly ops summary attached"
- [NORMAL/result] from agent://analyst-01: "Vendor #3 deep dive complete"

### Active Tasks (2)
- task_042: "Analyze CMMC-qualified vendors" [in_progress] due 2026-02-17
- task_045: "Update vendor onboarding process" [assigned] due 2026-02-20

### Available Tools: send_message, read_inbox, search_kb, read_kb, ...
```

The agent's LLM then decides autonomously how to proceed—which messages to process first, which tasks to work on, what KB entries to consult.

---

## 11. CLI Reference

All commands are accessed via the `arc-team` CLI. Global options:

- `--root PATH`: Data directory (default: `~/.arc/team`)
- `--as ENTITY_ID`: Act as this entity (e.g., `--as agent://procurement-01`)

### Messaging Commands

| Command | Description | Key Options |
|---|---|---|
| `arc-team register ID` | Register an agent or user | `--roles`, `--name` |
| `arc-team entities` | List registered entities | `--role` (filter) |
| `arc-team channel NAME` | Create a channel | `--members`, `--description` |
| `arc-team join CHANNEL ENTITY` | Add entity to channel | |
| `arc-team channels` | List all channels | |
| `arc-team send` | Send a message | `--to`, `--body`, `--subject`, `--type`, `--priority`, `--action`, `--refs`, `--reply-to` |
| `arc-team inbox` | Check inbox | `--all` (include read), `--limit` |
| `arc-team drain` | Drain inbox (mark all read) | |
| `arc-team read` | Read channel/DM history | `--channel`, `--dm`, `--limit` |
| `arc-team thread THREAD_ID` | View message thread | `--channel` (faster lookup) |
| `arc-team actions` | View pending action items | |

### Task Commands

| Command | Description | Key Options |
|---|---|---|
| `arc-team task list` | Show task board | `--mine`, `--status`, `--priority` |
| `arc-team task show TASK_ID` | View task detail | |
| `arc-team task create` | Create a task | `--title`, `--description`, `--assign`, `--priority`, `--due` |
| `arc-team task update TASK_ID` | Update task status | `--status`, `--output` |
| `arc-team task comment TASK_ID` | Add a comment | `--body` |
| `arc-team task assign TASK_ID` | Reassign a task | `--to` |

### Knowledge Base Commands

| Command | Description | Key Options |
|---|---|---|
| `arc-team kb tree` | Show KB structure | `--depth` |
| `arc-team kb search QUERY` | Search by tags/content | `--type`, `--tags` |
| `arc-team kb read PATH` | Read an entry | `--summary-only` |
| `arc-team kb add` | Create a new entry | `--path`, `--title`, `--type`, `--tags`, `--body` |
| `arc-team kb edit PATH` | Edit an entry | Opens `$EDITOR` or `--body` for inline |
| `arc-team kb link FROM TO` | Create a bidirectional link | |
| `arc-team kb backlinks PATH` | Show what links to this entry | |

### File Store Commands

| Command | Description | Key Options |
|---|---|---|
| `arc-team files tree` | Show file structure | `--depth` |
| `arc-team files search QUERY` | Search manifest | `--project`, `--tags` |
| `arc-team files open PATH` | Open a file | |
| `arc-team files add` | Add a file | `--path`, `--file`, `--description`, `--tags`, `--project` |
| `arc-team files info PATH` | Show file metadata | |

---

## 12. Scaling Roadmap

### Phase 1: Flat Files (Current)

| Attribute | Detail |
|---|---|
| Agent capacity | 1–50 concurrent agents |
| Storage | JSON/JSONL/Markdown on local filesystem |
| Concurrency | fcntl file locking for write safety |
| Dependencies | Python 3.11+ standard library only |
| Deployment | Single node, air-gapped compatible |
| Estimated code | 2,500–3,500 lines Python |
| Timeline | 4–6 weeks |

### Phase 2: SQLite (100+ agents)

| Attribute | Detail |
|---|---|
| Agent capacity | 50–500 concurrent agents |
| Storage | SQLite for messages and tasks (high-write); Markdown for KB (human-editable) |
| Concurrency | SQLite WAL mode for concurrent reads + moderate writes |
| Migration | Swap FileBackend for SQLiteBackend; no consumer changes |
| New capability | Full-text search on messages and KB (SQLite FTS5) |
| Dependencies | sqlite3 (stdlib) |
| Timeline | 2–3 weeks after Phase 1 |

### Phase 3: SQLite + Sharding (1,000+ agents)

| Attribute | Detail |
|---|---|
| Agent capacity | 500–2,000 concurrent agents |
| Storage | Sharded SQLite: message channels across DB files, tasks by project |
| Real-time | Optional ZMQ or Unix sockets for inbox notifications (replace polling) |
| New capability | Agent presence/status; real-time collaboration indicators |
| Dependencies | pyzmq (optional) |
| Timeline | 3–4 weeks after Phase 2 |

### Phase 4: Distributed (10,000+ agents)

| Attribute | Detail |
|---|---|
| Agent capacity | 2,000–10,000+ concurrent agents |
| Storage | PostgreSQL for write path; Redis Streams or NATS for message bus |
| KB search | Dedicated search index (tantivy/Rust or Elasticsearch) |
| New capability | Multi-node deployment; horizontal scaling; APEX-CONTEXT integration |
| Dependencies | PostgreSQL, Redis/NATS |
| Timeline | 6–8 weeks after Phase 3; aligns with APEX-CONTEXT development |

### Implementation Priority (Phase 1)

Build order within Phase 1, based on dependency chain:

| Order | Module | Depends On | Estimated LOC | Duration |
|---|---|---|---|---|
| 1 | Storage abstraction + FileBackend | None | 250–350 | 3–4 days |
| 2 | Audit logger | Storage | 80–100 | 1 day |
| 3 | Entity registry | Storage | 100–150 | 1–2 days |
| 4 | Messaging service + CLI | Storage, Audit, Registry | 500–700 | 5–7 days |
| 5 | Task engine + CLI | Storage, Audit, Messaging | 400–600 | 5–7 days |
| 6 | Knowledge base + CLI | Storage, Audit | 400–500 | 5–7 days |
| 7 | File store + CLI | Storage, Audit | 200–300 | 2–3 days |
| 8 | Cross-ref index | All subsystems | 150–200 | 2–3 days |
| 9 | ARC Agent plugin | All subsystems | 200–300 | 3–4 days |
| 10 | Security (ACL enforcement) | All subsystems | 200–300 | 3–4 days |

---

## 13. Open Questions and Design Decisions

### Decisions Made

| Decision | Choice | Rationale |
|---|---|---|
| Message format | JSONL over Markdown | Structured metadata agents parse without LLM calls; Markdown is for KB where human readability matters |
| Inbox pattern | Per-entity inbox file | Compatible with cron/wake-up pattern; no polling infrastructure; simple drain semantics |
| KB format | Markdown + YAML frontmatter | Human-readable, git-versionable, LLM-native; frontmatter provides structured agent interface |
| Storage abstraction | Protocol-based backend | Enables flat files now, SQLite later, Postgres at scale—zero consumer code changes |
| Addressing | Typed URIs (`agent://`, `channel://`, `role://`) | Self-documenting, extensible, no ambiguity; agents parse routing without NLP |
| Threading | Query filter on thread_id | No separate data structure; zero storage overhead; threads are just views over streams |
| Audit | Append-only JSONL | Satisfies NIST 800-53 AU controls; tamper-evident; simple, portable, no database needed |

### Open Questions

| Question | Options | Recommendation |
|---|---|---|
| Agent discovery | A: Static registry only B: Registry + capability advertisement C: Registry + heartbeat/presence | Start with A; add heartbeat in Phase 3 when real-time matters |
| Synchronous agent-to-agent calls | A: Purely async (all messaging) B: Async + sync RPC option C: Async + callback pattern | Start with A (async only); sync adds complexity and coupling. Callbacks via action_required messages |
| KB versioning | A: Git integration B: Built-in changelog in frontmatter C: Separate version store | A (git) for fed environments; it is already on every machine and provides full audit trail for free |
| Message retention policy | A: Retain all forever B: TTL-based expiry C: Archive after N days | A for Phase 1; add archival in Phase 3 when storage becomes a concern |
| Task auto-creation from messages | A: msg_type=task auto-creates task B: Explicit task creation only C: Configurable per channel | Start with B; add C in Phase 2 for workflow automation |
| File conflict resolution | A: Last-write-wins B: Optimistic locking (version check) C: Agent coordination via messaging | A + C: Last-write-wins with convention that agents coordinate via messages. Add B in Phase 2 |

---

## Appendix A: Complete Data Directory Structure

```
.arc/team/                               # ARC Team root
├── messages/                            # Messaging subsystem
│   ├── channels/                        # Channel definitions
│   │   └── {channel-name}.json
│   ├── streams/                         # Message content (JSONL)
│   │   ├── channel/{name}.jsonl
│   │   ├── direct/{a}__{b}.jsonl
│   │   └── role/{role}.jsonl
│   ├── inboxes/                         # Per-entity inbox queues
│   │   └── {entity_id}.jsonl
│   └── registry/                        # Entity definitions
│       └── {entity_id}.json
├── tasks/                               # Task engine
│   ├── _board.json                      # Task index
│   ├── active/{task_id}.json
│   ├── completed/{task_id}.json
│   └── templates/{name}.json
├── kb/                                  # Knowledge base
│   ├── _index.json                      # Full tree index
│   ├── _backlinks.json                  # Backlink index
│   ├── processes/*.md
│   ├── entities/**/*.md
│   ├── decisions/*.md
│   └── reference/*.md
├── files/                               # File store
│   ├── _manifest.json                   # File registry
│   ├── projects/**/*
│   ├── templates/*
│   └── reports/*
├── security/                            # Access control
│   └── acl.json                         # Role-based permissions
└── audit/                               # Audit trail
    └── audit.jsonl                      # Append-only log
```

---

## Appendix B: Message Flow Examples

### Example 1: Task Assignment and Completion

1. Josh sends task message to `channel://project-alpha`
2. Message delivered to inboxes of all channel members
3. `agent://procurement-01` wakes up, drains inbox, sees task message
4. Agent loads task_042 via refs, reads KB entries for context
5. Agent produces `vendor-analysis.xlsx`, writes KB entry, updates task outputs
6. Agent sends result message to `user://josh` with refs to task, KB, and file
7. Josh receives result in inbox, reviews deliverables via refs

### Example 2: Multi-Agent Collaboration

1. `agent://procurement-01` receives complex task, decomposes into 3 subtasks
2. Subtask 1 assigned to self, subtask 2 assigned to `agent://analyst-01`, subtask 3 assigned to `agent://analyst-02`
3. Subtask assignments generate task messages to each agent's inbox
4. Each agent works independently, producing KB entries and files
5. As subtasks complete, parent task progress updates, watchers notified
6. When all subtasks complete, procurement agent assembles final deliverable
7. Result message sent with full provenance chain: task → subtasks → KB entries → files

### Example 3: Knowledge Discovery Chain

1. Agent receives question: "What vendors meet CMMC L2?"
2. Agent calls `search_kb(tags=["cmmc", "vendor"])`
3. Finds `kb://vendors/cmmc-qualified`, reads summary from frontmatter
4. Follows links to `kb://processes/vendor-onboarding` for the evaluation process
5. Follows backlinks from vendor entry to find related decisions and tasks
6. Synthesizes response with full context, cites sources via refs
